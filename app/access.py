"""Optional browser-compatible read access for HTTP and WebSocket surfaces."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import secrets
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class PrivateAccessMiddleware:
    def __init__(self, app: ASGIApp, state: Any) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        settings = getattr(self.state, "settings", None)
        username = getattr(settings, "read_username", "")
        password = getattr(settings, "read_password", "")
        if not username and not password:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] in {"/api/health", "/api/ready"}:
            client = scope.get("client")
            try:
                local_probe = client is not None and ipaddress.ip_address(client[0]).is_loopback
            except ValueError:
                local_probe = False
            if local_probe:
                await self.app(scope, receive, send)
                return
        headers = dict(scope.get("headers", []))
        edit_token = getattr(settings, "edit_token", "")
        supplied_token = headers.get(b"x-edit-token", b"")
        if (
            scope["type"] == "http"
            and edit_token
            and secrets.compare_digest(supplied_token, edit_token.encode("utf-8"))
        ):
            await self.app(scope, receive, send)
            return
        authorization = headers.get(b"authorization", b"")
        supplied_user = supplied_password = b""
        try:
            scheme, encoded = authorization.split(b" ", 1)
            if scheme.lower() == b"basic":
                supplied_user, supplied_password = base64.b64decode(
                    encoded, validate=True
                ).split(b":", 1)
        except (ValueError, binascii.Error):
            pass
        user_matches = secrets.compare_digest(supplied_user, username.encode("utf-8"))
        password_matches = secrets.compare_digest(supplied_password, password.encode("utf-8"))
        if username and password and user_matches and password_matches:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            response = JSONResponse(
                {"detail": "read_credentials_required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Market Board", charset="UTF-8"'},
            )
            await response(scope, receive, send)
