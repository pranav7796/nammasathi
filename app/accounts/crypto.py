"""Password hashing and encryption of stored Swiggy tokens.

Passwords: scrypt with N=2^15, r=8, p=3, one of OWASP's listed configurations, chosen to use about
32 MB per hash on a small server.

Tokens: AES-256-GCM. The user and issuer are authenticated with the ciphertext, so a row copied to
another user cannot be decrypted. Several keys are accepted so they can be rotated.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from secrets import compare_digest

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_N, _R, _P = 2**15, 8, 3
_MAX_MEMORY = 64 * 1024 * 1024
_NONCE_BYTES = 12


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAX_MEMORY, dklen=32)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = _scrypt(password, salt, _N, _R, _P)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = _scrypt(password, base64.urlsafe_b64decode(salt), int(n), int(r), int(p))
        return compare_digest(digest, base64.urlsafe_b64decode(expected))
    except (ValueError, binascii.Error):
        return False


# Verifying against this when no account exists costs the same as a real check.
DUMMY_PASSWORD_HASH = hash_password("no account has this password")


def token_hash(token: str) -> str:
    """Session and one-time tokens are long and random, so a plain digest is the right store."""
    return hashlib.sha256(token.encode()).hexdigest()


def user_hash(user_id: str) -> str:
    """For logs: Swiggy's go-live checklist asks for a hashed user id on each call."""
    return "sha256:" + hashlib.sha256(user_id.encode()).hexdigest()[:16]


class TokenDecryptError(ValueError):
    """No configured key opens this record, or it belongs to someone else."""


def parse_keys(raw: str) -> tuple[bytes, ...]:
    """Comma-separated base64 keys of 32 bytes. The first encrypts; all can decrypt."""
    keys: list[bytes] = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        try:
            key = base64.urlsafe_b64decode(text)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEYS must be base64") from exc
        if len(key) != 32:
            raise ValueError("Each key in TOKEN_ENCRYPTION_KEYS must be 32 bytes")
        keys.append(key)
    if not keys:
        raise ValueError("TOKEN_ENCRYPTION_KEYS is empty")
    return tuple(keys)


class TokenCipher:
    def __init__(self, keys: tuple[bytes, ...]) -> None:
        self._ciphers = tuple(AESGCM(key) for key in keys)

    def encrypt(self, plaintext: str, *, user_id: str, issuer: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._ciphers[0].encrypt(nonce, plaintext.encode(), f"{user_id}|{issuer}".encode())
        return _b64(nonce + sealed)

    def decrypt(self, ciphertext: str, *, user_id: str, issuer: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(ciphertext)
        except (ValueError, binascii.Error) as exc:
            raise TokenDecryptError("Stored token is malformed") from exc
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        for cipher in self._ciphers:
            try:
                return cipher.decrypt(nonce, sealed, f"{user_id}|{issuer}".encode()).decode()
            except InvalidTag:
                continue
        raise TokenDecryptError("Stored token cannot be opened")
