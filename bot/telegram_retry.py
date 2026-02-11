"""Retry wrapper and rate limiter for Telegram API calls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

T = TypeVar("T")


async def telegram_api_retry(
    func: Callable[..., Any],
    *args: Any,
    max_retries: int = 2,
    ignore_message_not_modified: bool = False,
    **kwargs: Any,
) -> T:
    """
    Universal retry wrapper for Telegram API calls.

    Handles:
    - TelegramRetryAfter (flood control): sleeps retry_after + 1 seconds and retries
    - TelegramBadRequest with "message is not modified": optionally ignores

    Args:
        func: Async function to call (e.g., bot.send_document)
        *args: Positional arguments for func
        max_retries: Maximum number of retry attempts for TelegramRetryAfter (default: 2)
        ignore_message_not_modified: If True, ignore "message is not modified" errors
        **kwargs: Keyword arguments for func

    Returns:
        Result of the function call

    Raises:
        TelegramRetryAfter: If max_retries exceeded
        TelegramBadRequest: If not ignored
        Other exceptions: Pass through
    """
    attempt = 0

    while True:
        try:
            return await func(*args, **kwargs)
        except TelegramRetryAfter as exc:
            attempt += 1
            if attempt > max_retries:
                logging.error(
                    "TelegramRetryAfter: max retries (%s) exceeded for %s",
                    max_retries,
                    func.__name__,
                )
                raise

            retry_after = exc.retry_after
            sleep_time = retry_after + 1
            logging.warning(
                "TelegramRetryAfter on %s (attempt %s/%s): sleeping %ss",
                func.__name__,
                attempt,
                max_retries,
                sleep_time,
            )
            await asyncio.sleep(sleep_time)
            # Continue to next iteration to retry
        except TelegramBadRequest as exc:
            if ignore_message_not_modified and "message is not modified" in str(exc).lower():
                logging.debug("Ignoring 'message is not modified' error on %s", func.__name__)
                return None  # type: ignore[return-value]
            raise


class TelegramFileSemaphore:
    """
    Rate limiter for sending files to Telegram.

    Ensures only one file is sent at a time to prevent flood control errors.
    """

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(1)

    async def __aenter__(self) -> TelegramFileSemaphore:
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._semaphore.release()


class ProgressDebouncer:
    """
    Debounces progress updates to avoid spamming edit_message_text.

    Updates are sent if:
    - It's the first update (processed == 1)
    - It's the final update (processed == total)
    - At least min_interval seconds have passed since last update
    - At least min_files files have been processed since last update
    - There's an error (for immediate error reporting)
    """

    def __init__(self, min_interval: float = 3.0, min_files: int = 3):
        """
        Initialize debouncer.

        Args:
            min_interval: Minimum seconds between updates (default: 3.0)
            min_files: Minimum files processed between updates (default: 3)
        """
        self.min_interval = min_interval
        self.min_files = min_files
        self._last_update_time: dict[int, float] = {}  # batch_id -> timestamp
        self._last_update_count: dict[int, int] = {}  # batch_id -> processed count

    def should_update(
        self,
        batch_id: int,
        processed: int,
        total: int,
        has_error: bool,
        current_time: float,
    ) -> bool:
        """
        Check if progress update should be sent.

        Args:
            batch_id: Unique batch identifier
            processed: Number of files processed
            total: Total number of files
            has_error: Whether this update is due to an error
            current_time: Current time from time.perf_counter()

        Returns:
            True if update should be sent
        """
        # Always update on first, last, or error
        if processed == 1 or processed == total or has_error:
            self._last_update_time[batch_id] = current_time
            self._last_update_count[batch_id] = processed
            return True

        last_time = self._last_update_time.get(batch_id, 0.0)
        last_count = self._last_update_count.get(batch_id, 0)

        time_elapsed = current_time - last_time
        files_since_update = processed - last_count

        should_update = time_elapsed >= self.min_interval or files_since_update >= self.min_files

        if should_update:
            self._last_update_time[batch_id] = current_time
            self._last_update_count[batch_id] = processed

        return should_update

    def reset(self, batch_id: int) -> None:
        """Reset debouncer state for a batch (call when batch completes)."""
        self._last_update_time.pop(batch_id, None)
        self._last_update_count.pop(batch_id, None)
