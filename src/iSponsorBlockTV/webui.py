"""Small web UI to edit the config file.

Enabled by setting the environment variable ``iSPBTV_webui_port``. Optional:

* ``iSPBTV_webui_password``: HTTP basic auth password (any user name). Strongly
  recommended, the config contains the YouTube API key.
* ``iSPBTV_webui_host``: address to bind to (default ``0.0.0.0``).

Changes are written to ``config.json`` and take effect after a restart.
"""

import asyncio
import base64
import hmac
import json
import logging
import os
import re
from signal import SIGTERM
from typing import Any, Optional

from aiohttp import ClientSession, web

from .constants import SponsorBlock_api, skip_categories

logger = logging.getLogger("iSponsorBlockTV-webui")

BOOL_FIELDS = ("skip_count_tracking", "mute_ads", "skip_ads", "auto_play", "use_proxy")
CATEGORY_RE = re.compile(r"^[a-z_]{1,40}$")
CSRF_HEADER = ("X-Requested-With", "iSponsorBlockTV")


class ConfigError(ValueError):
    """The submitted config is invalid (message is shown to the user)."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def merge_config(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Validate ``incoming`` and merge it into ``existing`` (unknown keys are kept)."""
    if not isinstance(incoming, dict):
        raise ConfigError("Ungültige Anfrage")
    result = dict(existing)

    for key in BOOL_FIELDS:
        if key in incoming:
            if not isinstance(incoming[key], bool):
                raise ConfigError(f"'{key}' muss an oder aus sein")
            result[key] = incoming[key]

    if "minimum_skip_length" in incoming:
        value = incoming["minimum_skip_length"]
        if not _is_number(value) or value < 0:
            raise ConfigError("Minimale Länge muss eine Zahl >= 0 sein")
        result["minimum_skip_length"] = value

    if "join_name" in incoming:
        value = incoming["join_name"]
        if not isinstance(value, str) or not value.strip() or len(value) > 64:
            raise ConfigError("Name im Fernseher darf nicht leer sein (max. 64 Zeichen)")
        result["join_name"] = value.strip()

    if "sponsorblock_api_url" in incoming:
        value = incoming["sponsorblock_api_url"]
        if not isinstance(value, str) or not re.match(r"^https?://\S+$", value.strip()):
            raise ConfigError("SponsorBlock-API-URL muss mit http:// oder https:// beginnen")
        value = value.strip()
        result["sponsorblock_api_url"] = value if value.endswith("/") else value + "/"

    if "skip_categories" in incoming:
        value = incoming["skip_categories"]
        if not isinstance(value, list) or not all(
            isinstance(c, str) and CATEGORY_RE.match(c) for c in value
        ):
            raise ConfigError("Ungültige Kategorien")
        result["skip_categories"] = list(dict.fromkeys(value))

    # API key: missing/None keeps the current key, "" with clear_apikey removes it
    if incoming.get("clear_apikey") is True:
        result["apikey"] = ""
    elif isinstance(incoming.get("apikey"), str) and incoming["apikey"].strip():
        result["apikey"] = incoming["apikey"].strip()

    if "channel_whitelist" in incoming:
        value = incoming["channel_whitelist"]
        if not isinstance(value, list):
            raise ConfigError("Ungültige Kanal-Whitelist")
        channels = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ConfigError("Ungültiger Eintrag in der Kanal-Whitelist")
            if not item["id"].strip():
                continue  # ignore empty rows
            channels.append({"id": item["id"].strip(), "name": str(item.get("name", "")).strip()})
        result["channel_whitelist"] = channels

    if "devices" in incoming:
        value = incoming["devices"]
        if not isinstance(value, list):
            raise ConfigError("Ungültige Geräteliste")
        old_by_id = {
            d.get("screen_id"): d for d in existing.get("devices", []) if isinstance(d, dict)
        }
        devices = []
        for item in value:
            if not isinstance(item, dict):
                raise ConfigError("Ungültiges Gerät")
            screen_id = item.get("screen_id")
            if not isinstance(screen_id, str) or not screen_id.strip():
                raise ConfigError("Jedes Gerät braucht eine Screen-ID")
            screen_id = screen_id.strip()
            offset = item.get("offset", 0)
            if not _is_number(offset):
                raise ConfigError("Der Offset muss eine Zahl (in ms) sein")
            device = dict(old_by_id.get(screen_id, {}))  # keep keys we don't show
            device.update(
                {"screen_id": screen_id, "name": str(item.get("name", "")).strip(), "offset": offset}
            )
            devices.append(device)
        ids = [d["screen_id"] for d in devices]
        if len(ids) != len(set(ids)):
            raise ConfigError("Eine Screen-ID ist doppelt vorhanden")
        result["devices"] = devices

    if result.get("channel_whitelist") and not result.get("apikey"):
        raise ConfigError("Für die Kanal-Whitelist wird ein YouTube-API-Key benötigt")
    return result


