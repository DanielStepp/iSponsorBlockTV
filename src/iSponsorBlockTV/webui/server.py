"""The aiohttp application: routes, authentication and security headers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from signal import SIGTERM
from typing import Any

from aiohttp import web

from ..constants import skip_categories
from .config_store import ConfigError, ConfigFileError, ConfigStore, public_config

logger = logging.getLogger("iSponsorBlockTV-webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 1024 * 1024
RESTART_DELAY_SECONDS = 0.5
PAIRING_CODE_RE = re.compile(r"^[0-9 \-]{6,20}$")

# Browsers send credentials automatically, so state-changing requests must carry a header
# that a foreign website can't add without a CORS preflight.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "iSponsorBlockTV"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

CATEGORY_CHOICES = [{"label": label, "id": category} for label, category in skip_categories]

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def error_response(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


# --- middleware and response hooks ------------------------------------------------------


def _password_from_request(request: web.Request) -> str:
    """The password of an HTTP basic auth header (the user name is ignored)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return ""
    try:
        return base64.b64decode(header[6:]).decode().partition(":")[2]
    except (binascii.Error, UnicodeDecodeError):
        return ""


def require_password(password: str):
    """Middleware that asks for the password. Without a password it lets everything in."""
    expected = password.encode()

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if expected and not hmac.compare_digest(_password_from_request(request).encode(), expected):
            return web.Response(
                status=401,
                text="Authentication required",
                headers={"WWW-Authenticate": 'Basic realm="iSponsorBlockTV"'},
            )
        return await handler(request)

    return middleware


@web.middleware
async def require_json_post(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Reject POST requests that don't look like they come from our own page."""
    if request.method == "POST" and (
        request.headers.get(CSRF_HEADER) != CSRF_VALUE or request.content_type != "application/json"
    ):
        return error_response("Invalid request", 403)
    return await handler(request)


async def add_response_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.update(SECURITY_HEADERS)
    response.headers["Cache-Control"] = (
        "no-store" if request.path.startswith("/api/") else "no-cache"
    )


# --- application ------------------------------------------------------------------------


class WebUI:
    def __init__(
        self,
        store: ConfigStore,
        api_helper: Any = None,
        password: str = "",
        can_restart: bool = False,
    ) -> None:
        self.store = store
        self.api_helper = api_helper
        self.can_restart = can_restart
        self.password_protected = bool(password)
        self.app = web.Application(
            middlewares=[require_password(password), require_json_post],
            client_max_size=MAX_REQUEST_BYTES,
        )
        self.app.on_response_prepare.append(add_response_headers)
        self.app.add_routes(
            [
                web.get("/", self._index),
                web.get("/api/config", self._get_config),
                web.post("/api/config", self._post_config),
                web.post("/api/pair", self._pair),
                web.post("/api/restart", self._restart),
            ]
        )
        self.app.router.add_static("/static", STATIC_DIR, follow_symlinks=False)

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _get_config(self, request: web.Request) -> web.Response:
        try:
            config = public_config(self.store.read())
        except ConfigFileError as error:
            return error_response(str(error), 500)
        return web.json_response(
            {
                "config": config,
                "categories": CATEGORY_CHOICES,
                "can_restart": self.can_restart,
                "can_pair": self.api_helper is not None,
                "password_protected": self.password_protected,
            }
        )

    async def _post_config(self, request: web.Request) -> web.Response:
        try:
            incoming = await request.json()
        except ValueError:
            return error_response("Invalid JSON", 400)
        try:
            merged = await self.store.update(incoming)
        except ConfigError as error:
            return error_response(str(error), 400)
        except ConfigFileError as error:
            logger.error("%s", error)
            return error_response(str(error), 500)
        logger.info("Config saved via web UI")
        return web.json_response({"ok": True, "config": public_config(merged)})

    async def _pair(self, request: web.Request) -> web.Response:
        if self.api_helper is None:
            return error_response("Pairing is not available", 400)
        try:
            body = await request.json()
        except ValueError:
            return error_response("Invalid JSON", 400)
        code = str(body.get("code", "")).strip() if isinstance(body, dict) else ""
        if not PAIRING_CODE_RE.match(code):
            return error_response("Invalid code", 400)
        try:
            result = await self.api_helper.pair_with_code(code)
        except Exception as error:  # noqa: BLE001 - any failure is shown as a generic one
            logger.error("Pairing failed: %s", error)
            return error_response("Pairing failed", 502)
        if not result:
            return error_response("The code is invalid or has expired", 404)
        return web.json_response(
            {"screen_id": result["screen_id"], "name": result.get("name") or ""}
        )

    async def _restart(self, request: web.Request) -> web.Response:
        if not self.can_restart:
            return error_response("Restarting is not possible here", 400)
        logger.info("Restart requested via web UI")
        # The program shuts down on SIGTERM; Docker's restart policy starts it again.
        asyncio.get_running_loop().call_later(RESTART_DELAY_SECONDS, os.kill, os.getpid(), SIGTERM)
        return web.json_response({"ok": True})
