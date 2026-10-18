"""Reading and writing ``config.json`` for the web UI."""

import asyncio
import contextlib
import json
import logging
import os
import shutil
from typing import Any

from .config_schema import merge_config

logger = logging.getLogger(__name__)


class ConfigFileError(Exception):
    """``config.json`` could not be read or written. The message is shown to the user."""


class ConfigStore:
    """The config file on disk. Saves are serialized, so two requests cannot interleave."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def read(self) -> dict[str, Any]:
        """The stored config. Empty if the file does not exist yet."""
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:  # ValueError covers invalid JSON and encoding
            raise ConfigFileError(f"Could not read config.json: {error}") from error
        if not isinstance(data, dict):
            raise ConfigFileError("Could not read config.json: unexpected format")
        return data

    async def update(self, submitted: Any) -> dict[str, Any]:
        """Validate ``submitted``, merge it into the stored config and save the result.

        Raises ``ConfigError`` for invalid values and ``ConfigFileError`` for file problems.
        """
        async with self._lock:
            merged = merge_config(self.read(), submitted)
            self._write(merged)
        logger.info("Config saved via web UI")
        return merged

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self._write_atomically(data)
        except OSError:
            # For example when config.json is a bind-mounted file, which cannot be replaced
            self._remove_temp_file()
            try:
                self._dump(self.path, data)
            except OSError as error:
                raise ConfigFileError(f"Saving failed: {error}") from error

    def _write_atomically(self, data: dict[str, Any]) -> None:
        """Write to a temporary file and move it over the config, so it is never half-written."""
        self._dump(self._temp_path, data)
        with contextlib.suppress(OSError):  # keep the permissions of the existing file
            shutil.copymode(self.path, self._temp_path)
        os.replace(self._temp_path, self.path)

    @staticmethod
    def _dump(path: str, data: dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)

    @property
    def _temp_path(self) -> str:
        return self.path + ".tmp"

    def _remove_temp_file(self) -> None:
        with contextlib.suppress(OSError):
            os.remove(self._temp_path)