def public_config(raw: dict[str, Any]) -> dict[str, Any]:
    """The config as sent to the browser (never includes the API key)."""
    return {
        "devices": [
            {
                "screen_id": d.get("screen_id", ""),
                "name": d.get("name", ""),
                "offset": d.get("offset", 0),
            }
            for d in raw.get("devices", [])
            if isinstance(d, dict)
        ],
        "skip_categories": raw.get("skip_categories") or ["sponsor"],
        "channel_whitelist": [
            {"id": c.get("id", ""), "name": c.get("name", "")}
            for c in raw.get("channel_whitelist", [])
            if isinstance(c, dict)
        ],
        "apikey_set": bool(raw.get("apikey")),
        "skip_count_tracking": raw.get("skip_count_tracking", True),
        "mute_ads": raw.get("mute_ads", False),
        "skip_ads": raw.get("skip_ads", False),
        "minimum_skip_length": raw.get("minimum_skip_length", 1),
        "auto_play": raw.get("auto_play", True),
        "join_name": raw.get("join_name", "iSponsorBlockTV"),
        "use_proxy": raw.get("use_proxy", False),
        "sponsorblock_api_url": raw.get("sponsorblock_api_url", SponsorBlock_api),
    }


class WebUI:
    def __init__(
        self,
        config_file: str,
        api_helper: Any = None,
        password: str = "",
        can_restart: bool = False,
    ) -> None:
        self.config_file = config_file
        self.api_helper = api_helper
        self.password = password
        self.can_restart = can_restart
        self._write_lock = asyncio.Lock()
        self.app = web.Application(middlewares=[self._security], client_max_size=1024 * 1024)
        self.app.add_routes(
            [
                web.get("/", self._index),
                web.get("/api/config", self._get_config),
                web.post("/api/config", self._post_config),
                web.post("/api/pair", self._pair),
                web.post("/api/restart", self._restart),
            ]
        )

    @web.middleware
    async def _security(self, request: web.Request, handler):
        if self.password:
            given = ""
            header = request.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    given = base64.b64decode(header[6:]).decode().partition(":")[2]
                except Exception:
                    given = ""
            if not hmac.compare_digest(given.encode(), self.password.encode()):
                return web.Response(
                    status=401,
                    text="Anmeldung erforderlich",
                    headers={"WWW-Authenticate": 'Basic realm="iSponsorBlockTV"'},
                )
        if request.method == "POST" and (
            request.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]
            or request.content_type != "application/json"
        ):
            return web.json_response({"error": "Ungültige Anfrage"}, status=403)
        return await handler(request)

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ConfigError("config.json hat ein unerwartetes Format")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.config_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp, self.config_file)
        except OSError:
            # e.g. the config file itself is a bind mount; fall back to writing in place
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            try:
                os.remove(tmp)
            except OSError:
                pass

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def _get_config(self, request: web.Request) -> web.Response:
        try:
            raw = self._read()
        except (ConfigError, json.JSONDecodeError) as e:
            return web.json_response({"error": f"config.json nicht lesbar: {e}"}, status=500)
        return web.json_response(
            {
                "config": public_config(raw),
                "categories": [{"label": label, "id": cid} for label, cid in skip_categories],
                "can_restart": self.can_restart,
                "can_pair": self.api_helper is not None,
                "password_protected": bool(self.password),
            }
        )

    async def _post_config(self, request: web.Request) -> web.Response:
        try:
            incoming = await request.json()
        except Exception:
            return web.json_response({"error": "Ungültiges JSON"}, status=400)
        async with self._write_lock:
            try:
                merged = merge_config(self._read(), incoming)
            except ConfigError as e:
                return web.json_response({"error": str(e)}, status=400)
            except json.JSONDecodeError as e:
                return web.json_response({"error": f"config.json nicht lesbar: {e}"}, status=500)
            try:
                self._write(merged)
            except OSError as e:
                logger.error("Could not write %s: %s", self.config_file, e)
                return web.json_response({"error": f"Speichern fehlgeschlagen: {e}"}, status=500)
        logger.info("Config saved via web UI")
        return web.json_response({"ok": True, "config": public_config(merged)})

    async def _pair(self, request: web.Request) -> web.Response:
        if self.api_helper is None:
            return web.json_response({"error": "Koppeln ist nicht verfügbar"}, status=400)
        try:
            code = str((await request.json()).get("code", "")).strip()
        except Exception:
            return web.json_response({"error": "Ungültiges JSON"}, status=400)
        if not re.match(r"^[0-9 \-]{6,20}$", code):
            return web.json_response({"error": "Ungültiger Code"}, status=400)
        try:
            result = await self.api_helper.pair_with_code(code)
        except Exception as e:
            logger.error("Pairing failed: %s", e)
            return web.json_response({"error": "Koppeln fehlgeschlagen"}, status=502)
        if not result:
            return web.json_response({"error": "Code nicht gültig oder abgelaufen"}, status=404)
        return web.json_response(
            {"screen_id": result["screen_id"], "name": result.get("name") or ""}
        )

    async def _restart(self, request: web.Request) -> web.Response:
        if not self.can_restart:
            return web.json_response({"error": "Neustart ist hier nicht möglich"}, status=400)
        logger.info("Restart requested via web UI")
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), SIGTERM)
        return web.json_response({"ok": True})


