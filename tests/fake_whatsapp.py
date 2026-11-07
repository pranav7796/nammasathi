"""httpx.MockTransport fake of the WhatsApp Cloud API messages endpoint. No network."""

from __future__ import annotations

import json

import httpx


class FakeWhatsApp:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.requests: list[httpx.Request] = []
        self.counter = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        mode = self.mode
        if mode == "ok":
            self.counter += 1
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": "x", "wa_id": "x"}],
                    "messages": [{"id": f"wamid.TEST{self.counter}"}],
                },
            )
        if mode == "timeout":
            raise httpx.ReadTimeout("hung", request=request)
        if mode == "connect":
            raise httpx.ConnectError("down", request=request)
        if mode == "500":
            return httpx.Response(500, json={"error": {"code": 2, "message": "boom"}})
        if mode == "429":
            return httpx.Response(429, json={})
        if mode == "no_id":
            return httpx.Response(200, json={"messaging_product": "whatsapp"})
        if mode == "401":
            return httpx.Response(401, json={})
        if mode.startswith("code:"):
            code = int(mode.split(":")[1])
            return httpx.Response(400, json={"error": {"code": code, "message": "m"}})
        raise AssertionError(mode)
