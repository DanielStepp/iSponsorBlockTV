"""Validation of the config submitted by the web UI.

``merge_config`` is the entry point: it validates the submitted values and merges them into
the config that is already stored. Keys the web UI does not know about are kept untouched.
``to_public_config`` is the opposite direction: the config as it is sent to the browser.
"""

import re
from collections.abc import Callable
from typing import Any

from ..constants import SponsorBlock_api
from ..constants import skip_categories as SKIP_CATEGORY_OPTIONS

# Settings that are plain on/off switches
FLAG_FIELDS = ("skip_count_tracking", "mute_ads", "skip_ads", "auto_play", "use_proxy")

CATEGORY_PATTERN = re.compile(r"^[a-z_]{1,40}$")
HTTP_URL_PATTERN = re.compile(r"^https?://\S+$")
MAX_JOIN_NAME_LENGTH = 64


class ConfigError(ValueError):
    """The submitted config is invalid. The message is shown to the user."""


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# --- Parsers: one per setting. Each returns the cleaned value or raises ConfigError. ---


def _parse_flag(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"'{name}' must be true or false")
    return value


def _parse_minimum_skip_length(value: Any) -> float:
    if not _is_number(value) or value < 0:
        raise ConfigError("Minimum segment length must be a number >= 0")
    return value


def _parse_join_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_JOIN_NAME_LENGTH:
        raise ConfigError(
            f"The name shown on the TV must not be empty (max. {MAX_JOIN_NAME_LENGTH} characters)"
        )
    return value.strip()


def _parse_api_url(value: Any) -> str:
    if not isinstance(value, str) or not HTTP_URL_PATTERN.match(value.strip()):
        raise ConfigError("The SponsorBlock API URL must start with http:// or https://")
    url = value.strip()
    return url if url.endswith("/") else url + "/"


def _parse_categories(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(category, str) and CATEGORY_PATTERN.match(category) for category in value
    ):
        raise ConfigError("Invalid categories")
    return list(dict.fromkeys(value))  # drop duplicates, keep the order


def _parse_channel_whitelist(value: Any) -> list[dict[str, str]]:
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


def _parse_device(item: Any, known_devices: dict[Any, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ConfigError("Invalid device")
    screen_id = item.get("screen_id")
    if not isinstance(screen_id, str) or not screen_id.strip():
        raise ConfigError("Every device needs a screen ID")
    screen_id = screen_id.strip()
    offset = item.get("offset", 0)
    if not _is_number(offset):
        raise ConfigError("The offset must be a number (in ms)")
    device = dict(known_devices.get(screen_id, {}))  # keep keys the web UI does not show
    device.update(screen_id=screen_id, name=str(item.get("name", "")).strip(), offset=offset)
    return device


def _parse_devices(value: Any, stored_devices: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ConfigError("Invalid device list")
    if not isinstance(stored_devices, list):
        stored_devices = []
    known_devices = {
        device.get("screen_id"): device for device in stored_devices if isinstance(device, dict)
    }
    devices = [_parse_device(item, known_devices) for item in value]
    screen_ids = [device["screen_id"] for device in devices]
    if len(screen_ids) != len(set(screen_ids)):
        raise ConfigError("A screen ID appears more than once")
    return devices


def _apply_api_key(merged: dict[str, Any], submitted: dict[str, Any]) -> None:
    """An empty or missing key keeps the stored one; ``clear_apikey`` removes it."""
    if submitted.get("clear_apikey") is True:
        merged["apikey"] = ""
        return
    api_key = submitted.get("apikey")
    if isinstance(api_key, str) and api_key.strip():
        merged["apikey"] = api_key.strip()


# Settings that only depend on their own value (devices and the API key are handled separately)
_FIELD_PARSERS: dict[str, Callable[[Any], Any]] = {
    "minimum_skip_length": _parse_minimum_skip_length,
    "join_name": _parse_join_name,
    "sponsorblock_api_url": _parse_api_url,
    "skip_categories": _parse_categories,
    "channel_whitelist": _parse_channel_whitelist,
}


def merge_config(stored: dict[str, Any], submitted: Any) -> dict[str, Any]:
    """Validate ``submitted`` and return ``stored`` updated with it.

    Only the keys present in ``submitted`` are changed.
    Raises ``ConfigError`` if a value is invalid.
    """
    if not isinstance(submitted, dict):
        raise ConfigError("Invalid request")
    merged = dict(stored)

    for name in FLAG_FIELDS:
        if name in submitted:
            merged[name] = _parse_flag(name, submitted[name])
    for name, parse in _FIELD_PARSERS.items():
        if name in submitted:
            merged[name] = parse(submitted[name])
    if "devices" in submitted:
        merged["devices"] = _parse_devices(submitted["devices"], stored.get("devices"))
    _apply_api_key(merged, submitted)

    if merged.get("channel_whitelist") and not merged.get("apikey"):
        raise ConfigError("The channel whitelist needs a YouTube API key")
    return merged


# --- The config as shown in the browser ---


def category_options() -> list[dict[str, str]]:
    """All SponsorBlock categories as ``{"label": ..., "id": ...}``."""
    return [{"label": label, "id": category} for label, category in SKIP_CATEGORY_OPTIONS]


def _public_device(device: dict[str, Any]) -> dict[str, Any]:
    return {
        "screen_id": device.get("screen_id", ""),
        "name": device.get("name", ""),
        "offset": device.get("offset", 0),
    }


def _public_channel(channel: dict[str, Any]) -> dict[str, Any]:
    return {"id": channel.get("id", ""), "name": channel.get("name", "")}


def to_public_config(stored: dict[str, Any]) -> dict[str, Any]:
    """The stored config as sent to the browser. The API key is never included."""
    return {
        "devices": [_public_device(d) for d in stored.get("devices", []) if isinstance(d, dict)],
        "skip_categories": stored.get("skip_categories") or ["sponsor"],
        "channel_whitelist": [
            _public_channel(c) for c in stored.get("channel_whitelist", []) if isinstance(c, dict)
        ],
        "apikey_set": bool(stored.get("apikey")),
        "skip_count_tracking": stored.get("skip_count_tracking", True),
        "mute_ads": stored.get("mute_ads", False),
        "skip_ads": stored.get("skip_ads", False),
        "minimum_skip_length": stored.get("minimum_skip_length", 1),
        "auto_play": stored.get("auto_play", True),
        "join_name": stored.get("join_name", "iSponsorBlockTV"),
        "use_proxy": stored.get("use_proxy", False),
        "sponsorblock_api_url": stored.get("sponsorblock_api_url", SponsorBlock_api),
    }
