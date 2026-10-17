"""Optional web UI to edit ``config.json``.

Enable it by setting ``iSPBTV_webui_port``. See the README for all options.
"""

from .launcher import run_standalone, start_from_env

__all__ = ["run_standalone", "start_from_env"]
