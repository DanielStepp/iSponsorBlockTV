"""Starting the web UI: next to the running program, or on its own."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import ClientSession, web

from .. import api_helpers
from .config_store import ConfigStore
from .server import WebUI
from .settings import Settings, from_env

logger = logging.getLogger("iSponsorBlockTV-webui")


async def _start(
    config_file: str, api_helper: Any, settings: Settings, can_restart: bool
) -> web.AppRunner:
    ui = WebUI(ConfigStore(config_file), api_helper, settings.password, can_restart)
    runner = web.AppRunner(ui.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.host, settings.port).start()
    logger.info("Web UI listening on http://%s:%d", settings.host, settings.port)
    if not settings.password:
        logger.warning(
            "The web UI has no password. Anyone in your network can change the config. "
            "Set iSPBTV_webui_password to protect it."
        )
    return runner


async def start_from_env(config_file: str, api_helper: Any) -> web.AppRunner | None:
    """Start the web UI inside the running program if it is enabled.

    Returns the runner (call ``cleanup()`` on it when done) or ``None`` if the web UI is
    disabled or could not be started.
    """
    settings = from_env()
    if settings is None:
        return None
    try:
        return await _start(config_file, api_helper, settings, can_restart=True)
    except OSError as error:
        logger.error("Could not start the web UI on port %d: %s", settings.port, error)
        return None


async def _serve_forever(config: Any, settings: Settings) -> None:
    async with ClientSession(trust_env=getattr(config, "use_proxy", False)) as session:
        api_helper = api_helpers.ApiHelper(config, session)
        runner = await _start(config.config_file, api_helper, settings, can_restart=False)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def run_standalone(config: Any, port: int) -> None:
    """Run only the web UI, without any devices. Useful for the first setup."""
    settings = from_env(port)
    if settings is None:
        return
    try:
        asyncio.run(_serve_forever(config, settings))
    except KeyboardInterrupt:
        pass
