"""Test-wide safety net: no test may reach a real network or the real credential store."""

from __future__ import annotations

import socket
from typing import Any

import pytest

_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError(f"Test attempted a real network connection to {address!r}")
    _real_connect(self, address)


@pytest.fixture(autouse=True)
def _no_real_world(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)

    def no_keyring() -> None:
        raise AssertionError("Test attempted to use the real OS credential store")

    monkeypatch.setattr("keyring.get_keyring", no_keyring)
    for name in ("COMMERCE_BACKEND", "SWIGGY_LIVE_ACCESS", "ALLOW_REAL_CHECKOUT"):
        monkeypatch.delenv(name, raising=False)
