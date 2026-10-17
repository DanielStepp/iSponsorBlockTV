"""Web UI settings from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("iSponsorBlockTV-webui")

ENV_PORT = "iSPBTV_webui_port"
ENV_PASSWORD = "iSPBTV_webui_password"
ENV_HOST = "iSPBTV_webui_host"
DEFAULT_HOST = "0.0.0.0"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    password: str


def from_env(port: int | str | None = None) -> Settings | None:
    """The settings, or ``None`` if the web UI is not enabled or the port is invalid.

    ``port`` overrides the port from the environment (used by the ``webui`` command).
    """
    raw_port = port if port is not None else os.getenv(ENV_PORT)
    if not raw_port:
        return None
    try:
        port_number = int(raw_port)
        if not 0 < port_number < 65536:
            raise ValueError
    except ValueError:
        logger.error("Invalid web UI port %r, web UI disabled", raw_port)
        return None
    return Settings(
        host=os.getenv(ENV_HOST) or DEFAULT_HOST,
        port=port_number,
        password=os.getenv(ENV_PASSWORD) or "",
    )
