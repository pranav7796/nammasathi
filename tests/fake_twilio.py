"""httpx.MockTransport stand-in for Twilio's Messages API and account read. No network.

Shapes follow https://www.twilio.com/docs/messaging/api/message-resource and Twilio's error pages.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qsl

import httpx


class FakeTwilio:
    def __init__(self, mode: str = "ok", account: str = "ACtestaccountsid") -> None:
        self.mode = mode
        self.account = account
        self.sent: list[dict[str, str]] = []
        self.auth: list[str] = []
        self.counter = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.twilio.com":
            return httpx.Response(404, json={"message": "unexpected host"})
        header = request.headers.get("Authorization", "")
        user = (
            base64.b64decode(header.removeprefix("Basic ")).decode().split(":")[0] if header else ""
        )
        self.auth.append(user)
        mode = self.mode
        if mode == "401":
            return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})
        if request.method == "GET":
            if request.url.path == f"/2010-04-01/Accounts/{self.account}/Messages.json":
                return httpx.Response(200, json={"messages": [], "page_size": 1})
            return httpx.Response(404, json={"code": 20404})
        if mode == "timeout":
            raise httpx.ReadTimeout("hung", request=request)
        if mode == "connect":
            raise httpx.ConnectError("down", request=request)
        if mode == "500":
            return httpx.Response(500, json={"message": "boom"})
        if mode == "429":
            return httpx.Response(429, json={"code": 20429})
        if mode.startswith("code:"):
            return httpx.Response(400, json={"code": int(mode[5:]), "message": "refused"})
        form = dict(parse_qsl(request.content.decode()))
        self.sent.append(form)
        self.counter += 1
        return httpx.Response(
            201,
            json={"sid": f"SMfake{self.counter:04d}", "status": "queued", "to": form.get("To")},
        )
