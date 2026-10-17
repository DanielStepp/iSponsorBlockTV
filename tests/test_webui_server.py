"""Tests for the web UI server: authentication, routes, security headers and settings.

Run from the repository root with:  PYTHONPATH=src python -m unittest discover -s tests
"""

import asyncio
import base64
import json
import os
import tempfile
import unittest
from signal import SIGTERM
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from iSponsorBlockTV.webui import server, settings
from iSponsorBlockTV.webui.config_store import ConfigStore


def basic_auth(user, password):
    """Headers for HTTP basic auth."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


PASSWORD = "secret"
AUTH = basic_auth("anyone", PASSWORD)
POST_HEADERS = {"X-Requested-With": "iSponsorBlockTV"}
POST_AUTH = {**POST_HEADERS, **AUTH}
INITIAL_CONFIG = {
    "devices": [{"screen_id": "abc", "name": "TV", "offset": 100}],
    "apikey": "SECRET-KEY",
    "mute_ads": True,
    "future_option": 1,
}


class FakeApiHelper:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.codes = []

    async def pair_with_code(self, code):
        self.codes.append(code)
        if self.error:
            raise self.error
        return self.result


class WebUITestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = os.path.join(directory.name, "config.json")
        self.write_config(INITIAL_CONFIG)

    def write_config(self, config):
        self.write_config_text(json.dumps(config))

    def write_config_text(self, text):
        with open(self.config_path, "w", encoding="utf-8") as file:
            file.write(text)

    def read_config(self):
        with open(self.config_path, encoding="utf-8") as file:
            return json.load(file)

    async def make_client(self, password=PASSWORD, api_helper=None, can_restart=False):
        ui = server.WebUI(ConfigStore(self.config_path), api_helper, password, can_restart)
        client = TestClient(TestServer(ui.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client


class AuthenticationTests(WebUITestCase):
    async def test_password_is_required(self):
        client = await self.make_client()
        for path in ("/", "/api/config", "/static/style.css"):
            with self.subTest(path=path):
                response = await client.get(path)
                self.assertEqual(response.status, 401)
                self.assertIn("Basic", response.headers["WWW-Authenticate"])

    async def test_wrong_password_is_rejected(self):
        client = await self.make_client()
        response = await client.get("/api/config", headers=basic_auth("anyone", "wrong"))
        self.assertEqual(response.status, 401)

    async def test_any_user_name_works_with_the_right_password(self):
        client = await self.make_client()
        for user in ("admin", "", "x"):
            with self.subTest(user=user):
                response = await client.get("/api/config", headers=basic_auth(user, PASSWORD))
                self.assertEqual(response.status, 200)

    async def test_garbage_authorization_header_is_rejected(self):
        client = await self.make_client()
        response = await client.get("/api/config", headers={"Authorization": "Basic %%%"})
        self.assertEqual(response.status, 401)

    async def test_without_a_password_everything_is_open(self):
        client = await self.make_client(password="")
        response = await client.get("/api/config")
        self.assertEqual(response.status, 200)
        self.assertIs((await response.json())["password_protected"], False)

    async def test_reports_that_a_password_is_set(self):
        client = await self.make_client()
        response = await client.get("/api/config", headers=AUTH)
        self.assertIs((await response.json())["password_protected"], True)


class CsrfProtectionTests(WebUITestCase):
    async def test_post_needs_the_custom_header(self):
        client = await self.make_client()
        response = await client.post("/api/config", json={"skip_ads": True}, headers=AUTH)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.read_config(), INITIAL_CONFIG)

    async def test_post_needs_a_json_content_type(self):
        client = await self.make_client()
        response = await client.post("/api/config", data="skip_ads=1", headers=POST_AUTH)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.read_config(), INITIAL_CONFIG)


class ConfigRouteTests(WebUITestCase):
    async def test_get_config_hides_the_api_key(self):
        client = await self.make_client()
        response = await client.get("/api/config", headers=AUTH)
        body = await response.text()
        self.assertNotIn("SECRET-KEY", body)
        data = json.loads(body)
        self.assertIs(data["config"]["apikey_set"], True)
        self.assertEqual(data["config"]["devices"][0]["screen_id"], "abc")
        self.assertIn({"label": "Sponsor", "id": "sponsor"}, data["categories"])

    async def test_post_saves_and_keeps_unknown_keys(self):
        client = await self.make_client()
        response = await client.post(
            "/api/config", json={"skip_ads": True, "mute_ads": False}, headers=POST_AUTH
        )
        self.assertEqual(response.status, 200)
        saved = self.read_config()
        self.assertEqual(saved["future_option"], 1)
        self.assertEqual(saved["apikey"], "SECRET-KEY")
        self.assertIs(saved["skip_ads"], True)
        self.assertIs(saved["mute_ads"], False)

    async def test_invalid_config_is_rejected_and_not_saved(self):
        client = await self.make_client()
        response = await client.post("/api/config", json={"mute_ads": "yes"}, headers=POST_AUTH)
        self.assertEqual(response.status, 400)
        self.assertIn("mute_ads", (await response.json())["error"])
        self.assertEqual(self.read_config(), INITIAL_CONFIG)

    async def test_invalid_json_is_rejected(self):
        client = await self.make_client()
        response = await client.post(
            "/api/config",
            data="{oops",
            headers={**POST_AUTH, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 400)

    async def test_unreadable_config_file_is_a_server_error(self):
        self.write_config_text("{broken")
        client = await self.make_client()
        response = await client.get("/api/config", headers=AUTH)
        self.assertEqual(response.status, 500)
        self.assertIn("config.json", (await response.json())["error"])


class StaticFilesTests(WebUITestCase):
    async def test_index_and_assets_are_served(self):
        client = await self.make_client()
        expectations = {
            "/": "<title>iSponsorBlockTV",
            "/static/style.css": "--accent",
            "/static/js/app.js": "loadConfig",
        }
        for path, expected in expectations.items():
            with self.subTest(path=path):
                response = await client.get(path, headers=AUTH)
                self.assertEqual(response.status, 200)
                self.assertIn(expected, await response.text())

    async def test_javascript_has_a_javascript_content_type(self):
        client = await self.make_client()
        response = await client.get("/static/js/app.js", headers=AUTH)
        self.assertIn("javascript", response.headers["Content-Type"])

    async def test_cannot_read_files_outside_the_static_folder(self):
        client = await self.make_client()
        for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py"):
            with self.subTest(path=path):
                response = await client.get(path, headers=AUTH)
                self.assertIn(response.status, (403, 404))
                self.assertNotIn("class WebUI", await response.text())

    async def test_security_headers(self):
        client = await self.make_client()
        page = await client.get("/", headers=AUTH)
        self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", page.headers["Content-Security-Policy"])
        self.assertEqual(page.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        api = await client.get("/api/config", headers=AUTH)
        self.assertEqual(api.headers["Cache-Control"], "no-store")


class PairingTests(WebUITestCase):
    async def pair(self, client, code):
        return await client.post("/api/pair", json={"code": code}, headers=POST_AUTH)

    async def test_pairs_with_a_code_and_never_returns_the_token(self):
        helper = FakeApiHelper({"screen_id": "new-id", "name": "Living room", "lounge_token": "T"})
        client = await self.make_client(api_helper=helper)
        response = await self.pair(client, "123 456 789")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"screen_id": "new-id", "name": "Living room"})
        self.assertEqual(helper.codes, ["123 456 789"])

    async def test_rejects_malformed_codes_without_calling_youtube(self):
        helper = FakeApiHelper({"screen_id": "x"})
        client = await self.make_client(api_helper=helper)
        for code in ("abc", "1", "1" * 30, "12345; drop"):
            with self.subTest(code=code):
                self.assertEqual((await self.pair(client, code)).status, 400)
        self.assertEqual(helper.codes, [])

    async def test_unknown_code(self):
        client = await self.make_client(api_helper=FakeApiHelper(None))
        self.assertEqual((await self.pair(client, "123456789")).status, 404)

    async def test_youtube_errors_become_a_generic_failure(self):
        client = await self.make_client(api_helper=FakeApiHelper(error=RuntimeError("boom")))
        with self.assertLogs(server.logger, "ERROR"):
            response = await self.pair(client, "123456789")
        self.assertEqual(response.status, 502)
        self.assertNotIn("boom", await response.text())

    async def test_unavailable_without_an_api_helper(self):
        client = await self.make_client(api_helper=None)
        self.assertEqual((await self.pair(client, "123456789")).status, 400)
        data = await (await client.get("/api/config", headers=AUTH)).json()
        self.assertIs(data["can_pair"], False)


class RestartTests(WebUITestCase):
    async def test_not_available_when_the_program_is_not_running(self):
        client = await self.make_client(can_restart=False)
        response = await client.post("/api/restart", json={}, headers=POST_AUTH)
        self.assertEqual(response.status, 400)

    async def test_sends_sigterm_to_itself(self):
        client = await self.make_client(can_restart=True)
        with (
            mock.patch.object(server, "RESTART_DELAY_SECONDS", 0),
            mock.patch.object(server.os, "kill") as kill,
        ):
            response = await client.post("/api/restart", json={}, headers=POST_AUTH)
            self.assertEqual(response.status, 200)
            await asyncio.sleep(0.05)
        kill.assert_called_once_with(os.getpid(), SIGTERM)


class SettingsTests(unittest.TestCase):
    def from_env(self, env, port=None):
        with mock.patch.dict(os.environ, env, clear=True):
            return settings.from_env(port)

    def test_disabled_without_a_port(self):
        self.assertIsNone(self.from_env({}))
        self.assertIsNone(self.from_env({"iSPBTV_webui_port": ""}))

    def test_reads_the_environment(self):
        env = {
            "iSPBTV_webui_port": "1232",
            "iSPBTV_webui_password": "pw",
            "iSPBTV_webui_host": "127.0.0.1",
        }
        self.assertEqual(self.from_env(env), settings.Settings("127.0.0.1", 1232, "pw"))

    def test_defaults(self):
        self.assertEqual(
            self.from_env({"iSPBTV_webui_port": "80"}), settings.Settings("0.0.0.0", 80, "")
        )

    def test_invalid_ports_disable_the_web_ui(self):
        for port in ("abc", "0", "-5", "70000"):
            with self.subTest(port=port), self.assertLogs(settings.logger, "ERROR"):
                self.assertIsNone(self.from_env({"iSPBTV_webui_port": port}))

    def test_explicit_port_overrides_the_environment(self):
        result = self.from_env({"iSPBTV_webui_port": "1"}, port=8080)
        self.assertEqual(result.port, 8080)


if __name__ == "__main__":
    unittest.main()
