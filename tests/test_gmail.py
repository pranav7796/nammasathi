"""GmailClient against the in-process fake: parsing, needs_reply, sending, errors."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.google.gmail import (
    IMPORTANT_QUERY,
    MAX_BODY,
    GmailClient,
    build_raw,
    html_to_text,
)
from app.integrations.base import (
    IntegrationError,
    OutgoingEmail,
    RateLimited,
    ReauthRequired,
)
from tests.fake_google import (
    INJECTION,
    FakeGoogle,
    make_message,
    part,
    rate_limit,
    scope_error,
    server_error,
)
from tests.test_google_oauth import connect, google_site

USER = "user-1"


class FakeTokens:
    """A TokenProvider backed by the fake's own access tokens."""

    def __init__(self, fake: FakeGoogle) -> None:
        self.fake = fake
        self.current = fake._new_access()  # pyright: ignore[reportPrivateUsage]
        self.invalidated = 0

    async def access_token(self, user_id: str, provider: str) -> str:
        assert provider == "gmail"
        return self.current

    async def invalidate(self, user_id: str, provider: str, rejected: str) -> str:
        self.invalidated += 1
        self.current = self.fake._new_access()  # pyright: ignore[reportPrivateUsage]
        return self.current


class NoInvalidate:
    """A provider that knows nothing about invalidation: asking again gives a fresh token."""

    def __init__(self, fake: FakeGoogle) -> None:
        self.fake = fake
        self.calls = 0

    async def access_token(self, user_id: str, provider: str) -> str:
        self.calls += 1
        if self.calls == 1:
            return "stale"
        return self.fake._new_access()  # pyright: ignore[reportPrivateUsage]


class Sleeps:
    def __init__(self) -> None:
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


def client_for(
    fake: FakeGoogle, sleeps: Sleeps | None = None
) -> tuple[GmailClient, FakeTokens, Sleeps]:
    tokens = FakeTokens(fake)
    sleeps = sleeps or Sleeps()
    return GmailClient(tokens, fake.transport(), sleep=sleeps), tokens, sleeps


# Reading


async def test_search_and_important_parse_summaries() -> None:
    fake = FakeGoogle()
    fake.add(
        make_message(
            "m1",
            sender='"Ravi K" <Ravi@Example.com>',
            subject="Lunch?",
            body="Can't make it",
            labels=("INBOX", "UNREAD", "IMPORTANT"),
        )
    )
    fake.add(make_message("m2", labels=("INBOX",), body="x"))
    client, _, _ = client_for(fake)
    found = await client.search(USER, "from:ravi", 10)
    first = next(s for s in found if s.id == "m1")
    assert (first.sender_name, first.sender_email) == ("Ravi K", "ravi@example.com")
    assert first.subject == "Lunch?"
    assert first.snippet == "Can't make it"  # HTML entities in Gmail snippets are unescaped
    assert first.unread and first.important and not first.needs_reply
    assert first.received.tzinfo is not None
    assert first.received.timestamp() == 1_700_000_000
    important = await client.important(USER, 5)
    assert [s.id for s in important] == ["m1"]
    listing = [r for r in fake.gmail_requests if r.url.path.endswith("/messages")][-1]
    assert listing.url.params["q"] == IMPORTANT_QUERY == "is:important in:inbox newer_than:14d"


async def test_get_plain_multipart_prefers_text_plain_and_skips_attachments() -> None:
    fake = FakeGoogle()
    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "filename": "",
        "body": {},
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "filename": "",
                "body": {},
                "parts": [part("text/plain", "Plain version"), part("text/html", "<b>HTML</b>")],
            },
            part("text/plain", "secret attachment text", filename="notes.txt"),
        ],
    }
    fake.add(
        make_message(
            "m1",
            payload=payload,
            to="me@example.com, Other <o@example.com>",
            cc="C <C@Example.com>",
            message_id="<abc@x>",
        )
    )
    client, _, _ = client_for(fake)
    mail = await client.get(USER, "m1")
    assert mail.body_text == "Plain version"
    assert mail.to == ("me@example.com", "o@example.com")
    assert mail.cc == ("c@example.com",)
    assert mail.message_id_header == "<abc@x>"
    assert mail.summary.subject == "Hello"


