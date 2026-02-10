from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class BatchProgress:
    chat_id: int
    topic_id: int
    user_id: int
    created_at: datetime
    total: int = 0
    processed: int = 0
    success: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    progress_message_id: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BatchRegistry:
    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self._batches: dict[tuple[int, int], deque[BatchProgress]] = defaultdict(deque)

    def get_or_create(self, chat_id: int, topic_id: int, user_id: int, now: datetime | None = None) -> BatchProgress:
        now = now or datetime.now(timezone.utc)
        key = (chat_id, user_id)
        queue = self._batches[key]
        while queue and now - queue[0].created_at > timedelta(seconds=self.window_seconds):
            queue.popleft()

        if queue and now - queue[-1].created_at <= timedelta(seconds=self.window_seconds):
            return queue[-1]

        batch = BatchProgress(chat_id=chat_id, topic_id=topic_id, user_id=user_id, created_at=now)
        queue.append(batch)
        return batch
