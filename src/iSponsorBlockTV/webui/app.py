"""The aiohttp application: routes and request handlers.

The page itself lives in ``static/``. Everything else is a small JSON API:

* ``GET  /api/config``  - the config and what the page needs to draw itself
* ``POST /api/config``  - validate and save changed settings
* ``POST /api/pair``    - turn a pairing code from the TV into a screen ID
* ``POST /api/restart`` - stop the program (Docker starts it again)
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from signal import SIGTERM
from typing import Any

from aiohttp import web

from .config_schema import ConfigError, category_options, to_public_config
from .config_store import ConfigFileError, ConfigStore
from .security import add_response_headers, csrf_middleware, password_middleware

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

MAX_REQUEST_BYTES = 1024 * 1024
PAIRING_CODE_PATTERN = re.compile(r"^[0-9 \-]{6,20}$")
RESTART_DELAY_SECONDS = 0.5  # lets the answer reach the browser before the program stops


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _read_json_object(request: web.Request) -> dict[str, Any]:
    """The JSON body of ``request``, which must be an object."""
    try:
        body = await request.json()
    except ValueError as error:
        raise ConfigError("Invalid JSON") from error
    if not isinstance(body, dict):
        raise ConfigError("Invalid request")
    return body


class ConfigApi:
    """The JSON API used by the page."""

    def __init__(
        self,
        store: ConfigStore,
        api_helper: Any,
        *,
        can_restart: bool,
        password_protected: bool,
    ) -> None:
        self._store = store
        self._api_helper = api_helper  # None if pairing is not possible
        self._can_restart = can_restart
        self._password_protected = password_protected

    async def get_config(self, request: web.Request) -> web.Response:
        try:
            stored = self._store.read()
        except ConfigFileError as error:
            return _error(str(error), 500)
        return web.json_response(
            {
                "config": to_public_config(stored),
                "categories": category_options(),
                "can_restart": self._can_restart,
                "can_pair": self._api_helper is not None,
                "password_protected": self._password_protected,
            }
        )

    async def save_config(self, request: web.Request) -> web.Response:
        try:
            merged = await self._store.update(await _read_json_object(request))
        except ConfigError as error:
            return _error(str(error), 400)
        except ConfigFileError as error:
            return _error(str(error), 500)
        return web.json_response({"ok": True, "config": to_public_config(merged)})

    async def pair_device(self, request: web.Request) -> web.Response:
        if self._api_helper is None:
            return _error("Pairing is not available", 400)
        try:
            code = str((await _read_json_object(request)).get("code", "")).strip()
        except ConfigError as error:
            return _error(str(error), 400)
        if not PAIRING_CODE_PATTERN.match(code):
            return _error("Invalid code", 400)
        try:
            screen = await self._api_helper.pair_with_code(code)
        except Exception:
            logger.exception("Pairing failed")
            return _error("Pairing failed", 502)
        if not screen:
            return _error("The code is invalid or has expired", 404)
        return web.json_response(
            {"screen_id": screen["screen_id"], "name": screen.get("name") or ""}
        )

    async def restart(self, request: web.Request) -> web.Response:
        if not self._can_restart:
            return _error("Restarting is not possible here", 400)
        logger.info("Restart requested via web UI")
        asyncio.get_running_loop().call_later(RESTART_DELAY_SECONDS, os.kill, os.getpid(), SIGTERM)
        return web.json_response({"ok": True})


async def _index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(INDEX_FILE)


def create_app(
    store: ConfigStore,
    api_helper: Any,
    *,
    can_restart: bool,
    password: str = "",
) -> web.Application:
    """Build the web application. An empty ``password`` means no authentication."""
    if not INDEX_FILE.is_file():
        raise FileNotFoundError(f"Web UI files are missing: {INDEX_FILE}")

    api = ConfigApi(store, api_helper, can_restart=can_restart, password_protected=bool(password))
    middlewares = [password_middleware(password)] if password else []
    middlewares.append(csrf_middleware)

    app = web.Application(middlewares=middlewares, client_max_size=MAX_REQUEST_BYTES)
    app.on_response_prepare.append(add_response_headers)
    app.add_routes(
        [
            web.get("/", _index),
            web.get("/api/config", api.get_config),
            web.post("/api/config", api.save_config),
            web.post("/api/pair", api.pair_device),
            web.post("/api/restart", api.restart),
        ]
    )
    app.router.add_static("/static/", STATIC_DIR)
    return app
