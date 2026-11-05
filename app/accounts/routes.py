"""Registration, sign-in, and account management."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import cast

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.accounts.service import (
    UNAVAILABLE_MESSAGE,
    AccountService,
    AccountsUnavailable,
    environment_name,
)
from app.accounts.store import EmailTaken, InvalidEmail, User, normalize_email
from app.commerce.order_journal import UNRESOLVED
from app.observability import log_event

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
VERIFY_LINK_SECONDS = 24 * 60 * 60
RESET_LINK_SECONDS = 60 * 60
_COMMON = frozenset(
    {
        "1234567890",
        "12345678910",
        "1234512345",
        "0987654321",
        "qwertyuiop",
        "passwordpassword",
        "password123",
        "password1234",
        "iloveyou123",
        "swiggy12345",
    }
)

# Ends whatever a user's account has open. Set by the web layer, which owns conversations.
SessionCloser = Callable[[str], Awaitable[None]]


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class Registration(Credentials):
    consent: bool = False


class EmailOnly(BaseModel):
    email: str = Field(max_length=254)


class TokenOnly(BaseModel):
    token: str = Field(max_length=200)


class PasswordReset(TokenOnly):
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class PasswordCheck(BaseModel):
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


def _service(request: Request) -> AccountService:
    """Accounts on this site; 404 if there are none. Database trouble surfaces as a 503."""
    service = cast(AccountService | None, getattr(request.app.state, "accounts", None))
    if service is None:
        raise HTTPException(status_code=404, detail="Accounts are not enabled here.")
    return service


def _address(request: Request) -> str:
    return request.client.host if request.client else ""


def _closer(request: Request) -> SessionCloser:
    return cast(SessionCloser, request.app.state.close_user_conversations)


def _check_new_password(password: str, email: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422, detail=f"Use at least {MIN_PASSWORD_LENGTH} characters."
        )
    lowered = password.casefold()
    if lowered in _COMMON or lowered == email or len(set(lowered)) < 4:
        raise HTTPException(status_code=422, detail="That password is too easy to guess.")


def _site(service: AccountService, request: Request) -> str:
    return service.settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")


async def _send_verification(service: AccountService, request: Request, user: User) -> bool:
    if not service.mailer.can_send:
        return False
    token = await service.run(service.store.issue_one_time, user.id, "verify", VERIFY_LINK_SECONDS)
    # In the fragment, so the token never reaches a server log or a Referer header.
    return await service.mailer.send(
        user.email,
        "Confirm your email for Namma Saathi",
        "Open this link to confirm your email. It works once, for 24 hours.\n\n"
        f"{_site(service, request)}/#verify={token}\n\n"
        "If you did not create this account, you can ignore this message.",
    )


def _start_browser_session(service: AccountService, response: Response, token: str) -> None:
    response.set_cookie(
        service.cookie_name,
        token,
        httponly=True,
        secure=service.secure_cookies,
        samesite="lax",
        path="/",
        max_age=14 * 24 * 60 * 60,
    )


def _end_browser_session(service: AccountService, response: Response) -> None:
    response.delete_cookie(
        service.cookie_name, path="/", httponly=True, secure=service.secure_cookies, samesite="lax"
    )


async def _required_user(service: AccountService, request: Request) -> User:
    await service.require_ready()
    user = await service.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


def account_router() -> APIRouter:
    router = APIRouter(prefix="/api/account")

    @router.get("/me")
    async def me(request: Request) -> dict[str, object]:
        service = cast(AccountService | None, getattr(request.app.state, "accounts", None))
        if service is None:
            return {"enabled": False, "user": None, "live": {"open": False}}
        try:
            user = await service.current_user(request)
            if not await service.ensure_ready():
                raise AccountsUnavailable(UNAVAILABLE_MESSAGE)
            refusal = await service.live_refusal(user)
            connected = await service.tokens.connected(user.id) if user else False
            language = await service.run(service.store.language, user.id) if user else None
        except AccountsUnavailable:
            # The page says accounts are resting, and keeps offering the demo.
            return {
                "enabled": True,
                "available": False,
                "message": UNAVAILABLE_MESSAGE,
                "user": None,
                "live": {"open": False},
            }
        settings = service.settings
        return {
            "enabled": True,
            "available": True,
            "preferences": {"language": language},
            "registration_open": settings.open_registration,
            "email_available": service.mailer.can_send,
            "user": (
                None
                if user is None
                else {
                    "email": user.email,
                    "email_verified": user.email_verified,
                    "live_access": user.live_access,
                    "created_at": user.created_at,
                }
            ),
            "live": {
                "open": settings.multi_user_live,
                "environment": environment_name(settings) if settings.multi_user_live else "",
                "connected": connected,
                "available": refusal is None,
                "reason": refusal or "",
                "cart_writes_enabled": settings.allow_swiggy_cart_writes,
                "checkout_enabled": settings.allow_real_checkout,
                "booking_enabled": settings.allow_real_booking,
            },
        }

    @router.post("/register")
    async def register(
        payload: Registration, request: Request, response: Response
    ) -> dict[str, str]:
        service = _service(request)
        if not service.settings.open_registration:
            raise HTTPException(status_code=403, detail="Registration is closed for now.")
        if not payload.consent:
            raise HTTPException(status_code=422, detail="Please accept the privacy notice.")
        await service.require_ready()
        # Each sign-up costs a slow hash. One address, and the whole site, get a bounded number
        # per hour, counted apart from failed sign-ins so one person cannot block the others.
        address = _address(request)
        if service.limiter.signup_blocked(address):
            log_event("account", op="register", outcome="rate_limited")
            raise HTTPException(
                status_code=429, detail="Too many new accounts just now. Please try again later."
            )
        service.limiter.record_signup(address)
        try:
            email = normalize_email(payload.email)
        except InvalidEmail as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _check_new_password(payload.password, email)
        hashed = await service.hash_password(payload.password)
        try:
            user = await service.run(service.store.create_user, email, hashed)
        except EmailTaken as exc:
            raise HTTPException(
                status_code=409, detail="An account with this email already exists."
            ) from exc
        session = await service.run(service.store.create_session, user.id)
        _start_browser_session(service, response, session)
        await _send_verification(service, request, user)
        log_event("account", op="register", outcome="ok")
        return {"status": "created"}

    @router.post("/login")
    async def login(payload: Credentials, request: Request, response: Response) -> dict[str, str]:
        service = _service(request)
        key = payload.email.strip().casefold()
        address = _address(request)
        if service.limiter.blocked(key, address):
            log_event("account", op="login", outcome="rate_limited")
            raise HTTPException(
                status_code=429, detail="Too many attempts. Please try again in 15 minutes."
            )
        user = await service.run(service.store.user_by_email, payload.email)
        matched = await service.check_password(
            payload.password, user.password_hash if user else None
        )
        if user is None or not matched:
            service.limiter.record_failure(key, address)
            log_event("account", op="login", outcome="refused")
            raise HTTPException(status_code=401, detail="That email or password is not right.")
        service.limiter.clear(key)
        previous = request.cookies.get(service.cookie_name)
        if previous:
            # A new sign-in never inherits a session someone else may have planted.
            await service.run(service.store.end_session, previous)
        session = await service.run(service.store.create_session, user.id)
        _start_browser_session(service, response, session)
        log_event("account", op="login", outcome="ok")
        return {"status": "signed_in"}

    @router.post("/logout")
    async def logout(request: Request, response: Response) -> dict[str, str]:
        service = _service(request)
        user = await service.current_user(request)
        token = request.cookies.get(service.cookie_name)
        if token:
            await service.run(service.store.end_session, token)
        if user is not None:
            await _closer(request)(user.id)
        _end_browser_session(service, response)
        return {"status": "signed_out"}

    @router.post("/logout-everywhere")
    async def logout_everywhere(request: Request, response: Response) -> dict[str, str]:
        service = _service(request)
        user = await _required_user(service, request)
        await service.run(service.store.end_all_sessions, user.id)
        await _closer(request)(user.id)
        _end_browser_session(service, response)
        return {"status": "signed_out"}

    @router.post("/password")
    async def change_password(
        payload: PasswordChange, request: Request, response: Response
    ) -> dict[str, str]:
        service = _service(request)
        user = await _required_user(service, request)
        if not await service.check_password(payload.current_password, user.password_hash):
            raise HTTPException(status_code=401, detail="Your current password is not right.")
        _check_new_password(payload.new_password, user.email)
        hashed = await service.hash_password(payload.new_password)
        await service.run(service.store.set_password, user.id, hashed)
        await _closer(request)(user.id)
        session = await service.run(service.store.create_session, user.id)
        _start_browser_session(service, response, session)
        log_event("account", op="password_change", outcome="ok")
        return {"status": "changed"}

    @router.post("/forgot")
    async def forgot(payload: EmailOnly, request: Request) -> dict[str, object]:
        """The same answer whether or not the account exists."""
        service = _service(request)
        if not service.mailer.can_send:
            return {"status": "unavailable", "support_email": service.settings.support_email}
        user = await service.run(service.store.user_by_email, payload.email)
        if user is not None:
            token = await service.run(
                service.store.issue_one_time, user.id, "reset", RESET_LINK_SECONDS
            )
            await service.mailer.send(
                user.email,
                "Reset your Namma Saathi password",
                "Open this link to choose a new password. It works once, for one hour.\n\n"
                f"{_site(service, request)}/#reset={token}\n\n"
                "If you did not ask for this, you can ignore this message.",
            )
        return {"status": "sent"}

    @router.post("/reset")
    async def reset(payload: PasswordReset, request: Request) -> dict[str, str]:
        service = _service(request)
        user_id = await service.run(service.store.consume_one_time, payload.token, "reset")
        user = await service.run(service.store.user_by_id, user_id) if user_id else None
        if user is None:
            raise HTTPException(status_code=400, detail="This link has expired or was used.")
        try:
            _check_new_password(payload.password, user.email)
        except HTTPException:
            # The link was spent checking it, so issue another rather than strand the person.
            await service.run(service.store.issue_one_time, user.id, "reset", RESET_LINK_SECONDS)
            raise
        hashed = await service.hash_password(payload.password)
        await service.run(service.store.set_password, user.id, hashed)
        await _closer(request)(user.id)
        log_event("account", op="password_reset", outcome="ok")
        return {"status": "changed"}

    @router.post("/verify")
    async def verify(payload: TokenOnly, request: Request) -> dict[str, str]:
        service = _service(request)
        user_id = await service.run(service.store.consume_one_time, payload.token, "verify")
        if user_id is None:
            raise HTTPException(status_code=400, detail="This link has expired or was used.")
        await service.run(service.store.mark_verified, user_id)
        return {"status": "verified"}

    @router.post("/resend-verification")
    async def resend(request: Request) -> dict[str, object]:
        service = _service(request)
        user = await _required_user(service, request)
        if user.email_verified:
            return {"status": "already_verified"}
        sent = await _send_verification(service, request, user)
        return {
            "status": "sent" if sent else "unavailable",
            "support_email": service.settings.support_email,
        }

    @router.get("/orders")
    async def orders(request: Request) -> dict[str, object]:
        """This user's real attempts only. Mock orders are never recorded here."""
        service = _service(request)
        user = await _required_user(service, request)
        attempts = await service.run(service.journal(user.id).attempts)
        return {
            "orders": [
                {
                    "capability": attempt.domain,
                    "status": attempt.status.value,
                    "needs_attention": attempt.status in UNRESOLVED,
                    "total_inr": attempt.total_inr,
                    "items": list(attempt.item_names),
                    "order_ids": list(attempt.order_ids),
                    "order_status": attempt.order_status,
                    "created_at": attempt.created_at,
                }
                for attempt in reversed(attempts)
            ]
        }

    @router.get("/history")
    async def history(request: Request) -> dict[str, object]:
        """This person's own demo conversation, newest last. Live turns are never kept."""
        service = _service(request)
        user = await _required_user(service, request)
        turns = await service.run(service.store.turns, user.id, 50)
        return {"turns": [asdict(turn) for turn in turns]}

    @router.post("/history/clear")
    async def clear_history(request: Request) -> dict[str, str]:
        service = _service(request)
        user = await _required_user(service, request)
        await service.run(service.store.clear_turns, user.id)
        return {"status": "cleared"}

    @router.get("/demo-orders")
    async def demo_orders(request: Request) -> dict[str, object]:
        """Finished demo orders and bookings. None of them were real."""
        service = _service(request)
        user = await _required_user(service, request)
        orders = await service.run(service.store.demo_orders, user.id, 50)
        return {"demo": True, "orders": [asdict(order) for order in orders]}

    @router.get("/export")
    async def export(request: Request) -> dict[str, object]:
        """Everything held about this person. Swiggy's own data stays with Swiggy."""
        service = _service(request)
        user = await _required_user(service, request)
        attempts = await service.run(service.journal(user.id).attempts)
        turns = await service.run(service.store.turns, user.id, 1000)
        demo = await service.run(service.store.demo_orders, user.id, 1000)
        language = await service.run(service.store.language, user.id)
        return {
            "preferences": {"language": language},
            "conversation_history": [asdict(turn) for turn in turns],
            "demo_orders": [asdict(order) for order in demo],
            "account": {
                "email": user.email,
                "email_verified": user.email_verified,
                "live_access": user.live_access,
                "created_at": user.created_at,
            },
            "swiggy_connected": await service.tokens.connected(user.id),
            "order_attempts": [
                {
                    "created_at": attempt.created_at,
                    "capability": attempt.domain,
                    "status": attempt.status.value,
                    "total_inr": attempt.total_inr,
                    "items": list(attempt.item_names),
                    "order_ids": list(attempt.order_ids),
                }
                for attempt in attempts
            ],
        }

    @router.post("/delete")
    async def delete(
        payload: PasswordCheck, request: Request, response: Response
    ) -> dict[str, str]:
        """Ends the Swiggy session, then removes the account and everything stored with it."""
        service = _service(request)
        user = await _required_user(service, request)
        if not await service.check_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="That password is not right.")
        await _closer(request)(user.id)
        await service.tokens.revoke(user.id)
        await service.run(service.store.delete_user, user.id)
        _end_browser_session(service, response)
        log_event("account", op="delete", outcome="ok")
        return {"status": "deleted"}

    return router
