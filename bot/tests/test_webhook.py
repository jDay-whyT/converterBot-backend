import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings
from main import handle_telegram_webhook


def _make_settings(allowed_editors: list[int]) -> Settings:
    return Settings(
        bot_token="token",
        tg_webhook_secret="secret",
        allowed_editors=frozenset(allowed_editors),
        chat_id=-100,
        topic_source_id=10,
        gcp_project="proj",
        pubsub_topic="topic",
    )


def _make_request(payload: dict, settings: Settings, publisher: MagicMock) -> MagicMock:
    bot_app = MagicMock()
    bot_app.settings = settings

    request = MagicMock()
    request.app = {
        "bot_app": bot_app,
        "pubsub_publisher": publisher,
        "pubsub_topic_path": "projects/proj/topics/topic",
    }
    request.headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}
    request.json = AsyncMock(return_value=payload)
    return request


def _make_update(user_id: int, chat_id: int = -100, thread_id: int = 10) -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id},
            "message_id": 1,
            "message_thread_id": thread_id,
            "from": {"id": user_id},
            "document": {
                "file_id": "abc",
                "file_unique_id": "uabc",
                "mime_type": "image/heic",
                "file_name": "photo.heic",
            },
        },
    }


class WebhookAllowedEditorsTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_user_publishes(self) -> None:
        publisher = MagicMock()
        request = _make_request(_make_update(user_id=111), _make_settings([111]), publisher)
        with patch.dict(os.environ, {"TG_WEBHOOK_SECRET": "secret"}):
            response = await handle_telegram_webhook(request)
        self.assertEqual(response.status, 200)
        publisher.publish.assert_called_once()

    async def test_disallowed_user_skips(self) -> None:
        publisher = MagicMock()
        request = _make_request(_make_update(user_id=999), _make_settings([111]), publisher)
        with patch.dict(os.environ, {"TG_WEBHOOK_SECRET": "secret"}):
            response = await handle_telegram_webhook(request)
        self.assertEqual(response.status, 200)
        publisher.publish.assert_not_called()

    async def test_wrong_chat_skips(self) -> None:
        publisher = MagicMock()
        request = _make_request(_make_update(user_id=111, chat_id=-999), _make_settings([111]), publisher)
        with patch.dict(os.environ, {"TG_WEBHOOK_SECRET": "secret"}):
            response = await handle_telegram_webhook(request)
        self.assertEqual(response.status, 200)
        publisher.publish.assert_not_called()

    async def test_wrong_topic_skips(self) -> None:
        publisher = MagicMock()
        request = _make_request(_make_update(user_id=111, thread_id=99), _make_settings([111]), publisher)
        with patch.dict(os.environ, {"TG_WEBHOOK_SECRET": "secret"}):
            response = await handle_telegram_webhook(request)
        self.assertEqual(response.status, 200)
        publisher.publish.assert_not_called()

    async def test_invalid_secret_returns_401(self) -> None:
        publisher = MagicMock()
        request = _make_request(_make_update(user_id=111), _make_settings([111]), publisher)
        request.headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        with patch.dict(os.environ, {"TG_WEBHOOK_SECRET": "secret"}):
            response = await handle_telegram_webhook(request)
        self.assertEqual(response.status, 401)
        publisher.publish.assert_not_called()

    async def test_missing_update_id_returns_200_no_publish(self) -> None:
        publisher = MagicMock()
        request = _make_request({"message": {}}, _make_settings([111]), publisher)
        with patch.dict(os.environ, {"TG_WEBHOOK_SECRET": "secret"}):
            response = await handle_telegram_webhook(request)
        self.assertEqual(response.status, 200)
        publisher.publish.assert_not_called()


    async def test_photo_message_publishes_largest(self) -> None:
        publisher = MagicMock()
        update = {
            "update_id": 2,
            "message": {
                "chat": {"id": -100},
                "message_id": 2,
                "message_thread_id": 10,
                "from": {"id": 111},
                "photo": [
                    {"file_id": "small_id", "file_unique_id": "usmall"},
                    {"file_id": "large_id", "file_unique_id": "ularge"},
                ],
            },
        }
        request = _make_request(update, _make_settings([111]), publisher)
        with patch.dict(os.environ, {"TG_WEBHOOK_SECRET": "secret"}):
            response = await handle_telegram_webhook(request)
        self.assertEqual(response.status, 200)
        publisher.publish.assert_called_once()
        job = json.loads(publisher.publish.call_args[0][1].decode())
        self.assertEqual(job["file_id"], "large_id")
        self.assertEqual(job["file_unique_id"], "ularge")


if __name__ == "__main__":
    unittest.main()