async def test_get_html_only_is_stripped_to_text() -> None:
    fake = FakeGoogle()
    markup = (
        "<html><head><style>p{color:red}</style></head><body><script>alert(1)</script>"
        "<p>Hello &amp; welcome</p><div>Second<br>line</div></body></html>"
    )
    fake.add(make_message("m1", payload=part("text/html", markup)))
    client, _, _ = client_for(fake)
    text = (await client.get(USER, "m1")).body_text
    assert "Hello & welcome" in text and "Second\nline" in text
    assert "alert" not in text and "color" not in text and "<" not in text
    assert html_to_text("<p>a</p><p>b</p>") == "a\n\nb"


async def test_get_decodes_charsets_and_truncates() -> None:
    fake = FakeGoogle()
    fake.add(make_message("m1", payload=part("text/plain", "café", charset="iso-8859-1")))
    fake.add(make_message("m2", body="x" * (MAX_BODY + 5000)))
    client, _, _ = client_for(fake)
    assert (await client.get(USER, "m1")).body_text == "café"
    assert len((await client.get(USER, "m2")).body_text) == MAX_BODY


async def test_get_rejects_odd_ids_without_a_request() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    with pytest.raises(IntegrationError):
        await client.get(USER, "../profile")
    assert fake.gmail_requests == []


async def test_injection_text_is_inert_data() -> None:
    fake = FakeGoogle()
    fake.add(make_message("m1", body=f"Hi.\n{INJECTION}", subject=INJECTION))
    client, _, _ = client_for(fake)
    mail = await client.get(USER, "m1")
    assert INJECTION in mail.body_text
    assert mail.summary.subject == INJECTION
    assert fake.sent == []
    assert not any(r.method == "POST" for r in fake.gmail_requests)


# needs_reply


def thread_fixture(fake: FakeGoogle) -> None:
    me = "me@example.com"
    # t1: they wrote last -> needs a reply
    fake.add(make_message("t1a", thread="t1", when=1_700_000_000, subject="One"))
    fake.add(make_message("t1b", thread="t1", when=1_700_000_500, subject="One"))
    # t2: they wrote, then I replied -> done
    fake.add(make_message("t2a", thread="t2", when=1_700_000_000))
    fake.add(make_message("t2b", thread="t2", when=1_700_000_900, sender=me, labels=("SENT",)))
    # t3: I wrote first, they answered -> needs a reply
    fake.add(make_message("t3a", thread="t3", when=1_700_000_100, sender=me, labels=("SENT",)))
    fake.add(make_message("t3b", thread="t3", when=1_700_000_800, subject="Three"))
    # t4: only mine in the inbox thread -> done
    fake.add(make_message("t4a", thread="t4", when=1_700_000_700, sender=f"Me <{me}>"))


async def test_needs_reply_uses_the_newest_message_in_each_thread() -> None:
    fake = FakeGoogle()
    thread_fixture(fake)
    client, _, _ = client_for(fake)
    found = await client.needs_reply(USER, 10)
    assert [(s.thread_id, s.id) for s in found] == [("t3", "t3b"), ("t1", "t1b")]
    assert all(s.needs_reply for s in found)
    listing = next(r for r in fake.gmail_requests if r.url.path.endswith("/threads"))
    assert listing.url.params["q"] == "in:inbox newer_than:14d"


async def test_needs_reply_fetches_at_most_limit_threads_four_at_a_time() -> None:
    fake = FakeGoogle()
    for n in range(12):
        fake.add(make_message(f"x{n}", thread=f"th{n}", when=1_700_000_000 + n))

    class Probe(httpx.AsyncBaseTransport):
        running = 0
        peak = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "/threads/" in request.url.path:
                Probe.running += 1
                Probe.peak = max(Probe.peak, Probe.running)
                await asyncio.sleep(0.01)
                Probe.running -= 1
            return await fake.transport().handle_async_request(request)

    tokens = FakeTokens(fake)
    client = GmailClient(tokens, Probe())
    found = await client.needs_reply(USER, 8)
    assert len(found) == 8
    fetched = [r for r in fake.gmail_requests if "/threads/" in r.url.path]
    assert len(fetched) == 8
    assert 1 < Probe.peak <= 4


# Sending


def decode(raw: str) -> str:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()


async def test_send_builds_a_correct_threaded_message_and_verifies() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    mail = OutgoingEmail(
        to=("Ravi <ravi@example.com>",),
        subject="Re: Lunch? नमस्ते",
        body="Sounds good.\nSee you at 1.",
        thread_id="th-9",
        in_reply_to="<orig@mail.example.com>",
    )
    sent = await client.send(USER, mail, "task-1")
    assert sent.verified is True and sent.id == "sent-1" and sent.thread_id == "th-9"
    (record,) = fake.sent
    assert record["thread_id"] == "th-9"
    message = record["parsed"]
    assert message["To"] == "ravi@example.com"
    assert message["Subject"] == "Re: Lunch? नमस्ते"
    assert message["In-Reply-To"] == "<orig@mail.example.com>"
    assert message["References"] == "<orig@mail.example.com>"
    assert message.get_content().strip() == "Sounds good.\nSee you at 1."
    assert "raw" not in decode(record["raw"]).lower().split("\n\n")[0]
    post = [r for r in fake.gmail_requests if r.method == "POST"]
    assert len(post) == 1 and post[0].url.path.endswith("/messages/send")


