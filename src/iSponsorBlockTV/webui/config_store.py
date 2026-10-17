"""Validation, reading and writing of ``config.json`` for the web UI.

Nothing in here depends on aiohttp, so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from typing import Any

from ..constants import SponsorBlock_api

Validator = Callable[[Any], Any]

BOOL_FIELDS = ("skip_count_tracking", "mute_ads", "skip_ads", "auto_play", "use_proxy")
CATEGORY_RE = re.compile(r"^[a-z_]{1,40}$")
URL_RE = re.compile(r"^https?://\S+$")
MAX_JOIN_NAME_LENGTH = 64

# What the browser gets for settings that are missing from config.json.
PUBLIC_DEFAULTS: dict[str, Any] = {
    "skip_count_tracking": True,
    "mute_ads": False,
    "skip_ads": False,
    "minimum_skip_length": 1,
    "auto_play": True,
    "join_name": "iSponsorBlockTV",
    "use_proxy": False,
    "sponsorblock_api_url": SponsorBlock_api,
}


class ConfigError(ValueError):
    """The submitted config is invalid. The message is shown to the user."""


class ConfigFileError(Exception):
    """config.json could not be read or written."""


# --- validators -------------------------------------------------------------------------
# Each takes the submitted value and returns the cleaned value or raises ConfigError.


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bool_validator(key: str) -> Validator:
    def validate(value: Any) -> bool:
        if not isinstance(value, bool):
            raise ConfigError(f"'{key}' must be true or false")
        return value

    return validate


def _minimum_skip_length(value: Any) -> float:
    if not _is_number(value) or value < 0:
        raise ConfigError("Minimum segment length must be a number >= 0")
    return value


def _join_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > MAX_JOIN_NAME_LENGTH:
        raise ConfigError(
            f"The name shown on the TV must not be empty (max. {MAX_JOIN_NAME_LENGTH} characters)"
        )
    return name


def _sponsorblock_api_url(value: Any) -> str:
    url = value.strip() if isinstance(value, str) else ""
    if not URL_RE.match(url):
        raise ConfigError("The SponsorBlock API URL must start with http:// or https://")
    return url if url.endswith("/") else url + "/"


def _skip_categories(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(category, str) and CATEGORY_RE.match(category) for category in value
    ):
        raise ConfigError("Invalid categories")
    return list(dict.fromkeys(value))  # drop duplicates, keep the order


def _channel_whitelist(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ConfigError("Invalid channel whitelist")
    channels = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ConfigError("Invalid entry in the channel whitelist")
        channel_id = item["id"].strip()
        if channel_id:  # empty rows are ignored
            channels.append({"id": channel_id, "name": str(item.get("name", "")).strip()})
    return channels


def _devices(value: Any, existing: list[Any]) -> list[dict[str, Any]]:
    """Clean the device list. Keys the UI doesn't show are kept from ``existing``."""
    if not isinstance(value, list):
        raise ConfigError("Invalid device list")
    known = {d.get("screen_id"): d for d in existing if isinstance(d, dict)}
    devices = [_device(item, known) for item in value]
    screen_ids = [device["screen_id"] for device in devices]
    if len(screen_ids) != len(set(screen_ids)):
        raise ConfigError("A screen ID appears more than once")
    return devices


def _device(item: Any, known: dict[Any, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ConfigError("Invalid device")
    screen_id = item.get("screen_id")
    if not isinstance(screen_id, str) or not screen_id.strip():
        raise ConfigError("Every device needs a screen ID")
    screen_id = screen_id.strip()
    offset = item.get("offset", 0)
    if not _is_number(offset):
        raise ConfigError("The offset must be a number (in ms)")
    device = dict(known.get(screen_id, {}))
    device.update(screen_id=screen_id, name=str(item.get("name", "")).strip(), offset=offset)
    return device


VALIDATORS: dict[str, Validator] = {
    **{key: _bool_validator(key) for key in BOOL_FIELDS},
    "minimum_skip_length": _minimum_skip_length,
    "join_name": _join_name,
    "sponsorblock_api_url": _sponsorblock_api_url,
    "skip_categories": _skip_categories,
    "channel_whitelist": _channel_whitelist,
}


# --- merging and presenting -------------------------------------------------------------


def _apply_api_key(result: dict[str, Any], incoming: dict[str, Any]) -> None:
    """``clear_apikey`` removes the key, a missing or empty ``apikey`` keeps the current one."""
    api_key = incoming.get("apikey")
    if incoming.get("clear_apikey") is True:
        result["apikey"] = ""
    elif isinstance(api_key, str) and api_key.strip():
        result["apikey"] = api_key.strip()


def merge_config(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Validate ``incoming`` and merge it into ``existing``. Unknown keys are kept."""
    if not isinstance(incoming, dict):
        raise ConfigError("Invalid request")
    result = dict(existing)
    for key, validate in VALIDATORS.items():
        if key in incoming:
            result[key] = validate(incoming[key])
    if "devices" in incoming:
        result["devices"] = _devices(incoming["devices"], existing.get("devices", []))
    _apply_api_key(result, incoming)
    if result.get("channel_whitelist") and not result.get("apikey"):
        raise ConfigError("The channel whitelist needs a YouTube API key")
    return result


def public_config(raw: dict[str, Any]) -> dict[str, Any]:
    """The config as sent to the browser. It never contains the API key."""
    return {
        "devices": [
            {
                "screen_id": device.get("screen_id", ""),
                "name": device.get("name", ""),
                "offset": device.get("offset", 0),
            }
            for device in raw.get("devices", [])
            if isinstance(device, dict)
        ],
        "skip_categories": raw.get("skip_categories") or ["sponsor"],
        "channel_whitelist": [
            {"id": channel.get("id", ""), "name": channel.get("name", "")}
            for channel in raw.get("channel_whitelist", [])
            if isinstance(channel, dict)
        ],
        "apikey_set": bool(raw.get("apikey")),
        **{key: raw.get(key, default) for key, default in PUBLIC_DEFAULTS.items()},
    }


# --- file access ------------------------------------------------------------------------


class ConfigStore:
    """Reads and writes the config file. Writes are serialised and atomic."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def read(self) -> dict[str, Any]:
        """The raw config. A missing file counts as an empty config."""
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigFileError(f"Could not read config.json: {error}") from error
        if not isinstance(data, dict):
            raise ConfigFileError("Could not read config.json: unexpected format")
        return data

    def write(self, data: dict[str, Any]) -> None:
        try:
            self._write_atomically(data)
        except OSError as error:
            raise ConfigFileError(f"Saving failed: {error}") from error

    async def update(self, incoming: dict[str, Any]) -> dict[str, Any]:
        """Validate ``incoming``, merge it into the file and return the new config."""
        async with self._lock:
            merged = merge_config(self.read(), incoming)
            self.write(merged)
        return merged

    def _write_atomically(self, data: dict[str, Any]) -> None:
        temp_path = self.path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=4)
            os.replace(temp_path, self.path)
        except OSError:
            # e.g. the config file itself is a bind mount and can't be replaced
            with open(self.path, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=4)
            if os.path.exists(temp_path):
                os.remove(temp_path)
