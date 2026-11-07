"""Local access guard: only the browser (or window) that the edge app opened
may use its UI, since the UI acts for the user (org tokens, starting
processes). The launcher uses it (a copy of ``tgzr.edge.nice_client.guard``: its UI can start processes).

* At launch, :class:`LaunchTokens` issues a random token that is valid for one
  use and a few minutes. The browser is opened on ``/?launch=<token>``. That
  URL can show up briefly in the browser's command line, which other local
  users can see, but once the browser has used the token (or it has expired)
  it is worthless.
* The launch request gets an HttpOnly, SameSite=Strict, host-only cookie
  holding a separate random secret, and a tiny page that moves to the same URL
  without the token. That second navigation starts on 127.0.0.1 itself, so the
  SameSite=Strict cookie is sent with it.
* Every HTTP request and WebSocket handshake needs that cookie and an allowed
  ``Host`` header (DNS rebinding); anything else gets 403.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from typing import Any

LAUNCH_TTL = 300.0  # seconds


class LaunchTokens:
    """Single-use, short-lived launch tokens (shared by the app and its guard)."""

    def __init__(self) -> None:
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, ttl: float = LAUNCH_TTL) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = time.monotonic() + ttl
        return token

    def consume(self, token: str) -> bool:
        now = time.monotonic()
        with self._lock:
            for t, expires in list(self._tokens.items()):
                if expires < now:
                    del self._tokens[t]
            for t in list(self._tokens):
                if secrets.compare_digest(t, token):
                    del self._tokens[t]
                    return True
        return False


class LocalAccessGuard:
    def __init__(
        self,
        app: Any,
        *,
        launch_tokens: LaunchTokens,
        allowed_hosts: set[str],
        cookie_name: str,
        app_name: str = "TGZR Home",
        relaunch_hint: str = "restart TGZR Home",
    ) -> None:
        self.app = app
        self.app_name = app_name
        self.relaunch_hint = relaunch_hint
        self.launch_tokens = launch_tokens
        self.allowed_hosts = allowed_hosts
        self.cookie_name = cookie_name
        self.secret = secrets.token_urlsafe(32)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers: dict[str, list[str]] = {}
        for k, v in scope.get("headers", []):
            headers.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))
        host = (headers.get("host") or [""])[0]
        if host not in self.allowed_hosts:
            await self._deny(scope, receive, send, "unexpected Host header")
            return
        jar = SimpleCookie()
        try:
            jar.load("; ".join(headers.get("cookie", [])))
        except Exception:  # noqa: BLE001, S110
            pass
        morsel = jar.get(self.cookie_name)
        if morsel is not None and secrets.compare_digest(morsel.value, self.secret):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
            launch = (query.pop("launch", None) or [""])[0]
            if launch and self.launch_tokens.consume(launch):
                await self._accept_launch(scope, query, send)
                return
        await self._deny(
            scope, receive, send, f"not opened by {self.app_name} (launch links work once; {self.relaunch_hint})"
        )

    async def _accept_launch(self, scope: dict, query: dict[str, list[str]], send: Any) -> None:
        target = scope.get("path", "/") or "/"
        if query:
            target += "?" + urllib.parse.urlencode(query, doseq=True)
        # Escape "<" so the target can never close the <script> element.
        script_target = json.dumps(target).replace("<", "\\u003c")
        body = (
            f"<!doctype html><meta charset=utf-8><title>{html.escape(self.app_name)}</title>"
            f"<script>location.replace({script_target})</script>"
        ).encode()
        cookie = f"{self.cookie_name}={self.secret}; HttpOnly; SameSite=Strict; Path=/"
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"set-cookie", cookie.encode()),
                (b"cache-control", b"no-store"),
                (b"referrer-policy", b"no-referrer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _deny(self, scope: dict, receive: Any, send: Any, reason: str) -> None:
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        body = f"403 Forbidden: {reason}\n".encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
