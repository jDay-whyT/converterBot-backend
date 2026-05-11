import os
import unittest
from unittest.mock import patch

from config import Settings, _parse_allowed, _required, load_settings


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
        self.assertEqual(_parse_allowed("123"), {123})

    def test_parse_allowed_multiple(self) -> None:
        self.assertEqual(_parse_allowed("111,222,333"), {111, 222, 333})

    def test_parse_allowed_with_spaces(self) -> None:
        self.assertEqual(_parse_allowed("111, 222 , 333"), {111, 222, 333})

    def test_parse_allowed_empty_items(self) -> None:
        self.assertEqual(_parse_allowed("111,,333"), {111, 333})

    def test_parse_allowed_pipe_separator(self) -> None:
        self.assertEqual(_parse_allowed("111|222|333"), {111, 222, 333})

    def test_parse_allowed_pipe_with_spaces(self) -> None:
        self.assertEqual(_parse_allowed("111 | 222 | 333"), {111, 222, 333})

    def test_parse_allowed_mixed_separators(self) -> None:
        self.assertEqual(_parse_allowed("111|222, 333 444"), {111, 222, 333, 444})

    def test_parse_allowed_trailing_separator(self) -> None:
        self.assertEqual(_parse_allowed("111|222|"), {111, 222})

    def test_load_settings_with_defaults(self) -> None:
        env = {
            "BOT_TOKEN": "test_token",
            "TG_WEBHOOK_SECRET": "webhook-secret",
            "ALLOWED_EDITORS": "111,222",
            "CHAT_ID": "-100123",
            "TOPIC_SOURCE_ID": "10",
            "GCP_PROJECT": "my-project",
            "PUBSUB_TOPIC": "my-topic",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
            self.assertEqual(settings.bot_token, "test_token")
            self.assertEqual(settings.tg_webhook_secret, "webhook-secret")
            self.assertEqual(settings.allowed_editors, {111, 222})
            self.assertEqual(settings.chat_id, -100123)
            self.assertEqual(settings.topic_source_id, 10)
            self.assertEqual(settings.gcp_project, "my-project")
            self.assertEqual(settings.pubsub_topic, "my-topic")
            self.assertFalse(settings.enable_webhook_setup)

    def test_load_settings_enable_webhook_setup(self) -> None:
        env = {
            "BOT_TOKEN": "token",
            "TG_WEBHOOK_SECRET": "secret",
            "ALLOWED_EDITORS": "999",
            "CHAT_ID": "-100999",
            "TOPIC_SOURCE_ID": "1",
            "GCP_PROJECT": "proj",
            "PUBSUB_TOPIC": "topic",
            "ENABLE_WEBHOOK_SETUP": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
            self.assertTrue(settings.enable_webhook_setup)

    def test_load_settings_empty_allowed_editors_raises(self) -> None:
        env = {
            "BOT_TOKEN": "token",
            "TG_WEBHOOK_SECRET": "secret",
            "ALLOWED_EDITORS": "   ",
            "CHAT_ID": "-100",
            "TOPIC_SOURCE_ID": "1",
            "GCP_PROJECT": "proj",
            "PUBSUB_TOPIC": "topic",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                load_settings()


if __name__ == "__main__":
    unittest.main()
