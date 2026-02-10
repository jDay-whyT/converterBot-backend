from datetime import datetime, timedelta, timezone
import unittest

from batching import BatchRegistry


class BatchRegistryTests(unittest.TestCase):
    def test_batch_registry_groups_within_window(self) -> None:
        registry = BatchRegistry(window_seconds=120)
        t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        first = registry.get_or_create(chat_id=1, topic_id=10, user_id=111, now=t0)
        second = registry.get_or_create(chat_id=1, topic_id=10, user_id=111, now=t0 + timedelta(seconds=100))
        self.assertIs(first, second)

    def test_batch_registry_creates_new_after_window(self) -> None:
        registry = BatchRegistry(window_seconds=120)
        t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        first = registry.get_or_create(chat_id=1, topic_id=10, user_id=111, now=t0)
        second = registry.get_or_create(chat_id=1, topic_id=10, user_id=111, now=t0 + timedelta(seconds=121))
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
