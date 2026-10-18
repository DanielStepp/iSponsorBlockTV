"""Starting and stopping the web UI server."""

import asyncio
import contextlib
import logging
from typing import Any

from aiohttp import ClientSession, web

from ..api_helpers import ApiHelper
from .app import create_app
from .config_store import ConfigStore
from .settings import Settings, settings_for_port, settings_from_env

logger = logging.getLogger(__name__)


async def _start(
    settings: Settings, config_file: str, api_helper: Any, *, can_restart: bool
) -> web.AppRunner:
    """Start listening. The caller must ``cleanup()`` the returned runner."""
    app = create_app(
        ConfigStore(config_file),
        api_helper,
        can_restart=can_restart,
        password=settings.password,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, settings.host, settings.port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info("Web UI listening on http://%s:%d", settings.host, settings.port)
    if not settings.password:
        logger.warning(
            "The web UI has no password. Anyone in your network can change the config. "
            "Set iSPBTV_webui_password to protect it."
        )
    return runner


async def start_from_env(config_file: str, api_helper: Any) -> web.AppRunner | None:
    """Start the web UI next to the running program, if it is enabled.

    Returns the runner (to ``cleanup()`` when the program ends), or ``None`` if the web UI
    is disabled or could not start. A failing web UI never stops the main program.
    """
    settings = settings_from_env()
    if settings is None:
        return None
    try:
        return await _start(settings, config_file, api_helper, can_restart=True)
    except OSError as error:
        logger.error("Could not start the web UI on port %d: %s", settings.port, error)
        return None


async def _serve_forever(config: Any, settings: Settings) -> None:
    async with ClientSession(trust_env=config.use_proxy) as session:
        runner = await _start(
            settings, config.config_file, ApiHelper(config, session), can_restart=False
        )
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def run_standalone(config: Any, port: int) -> None:
    """Run only the web UI, without any devices. Useful for the first setup."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve_forever(config, settings_for_port(port)))