async def test_send_without_reply_context_has_no_threading_headers() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    await client.send(USER, OutgoingEmail(("a@example.com",), "Hi", "Body"), "k")
    message = fake.sent[0]["parsed"]
    assert message["In-Reply-To"] is None and message["References"] is None
    assert fake.sent[0]["thread_id"] is None


@pytest.mark.parametrize(
    "mail",
    [
        OutgoingEmail(("a@example.com\r\nBcc: evil@example.com",), "Hi", "b"),
        OutgoingEmail(("a@example.com",), "Hi\nBcc: evil@example.com", "b"),
        OutgoingEmail(("a@example.com",), "Hi\r\n", "b"),
        OutgoingEmail(("a@example.com",), "Hi", "b", in_reply_to="<x>\r\nBcc: e@e.com"),
        OutgoingEmail(("a@example.com, evil@example.com",), "Hi", "b"),
        OutgoingEmail(("not-an-address",), "Hi", "b"),
        OutgoingEmail((), "Hi", "b"),
    ],
)
async def test_send_rejects_header_injection_and_bad_recipients(mail: OutgoingEmail) -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    with pytest.raises(IntegrationError):
        await client.send(USER, mail, "k")
    assert fake.sent == [] and fake.gmail_requests == []
    with pytest.raises(ValueError):
        build_raw(mail)


async def test_body_with_header_lookalikes_stays_in_the_body() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    await client.send(USER, OutgoingEmail(("a@example.com",), "Hi", "x\r\nBcc: e@e.com\r\n"), "k")
    assert fake.sent[0]["parsed"]["Bcc"] is None


async def test_unverified_when_the_sent_label_is_missing() -> None:
    fake = FakeGoogle(drop_sent_label=True)
    client, _, _ = client_for(fake)
    sent = await client.send(USER, OutgoingEmail(("a@example.com",), "Hi", "b"), "k")
    assert sent.verified is False and sent.id == "sent-1"


async def test_unverified_when_the_read_back_fails() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    original = fake._send  # pyright: ignore[reportPrivateUsage]

    def send_then_break(request: httpx.Request) -> httpx.Response:
        response = original(request)
        fake.fail_gmail.extend([server_error()] * 3)
        return response

    fake._send = send_then_break  # type: ignore[method-assign]  # pyright: ignore
    sent = await client.send(USER, OutgoingEmail(("a@example.com",), "Hi", "b"), "k")
    assert sent.verified is False and len(fake.sent) == 1


async def test_same_idempotency_key_sends_once_even_concurrently() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    mail = OutgoingEmail(("a@example.com",), "Hi", "b")
    results = await asyncio.gather(*(client.send(USER, mail, "same") for _ in range(3)))
    assert len(fake.sent) == 1 and len(set(results)) == 1
    other = await client.send(USER, mail, "different")
    assert other.id != results[0].id and len(fake.sent) == 2
    # the same key for another person is a different send
    await client.send("user-2", mail, "same")
    assert len(fake.sent) == 3


# Errors


async def test_401_refreshes_once_then_succeeds() -> None:
    fake = FakeGoogle()
    fake.add(make_message("m1"))
    client, tokens, _ = client_for(fake)
    fake.expire_access()
    assert (await client.get(USER, "m1")).summary.id == "m1"
    assert tokens.invalidated == 1


async def test_401_works_with_a_provider_that_cannot_invalidate() -> None:
    fake = FakeGoogle()
    fake.add(make_message("m1"))
    provider = NoInvalidate(fake)
    client = GmailClient(provider, fake.transport())
    assert (await client.get(USER, "m1")).summary.id == "m1"


async def test_a_second_401_means_reauth() -> None:
    fake = FakeGoogle()
    client, tokens, _ = client_for(fake)

    async def bad(user_id: str, provider: str, rejected: str) -> str:
        return "still-bad"

    tokens.invalidate = bad  # type: ignore[method-assign]
    fake.expire_access()
    with pytest.raises(ReauthRequired):
        await client.search(USER, "x", 5)