def settings_from_env() -> Optional[tuple[str, int, str]]:
    """(host, port, password) if the web UI is enabled via environment variables."""
    port = os.getenv("iSPBTV_webui_port")
    if not port:
        return None
    try:
        port_number = int(port)
        if not 0 < port_number < 65536:
            raise ValueError
    except ValueError:
        logger.error("Invalid iSPBTV_webui_port %r, web UI disabled", port)
        return None
    return (
        os.getenv("iSPBTV_webui_host") or "0.0.0.0",
        port_number,
        os.getenv("iSPBTV_webui_password") or "",
    )


async def start(
    config_file: str,
    api_helper: Any,
    can_restart: bool,
    host: str,
    port: int,
    password: str,
) -> web.AppRunner:
    ui = WebUI(config_file, api_helper, password, can_restart)
    runner = web.AppRunner(ui.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Web UI listening on http://%s:%d", host, port)
    if not password:
        logger.warning(
            "The web UI has no password. Anyone in your network can change the config. "
            "Set iSPBTV_webui_password to protect it."
        )
    return runner


async def start_from_env(config_file: str, api_helper: Any) -> Optional[web.AppRunner]:
    """Start the web UI next to the running program if it is enabled."""
    settings = settings_from_env()
    if settings is None:
        return None
    host, port, password = settings
    try:
        return await start(config_file, api_helper, True, host, port, password)
    except OSError as e:
        logger.error("Could not start the web UI on port %d: %s", port, e)
        return None


async def _run_standalone(config: Any, host: str, port: int, password: str) -> None:
    from . import api_helpers

    async with ClientSession(trust_env=getattr(config, "use_proxy", False)) as session:
        api_helper = api_helpers.ApiHelper(config, session)
        runner = await start(config.config_file, api_helper, False, host, port, password)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def run_standalone(config: Any, port: int) -> None:
    """Run only the web UI (no devices needed), e.g. for the first setup."""
    host = os.getenv("iSPBTV_webui_host") or "0.0.0.0"
    password = os.getenv("iSPBTV_webui_password") or ""
    try:
        asyncio.run(_run_standalone(config, host, port, password))
    except KeyboardInterrupt:
        pass


INDEX_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iSponsorBlockTV – Einstellungen</title>
<link rel="icon" href="data:,">
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1b1f24;--muted:#667085;--line:#e1e4e8;--accent:#2f6fed;--accent-text:#fff;--danger:#c62828;--ok:#2e7d32}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1e2127;--text:#e8eaed;--muted:#9aa3af;--line:#2f343c;--accent:#5b8def;--accent-text:#0d1117;--danger:#ef6b6b;--ok:#6fcf7a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 110px}
h1{font-size:1.35rem;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 18px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 14px}
h2{font-size:1rem;margin:0 0 10px}
label{display:block;font-weight:500;margin:10px 0 4px}
.hint{color:var(--muted);font-size:.85rem;margin:2px 0 0}
input[type=text],input[type=number],input[type=password],input[type=url]{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
input:focus,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.check{display:flex;align-items:center;gap:10px;margin:8px 0;font-weight:400}
.check input{width:18px;height:18px;margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:2px 14px}
.row{display:grid;grid-template-columns:1fr 1.4fr 90px auto;gap:8px;align-items:center;margin:6px 0}
.row.wl{grid-template-columns:1.4fr 1fr auto}
.row .lbl{display:none;font-size:.75rem;color:var(--muted)}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;padding:8px 12px;cursor:pointer}
button.primary{background:var(--accent);color:var(--accent-text);border-color:var(--accent);font-weight:600}
button.danger{color:var(--danger)}
button:disabled{opacity:.5;cursor:default}
.pair{display:flex;gap:8px;margin-top:10px}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 16px}
.bar div{max-width:760px;margin:0 auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#msg{flex:1;min-width:140px;font-size:.9rem}
#msg.err{color:var(--danger)}#msg.ok{color:var(--ok)}
.warn{background:#fff4e5;color:#7a4b00;border-radius:8px;padding:8px 10px;margin:0 0 14px;font-size:.9rem}
@media (prefers-color-scheme:dark){.warn{background:#3a2e17;color:#f3c98b}}
@media (max-width:560px){.row{grid-template-columns:1fr 1fr;}.row.wl{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<main>
<h1>iSponsorBlockTV</h1>
<p class="sub">Einstellungen. Änderungen gelten nach einem Neustart des Programms.</p>
<div id="warn" class="warn" hidden>Diese Seite ist nicht passwortgeschützt. Setze <code>iSPBTV_webui_password</code>, damit niemand im Netzwerk die Einstellungen ändern kann.</div>

<section>
<h2>Geräte</h2>
<div id="devices"></div>
<button id="addDevice" type="button">+ Gerät manuell hinzufügen</button>
<div class="pair" id="pairBox">
<input type="text" id="pairCode" inputmode="numeric" placeholder="Kopplungscode vom Fernseher" aria-label="Kopplungscode">
<button id="pairBtn" type="button">Koppeln</button>
</div>
<p class="hint">Den Code findest du im YouTube-App auf dem Fernseher unter Einstellungen &rarr; Mit Fernseher-Code verbinden. Der Offset wird in Millisekunden angegeben.</p>
</section>

<section>
<h2>Werbung</h2>
<label class="check"><input type="checkbox" id="skip_ads"> Werbung überspringen</label>
<label class="check"><input type="checkbox" id="mute_ads"> Ton bei Werbung stummschalten</label>
</section>

<section>
<h2>Segmente überspringen</h2>
<div class="grid" id="categories"></div>
<label for="minimum_skip_length">Minimale Länge eines Segments (Sekunden)</label>
<input type="number" id="minimum_skip_length" min="0" step="0.1">
</section>

<section>
<h2>Allgemein</h2>
<label class="check"><input type="checkbox" id="auto_play"> Autoplay einschalten</label>
<label class="check"><input type="checkbox" id="skip_count_tracking"> Übersprungene Segmente an SponsorBlock melden</label>
<label class="check"><input type="checkbox" id="use_proxy"> Proxy aus der Umgebung verwenden</label>
<label for="join_name">Name im Fernseher</label>
<input type="text" id="join_name" maxlength="64">
<label for="sponsorblock_api_url">SponsorBlock-API</label>
<input type="url" id="sponsorblock_api_url">
</section>

<section>
<h2>YouTube-API und Kanal-Whitelist</h2>
<label for="apikey">API-Key</label>
<input type="password" id="apikey" autocomplete="off">
<p class="hint" id="apikeyHint"></p>
<label class="check"><input type="checkbox" id="clear_apikey"> API-Key entfernen</label>
<label>Kanäle, bei denen nichts übersprungen wird</label>
<div id="whitelist"></div>
<button id="addChannel" type="button">+ Kanal hinzufügen</button>
</section>
</main>

<div class="bar"><div>
<span id="msg" role="status"></span>
<button id="save" class="primary" type="button">Speichern</button>
<button id="saveRestart" type="button" hidden>Speichern und neu starten</button>
</div></div>

<script>
const $ = (id) => document.getElementById(id);
const state = { devices: [], whitelist: [], categories: [] };

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids) el.append(kid);
  return el;
}

function say(text, kind = "") { const m = $("msg"); m.textContent = text; m.className = kind; }

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "iSponsorBlockTV" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("Fehler " + res.status));
  return data;
}

function field(value, placeholder, label, type = "text") {
  const input = h("input", { type, placeholder, "aria-label": label });
  input.value = value;
  return input;
}

function renderDevices() {
  const box = $("devices"); box.replaceChildren();
  state.devices.forEach((d, i) => {
    const name = field(d.name, "Name", "Name"); name.oninput = () => d.name = name.value;
    const sid = field(d.screen_id, "Screen-ID", "Screen-ID"); sid.oninput = () => d.screen_id = sid.value;
    const off = field(d.offset, "0", "Offset in ms", "number"); off.oninput = () => d.offset = Number(off.value) || 0;
    const del = h("button", { type: "button", class: "danger", "aria-label": "Gerät entfernen", onclick: () => { state.devices.splice(i, 1); renderDevices(); } }, "Entfernen");
    box.append(h("div", { class: "row" }, name, sid, off, del));
  });
  if (!state.devices.length) box.append(h("p", { class: "hint" }, "Noch kein Gerät eingetragen."));
}

function renderWhitelist() {
  const box = $("whitelist"); box.replaceChildren();
  state.whitelist.forEach((c, i) => {
    const id = field(c.id, "Kanal-ID", "Kanal-ID"); id.oninput = () => c.id = id.value;
    const name = field(c.name, "Name", "Kanalname"); name.oninput = () => c.name = name.value;
    const del = h("button", { type: "button", class: "danger", onclick: () => { state.whitelist.splice(i, 1); renderWhitelist(); } }, "Entfernen");
    box.append(h("div", { class: "row wl" }, id, name, del));
  });
}

function renderCategories(selected) {
  const box = $("categories"); box.replaceChildren();
  const known = new Set(state.categories.map((c) => c.id));
  const all = [...state.categories, ...selected.filter((id) => !known.has(id)).map((id) => ({ id, label: id }))];
  for (const c of all) {
    const cb = h("input", { type: "checkbox", value: c.id });
    cb.checked = selected.includes(c.id);
    box.append(h("label", { class: "check" }, cb, c.label));
  }
}

async function load() {
  try {
    const data = await api("/api/config");
    const c = data.config;
    state.categories = data.categories;
    state.devices = c.devices; state.whitelist = c.channel_whitelist;
    renderDevices(); renderWhitelist(); renderCategories(c.skip_categories);
    for (const k of ["skip_ads", "mute_ads", "auto_play", "skip_count_tracking", "use_proxy"]) $(k).checked = !!c[k];
    $("minimum_skip_length").value = c.minimum_skip_length;
    $("join_name").value = c.join_name; $("sponsorblock_api_url").value = c.sponsorblock_api_url;
    $("apikey").value = ""; $("clear_apikey").checked = false;
    $("apikeyHint").textContent = c.apikey_set ? "Ein Key ist gespeichert. Feld leer lassen, um ihn zu behalten." : "Kein Key gespeichert.";
    $("saveRestart").hidden = !data.can_restart;
    $("pairBox").hidden = !data.can_pair;
    $("warn").hidden = data.password_protected;
  } catch (e) { say(e.message, "err"); }
}

function collect() {
  return {
    devices: state.devices.map((d) => ({ screen_id: d.screen_id, name: d.name, offset: Number(d.offset) || 0 })),
    channel_whitelist: state.whitelist,
    skip_categories: [...document.querySelectorAll("#categories input:checked")].map((i) => i.value),
    skip_ads: $("skip_ads").checked, mute_ads: $("mute_ads").checked,
    auto_play: $("auto_play").checked, skip_count_tracking: $("skip_count_tracking").checked,
    use_proxy: $("use_proxy").checked,
    minimum_skip_length: Number($("minimum_skip_length").value),
    join_name: $("join_name").value, sponsorblock_api_url: $("sponsorblock_api_url").value,
    apikey: $("apikey").value, clear_apikey: $("clear_apikey").checked,
  };
}

async function save(restart) {
  const buttons = [$("save"), $("saveRestart")]; buttons.forEach((b) => b.disabled = true);
  try {
    await api("/api/config", collect());
    if (restart) {
      await api("/api/restart", {});
      say("Gespeichert. Das Programm startet neu …", "ok");
    } else {
      say("Gespeichert. Gilt nach dem nächsten Neustart.", "ok");
      await load();
    }
  } catch (e) { say(e.message, "err"); }
  buttons.forEach((b) => b.disabled = false);
}

$("save").onclick = () => save(false);
$("saveRestart").onclick = () => { if (confirm("Speichern und das Programm jetzt neu starten?")) save(true); };
$("addDevice").onclick = () => { state.devices.push({ screen_id: "", name: "", offset: 0 }); renderDevices(); };
$("addChannel").onclick = () => { state.whitelist.push({ id: "", name: "" }); renderWhitelist(); };
$("pairBtn").onclick = async () => {
  const code = $("pairCode").value.trim(); if (!code) return;
  $("pairBtn").disabled = true;
  try {
    const r = await api("/api/pair", { code });
    if (state.devices.some((d) => d.screen_id === r.screen_id)) say("Dieses Gerät ist schon eingetragen.", "err");
    else { state.devices.push({ screen_id: r.screen_id, name: r.name, offset: 0 }); renderDevices(); $("pairCode").value = ""; say("Gerät hinzugefügt. Jetzt speichern.", "ok"); }
  } catch (e) { say(e.message, "err"); }
  $("pairBtn").disabled = false;
};
load();
</script>
</body>
</html>
"""
