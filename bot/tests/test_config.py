import os
import unittest
from unittest.mock import patch

from config import Settings, _parse_allowed, _required, load_settings, normalize_converter_url


class ConfigTests(unittest.TestCase):
    def test_required_raises_on_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                _required("MISSING_VAR")
            self.assertIn("Missing required environment variable: MISSING_VAR", str(ctx.exception))

    def test_required_returns_value(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": "test_value"}):
            self.assertEqual(_required("TEST_VAR"), "test_value")

    def test_parse_allowed_single(self) -> None:
        result = _parse_allowed("123")
        self.assertEqual(result, {123})

    def test_parse_allowed_multiple(self) -> None:
        result = _parse_allowed("111,222,333")
        self.assertEqual(result, {111, 222, 333})

    def test_parse_allowed_with_spaces(self) -> None:
        result = _parse_allowed("111, 222 , 333")
        self.assertEqual(result, {111, 222, 333})

    def test_parse_allowed_empty_items(self) -> None:
        result = _parse_allowed("111,,333")
        self.assertEqual(result, {111, 333})

    def test_parse_allowed_pipe_separator(self) -> None:
        result = _parse_allowed("111|222|333")
        self.assertEqual(result, {111, 222, 333})

    def test_parse_allowed_pipe_with_spaces(self) -> None:
        result = _parse_allowed("111 | 222 | 333")
        self.assertEqual(result, {111, 222, 333})

    def test_parse_allowed_mixed_separators(self) -> None:
        result = _parse_allowed("111|222, 333 444")
        self.assertEqual(result, {111, 222, 333, 444})

    def test_parse_allowed_trailing_separator(self) -> None:
        result = _parse_allowed("111|222|")
        self.assertEqual(result, {111, 222})

    def test_load_settings_with_defaults(self) -> None:
        env = {
            "BOT_TOKEN": "test_token",
            "ALLOWED_EDITORS": "111,222",
            "CHAT_ID": "-100123",
            "TOPIC_SOURCE_ID": "10",
            "TOPIC_CONVERTED_ID": "20",
            "CONVERTER_URL": "https://example.com/",
            "CONVERTER_API_KEY": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
            self.assertEqual(settings.bot_token, "test_token")
            self.assertEqual(settings.allowed_editors, {111, 222})
            self.assertEqual(settings.chat_id, -100123)
            self.assertEqual(settings.topic_source_id, 10)
            self.assertEqual(settings.topic_converted_id, 20)
            self.assertEqual(settings.converter_url, "https://example.com/convert")
            self.assertEqual(settings.converter_api_key, "secret")
            self.assertEqual(settings.max_file_mb, 40)
            self.assertEqual(settings.batch_window_seconds, 120)
            self.assertEqual(settings.progress_update_every, 3)

    def test_load_settings_custom_values(self) -> None:
        env = {
            "BOT_TOKEN": "token",
            "ALLOWED_EDITORS": "999",
            "CHAT_ID": "-100999",
            "TOPIC_SOURCE_ID": "1",
            "TOPIC_CONVERTED_ID": "2",
            "CONVERTER_URL": "http://localhost:8080",
            "CONVERTER_API_KEY": "key",
            "MAX_FILE_MB": "50",
            "BATCH_WINDOW_SECONDS": "60",
            "PROGRESS_UPDATE_EVERY": "5",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
            self.assertEqual(settings.max_file_mb, 50)
            self.assertEqual(settings.batch_window_seconds, 60)
            self.assertEqual(settings.progress_update_every, 5)

    def test_normalize_converter_url_adds_convert(self) -> None:
        self.assertEqual(normalize_converter_url("https://example.com"), "https://example.com/convert")

    def test_normalize_converter_url_keeps_convert_suffix(self) -> None:
        self.assertEqual(
            normalize_converter_url("https://example.com/convert"),
            "https://example.com/convert",
        )

    def test_normalize_converter_url_trims_and_deduplicates_convert(self) -> None:
        self.assertEqual(
            normalize_converter_url("  https://example.com/convert/convert/  "),
            "https://example.com/convert",
        )


if __name__ == "__main__":
    unittest.main()