async def test_403_scope_error_is_reauth() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    fake.fail_gmail.append(scope_error())
    with pytest.raises(ReauthRequired) as caught:
        await client.search(USER, "x", 5)
    assert "connect Gmail again" in str(caught.value)


async def test_429_is_retried_with_retry_after_then_raises_rate_limited() -> None:
    fake = FakeGoogle()
    fake.add(make_message("m1"))
    client, _, sleeps = client_for(fake)
    fake.fail_gmail.append(rate_limit("2"))
    assert len(await client.search(USER, "x", 5)) == 1
    assert sleeps.waited == [2.0]
    fake.fail_gmail.extend([rate_limit("3")] * 5)
    sleeps.waited.clear()
    with pytest.raises(RateLimited) as caught:
        await client.search(USER, "x", 5)
    assert caught.value.retry_after == 3.0 and caught.value.retryable
    assert sleeps.waited == [3.0, 3.0]  # bounded: two retries


async def test_long_retry_after_is_not_waited_for() -> None:
    fake = FakeGoogle()
    client, _, sleeps = client_for(fake)
    fake.fail_gmail.append(rate_limit("600"))
    with pytest.raises(RateLimited) as caught:
        await client.search(USER, "x", 5)
    assert caught.value.retry_after == 600.0 and sleeps.waited == []


async def test_403_rate_limit_reason_is_rate_limited_not_reauth() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    limited: tuple[int, dict[str, str], dict[str, Any]] = (
        403,
        {},
        {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}},
    )
    fake.fail_gmail.extend([limited] * 3)
    with pytest.raises(RateLimited):
        await client.search(USER, "x", 5)


async def test_5xx_is_retryable_after_bounded_retries() -> None:
    fake = FakeGoogle()
    client, _, sleeps = client_for(fake)
    fake.fail_gmail.extend([server_error()] * 5)
    with pytest.raises(IntegrationError) as caught:
        await client.search(USER, "x", 5)
    assert caught.value.retryable is True and len(sleeps.waited) == 2


async def test_other_4xx_is_not_retryable() -> None:
    fake = FakeGoogle()
    client, _, sleeps = client_for(fake)
    fake.fail_gmail.append((400, {}, {"error": {"message": "bad q"}}))
    with pytest.raises(IntegrationError) as caught:
        await client.search(USER, "x", 5)
    assert caught.value.retryable is False and sleeps.waited == []
    assert "bad q" not in str(caught.value)


async def test_missing_message_is_a_plain_error() -> None:
    fake = FakeGoogle()
    client, _, _ = client_for(fake)
    with pytest.raises(IntegrationError) as caught:
        await client.get(USER, "nope")
    assert caught.value.retryable is False


async def test_send_is_never_retried_and_failures_are_not_marked_retryable() -> None:
    fake = FakeGoogle()
    client, _, sleeps = client_for(fake)
    mail = OutgoingEmail(("a@example.com",), "Hi", "b")
    fake.fail_gmail.extend([server_error(), server_error()])
    with pytest.raises(IntegrationError) as caught:
        await client.send(USER, mail, "k1")
    assert caught.value.retryable is False and "may or may not" in str(caught.value)
    assert len(fake.fail_gmail) == 1 and sleeps.waited == []  # exactly one attempt
    fake.fail_gmail[:] = [rate_limit("1")]
    with pytest.raises(RateLimited):
        await client.send(USER, mail, "k2")
    assert fake.sent == []
    # a failed send is not remembered as sent: the same key may be tried again
    assert (await client.send(USER, mail, "k1")).verified is True


async def test_timeout_on_send_is_ambiguous_and_not_retryable() -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    fake = FakeGoogle()
    tokens = FakeTokens(fake)
    client = GmailClient(tokens, httpx.MockTransport(hang))
    with pytest.raises(IntegrationError) as caught:
        await client.send(USER, OutgoingEmail(("a@example.com",), "Hi", "b"), "k")
    assert caught.value.retryable is False
    with pytest.raises(IntegrationError) as read:
        await client.search(USER, "x", 5)
    assert read.value.retryable is True


# With the real token provider


async def test_gmail_with_google_tokens_refreshes_after_a_401(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        s.fake.add(make_message("m1"))
        client = GmailClient(s.oauth.tokens, s.fake.transport())
        assert (await client.get(uid, "m1")).summary.id == "m1"
        s.fake.expire_access()
        assert (await client.get(uid, "m1")).summary.id == "m1"
        grants = [r["grant_type"] for r in s.fake.token_requests]
        assert grants == ["authorization_code", "refresh_token"]
