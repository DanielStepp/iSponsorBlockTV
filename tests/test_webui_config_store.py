"""Tests for the config validation and file handling of the web UI.

Run from the repository root with:  PYTHONPATH=src python -m unittest discover -s tests
"""

import json
import os
import tempfile
import unittest

from iSponsorBlockTV.webui.config_store import (
    ConfigError,
    ConfigFileError,
    ConfigStore,
    merge_config,
    public_config,
)


class MergeConfigTests(unittest.TestCase):
    def setUp(self):
        self.existing = {
            "devices": [{"screen_id": "abc", "name": "TV", "offset": 100, "extra": "keep"}],
            "apikey": "SECRET",
            "mute_ads": True,
            "future_option": {"nested": 1},
        }

    def test_keeps_unknown_keys_and_untouched_values(self):
        merged = merge_config(self.existing, {"skip_ads": True})
        self.assertEqual(merged["future_option"], {"nested": 1})
        self.assertIs(merged["mute_ads"], True)
        self.assertIs(merged["skip_ads"], True)

    def test_does_not_modify_the_input(self):
        merge_config(self.existing, {"mute_ads": False})
        self.assertIs(self.existing["mute_ads"], True)

    def test_rejects_non_dict_request(self):
        with self.assertRaises(ConfigError):
            merge_config(self.existing, ["not", "a", "dict"])

    def test_flags_must_be_booleans(self):
        for value in ("yes", 1, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                merge_config(self.existing, {"mute_ads": value})

    def test_minimum_skip_length(self):
        self.assertEqual(merge_config({}, {"minimum_skip_length": 2.5})["minimum_skip_length"], 2.5)
        for value in (-1, "3", True, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                merge_config({}, {"minimum_skip_length": value})

    def test_join_name_is_trimmed_and_limited(self):
        self.assertEqual(merge_config({}, {"join_name": "  TV  "})["join_name"], "TV")
        for value in ("", "   ", "x" * 65, 5):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                merge_config({}, {"join_name": value})

    def test_api_url_gets_a_trailing_slash(self):
        merged = merge_config({}, {"sponsorblock_api_url": " https://example.org/api "})
        self.assertEqual(merged["sponsorblock_api_url"], "https://example.org/api/")

    def test_api_url_must_be_http(self):
        for value in ("javascript:alert(1)", "ftp://example.org", "example.org", "", 5):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                merge_config({}, {"sponsorblock_api_url": value})

    def test_categories_are_checked_and_deduplicated(self):
        merged = merge_config({}, {"skip_categories": ["sponsor", "intro", "sponsor"]})
        self.assertEqual(merged["skip_categories"], ["sponsor", "intro"])
        for value in (["../x"], ["Sponsor"], [1], "sponsor"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                merge_config({}, {"skip_categories": value})

    def test_device_keeps_hidden_keys_and_updates_shown_ones(self):
        incoming = {"devices": [{"screen_id": "abc", "name": " New ", "offset": 250}]}
        device = merge_config(self.existing, incoming)["devices"][0]
        self.assertEqual(
            device, {"screen_id": "abc", "name": "New", "offset": 250, "extra": "keep"}
        )

    def test_new_device_gets_defaults(self):
        device = merge_config({}, {"devices": [{"screen_id": "xyz"}]})["devices"][0]
        self.assertEqual(device, {"screen_id": "xyz", "name": "", "offset": 0})

    def test_device_errors(self):
        invalid = [
            [{"screen_id": ""}],
            [{"screen_id": "   "}],
            [{"screen_id": 5}],
            [{"screen_id": "a", "offset": "10"}],
            [{"screen_id": "a"}, {"screen_id": "a"}],
            ["not a dict"],
            "not a list",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ConfigError):
                merge_config({}, {"devices": value})

    def test_whitelist_ignores_empty_rows(self):
        incoming = {"channel_whitelist": [{"id": "UC1", "name": "A"}, {"id": "  ", "name": ""}]}
        merged = merge_config(self.existing, incoming)
        self.assertEqual(merged["channel_whitelist"], [{"id": "UC1", "name": "A"}])

    def test_whitelist_needs_an_api_key(self):
        incoming = {"channel_whitelist": [{"id": "UC1"}], "clear_apikey": True}
        with self.assertRaises(ConfigError):
            merge_config(self.existing, incoming)

    def test_whitelist_entries_are_checked(self):
        for value in ([{"name": "no id"}], [{"id": 5}], ["x"], "x"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                merge_config(self.existing, {"channel_whitelist": value})

    def test_api_key_handling(self):
        self.assertEqual(merge_config(self.existing, {"apikey": ""})["apikey"], "SECRET")
        self.assertEqual(merge_config(self.existing, {"apikey": "  "})["apikey"], "SECRET")
        self.assertEqual(merge_config(self.existing, {"apikey": " NEW "})["apikey"], "NEW")
        self.assertEqual(merge_config(self.existing, {"clear_apikey": True})["apikey"], "")


class PublicConfigTests(unittest.TestCase):
    def test_never_contains_the_api_key(self):
        config = public_config({"apikey": "SECRET"})
        self.assertNotIn("SECRET", json.dumps(config))
        self.assertIs(config["apikey_set"], True)
        self.assertIs(public_config({})["apikey_set"], False)

    def test_fills_in_defaults(self):
        config = public_config({})
        self.assertEqual(config["skip_categories"], ["sponsor"])
        self.assertEqual(config["devices"], [])
        self.assertEqual(config["join_name"], "iSponsorBlockTV")
        self.assertIs(config["auto_play"], True)
        self.assertIs(config["mute_ads"], False)

    def test_only_shows_known_device_keys(self):
        raw = {"devices": [{"screen_id": "a", "name": "n", "offset": 5, "extra": 1}]}
        self.assertEqual(
            public_config(raw)["devices"], [{"screen_id": "a", "name": "n", "offset": 5}]
        )


class ConfigStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(self.directory, "config.json")

    def write_raw(self, text):
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(text)

    def read_json(self):
        with open(self.path, encoding="utf-8") as file:
            return json.load(file)

    def test_missing_file_is_an_empty_config(self):
        self.assertEqual(ConfigStore(self.path).read(), {})

    def test_broken_file_raises(self):
        for text in ("{not json", "[1, 2]"):
            with self.subTest(text=text):
                self.write_raw(text)
                with self.assertRaises(ConfigFileError):
                    ConfigStore(self.path).read()

    async def test_update_writes_and_keeps_other_keys(self):
        self.write_raw(json.dumps({"unknown": [1], "mute_ads": True}))
        merged = await ConfigStore(self.path).update({"mute_ads": False})
        on_disk = self.read_json()
        self.assertEqual(on_disk, merged)
        self.assertEqual(on_disk, {"unknown": [1], "mute_ads": False})

    async def test_update_leaves_no_temp_file(self):
        await ConfigStore(self.path).update({"skip_ads": True})
        self.assertEqual(os.listdir(self.directory), ["config.json"])

    async def test_invalid_update_does_not_touch_the_file(self):
        self.write_raw('{"mute_ads": true}')
        with self.assertRaises(ConfigError):
            await ConfigStore(self.path).update({"mute_ads": "nope"})
        self.assertEqual(self.read_json(), {"mute_ads": True})

    async def test_unwritable_location_raises_file_error(self):
        store = ConfigStore(os.path.join(self.directory, "missing-dir", "config.json"))
        with self.assertRaises(ConfigFileError):
            await store.update({"skip_ads": True})


if __name__ == "__main__":
    unittest.main()
