"""Web UI settings, read from environment variables.

* ``iSPBTV_webui_port``     - port of the web UI. Setting it enables the web UI.
* ``iSPBTV_webui_password`` - password (HTTP basic auth, any user name). Strongly
  recommended: the config contains the YouTube API key.
* ``iSPBTV_webui_host``     - address to listen on, default ``0.0.0.0``.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ENV_PORT = "iSPBTV_webui_port"
ENV_HOST = "iSPBTV_webui_host"
ENV_PASSWORD = "iSPBTV_webui_password"

DEFAULT_HOST = "0.0.0.0"
MAX_PORT = 65535


@dataclass(frozen=True)
class Settings:
    """Where the web UI listens and how it is protected."""

    host: str
    port: int
    password: str = ""


def settings_for_port(port: int) -> Settings:
    """Settings for ``port``; host and password come from the environment."""
    return Settings(
        host=os.getenv(ENV_HOST) or DEFAULT_HOST,
        port=port,
        password=os.getenv(ENV_PASSWORD) or "",
    )


def settings_from_env() -> Settings | None:
    """Settings from the environment, or ``None`` if the web UI is not enabled."""
    raw_port = os.getenv(ENV_PORT)
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if not 0 < port <= MAX_PORT:
        logger.error("Invalid %s %r, web UI disabled", ENV_PORT, raw_port)
        return None
    return settings_for_port(port)
