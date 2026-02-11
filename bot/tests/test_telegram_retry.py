"""Tests for telegram_retry module."""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from telegram_retry import ProgressDebouncer, TelegramFileSemaphore, telegram_api_retry


class TelegramApiRetryTests(unittest.IsolatedAsyncioTestCase):
    """Tests for telegram_api_retry wrapper."""

    async def test_successful_call(self) -> None:
        """Test that successful calls return immediately."""
        mock_func = AsyncMock(return_value="success")
        result = await telegram_api_retry(mock_func, "arg1", kwarg1="kwarg1")

        self.assertEqual(result, "success")
        self.assertEqual(mock_func.call_count, 1)
        mock_func.assert_called_once_with("arg1", kwarg1="kwarg1")

    async def test_retry_after_with_retry(self) -> None:
        """Test TelegramRetryAfter is retried with proper sleep."""
        mock_func = AsyncMock()
        mock_method = MagicMock()
        mock_func.side_effect = [
            TelegramRetryAfter(method=mock_method, message="Flood control", retry_after=1),
            "success",
        ]

        start_time = time.perf_counter()
        result = await telegram_api_retry(mock_func, max_retries=2)
        elapsed = time.perf_counter() - start_time

        self.assertEqual(result, "success")
        self.assertEqual(mock_func.call_count, 2)
        # Should sleep retry_after + 1 = 2 seconds
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertLess(elapsed, 2.5)  # Some buffer for execution time

    async def test_retry_after_max_retries_exceeded(self) -> None:
        """Test that TelegramRetryAfter raises after max retries."""
        mock_func = AsyncMock()
        mock_method = MagicMock()
        mock_func.side_effect = TelegramRetryAfter(
            method=mock_method, message="Flood control", retry_after=0.1
        )

        with self.assertRaises(TelegramRetryAfter):
            await telegram_api_retry(mock_func, max_retries=2)

        # Should try 3 times total (initial + 2 retries)
        self.assertEqual(mock_func.call_count, 3)

    async def test_message_not_modified_ignored(self) -> None:
        """Test that 'message is not modified' errors are ignored when flag is set."""
        mock_func = AsyncMock()
        mock_method = MagicMock()
        mock_func.side_effect = TelegramBadRequest(
            method=mock_method, message="Bad Request: message is not modified"
        )

        result = await telegram_api_retry(mock_func, ignore_message_not_modified=True)

        self.assertIsNone(result)
        self.assertEqual(mock_func.call_count, 1)

    async def test_message_not_modified_not_ignored(self) -> None:
        """Test that 'message is not modified' errors raise when flag is False."""
        mock_func = AsyncMock()
        mock_method = MagicMock()
        mock_func.side_effect = TelegramBadRequest(
            method=mock_method, message="Bad Request: message is not modified"
        )

        with self.assertRaises(TelegramBadRequest):
            await telegram_api_retry(mock_func, ignore_message_not_modified=False)

        self.assertEqual(mock_func.call_count, 1)

    async def test_other_telegram_bad_request_raises(self) -> None:
        """Test that other TelegramBadRequest errors are not ignored."""
        mock_func = AsyncMock()
        mock_method = MagicMock()
        mock_func.side_effect = TelegramBadRequest(method=mock_method, message="Bad Request: chat not found")

        with self.assertRaises(TelegramBadRequest):
            await telegram_api_retry(mock_func, ignore_message_not_modified=True)

        self.assertEqual(mock_func.call_count, 1)

    async def test_other_exceptions_pass_through(self) -> None:
        """Test that other exceptions are not caught."""
        mock_func = AsyncMock()
        mock_func.side_effect = ValueError("Some error")

        with self.assertRaises(ValueError):
            await telegram_api_retry(mock_func)

        self.assertEqual(mock_func.call_count, 1)

    async def test_multiple_retries_before_success(self) -> None:
        """Test multiple TelegramRetryAfter before success."""
        mock_func = AsyncMock()
        mock_method = MagicMock()
        mock_func.side_effect = [
            TelegramRetryAfter(method=mock_method, message="Flood control 1", retry_after=0.1),
            TelegramRetryAfter(method=mock_method, message="Flood control 2", retry_after=0.1),
            "success",
        ]

        result = await telegram_api_retry(mock_func, max_retries=2)

        self.assertEqual(result, "success")
        self.assertEqual(mock_func.call_count, 3)


class TelegramFileSemaphoreTests(unittest.IsolatedAsyncioTestCase):
    """Tests for TelegramFileSemaphore rate limiter."""

    async def test_single_file_passes(self) -> None:
        """Test that single file operation passes through."""
        semaphore = TelegramFileSemaphore()

        async with semaphore:
            # If we get here, semaphore was acquired
            pass

        # Should be able to acquire again after release
        async with semaphore:
            pass

    async def test_sequential_access(self) -> None:
        """Test that multiple sequential accesses work."""
        semaphore = TelegramFileSemaphore()
        results = []

        for i in range(3):
            async with semaphore:
                results.append(i)
                await asyncio.sleep(0.01)

        self.assertEqual(results, [0, 1, 2])

    async def test_concurrent_access_serialized(self) -> None:
        """Test that concurrent access is serialized (one at a time)."""
        semaphore = TelegramFileSemaphore()
        results = []

        async def task(task_id: int) -> None:
            async with semaphore:
                results.append(f"start_{task_id}")
                await asyncio.sleep(0.05)
                results.append(f"end_{task_id}")

        await asyncio.gather(task(1), task(2), task(3))

        # Should be interleaved as start_X, end_X, start_Y, end_Y...
        # Not start_1, start_2, end_1, end_2 (which would indicate concurrency)
        self.assertEqual(len(results), 6)
        # Check that each task completes before next starts
        for i in range(0, 6, 2):
            task_num = results[i].split("_")[1]
            self.assertEqual(results[i + 1], f"end_{task_num}")


class ProgressDebouncerTests(unittest.TestCase):
    """Tests for ProgressDebouncer."""

    def test_first_update_allowed(self) -> None:
        """Test that first update (processed=1) is always allowed."""
        debouncer = ProgressDebouncer(min_interval=10.0, min_files=10)

        should_update = debouncer.should_update(
            batch_id=1,
            processed=1,
            total=100,
            has_error=False,
            current_time=0.0,
        )

        self.assertTrue(should_update)

    def test_final_update_allowed(self) -> None:
        """Test that final update (processed=total) is always allowed."""
        debouncer = ProgressDebouncer(min_interval=10.0, min_files=10)

        # First update
        debouncer.should_update(1, 1, 100, False, 0.0)

        # Final update (should be allowed even if time/files threshold not met)
        should_update = debouncer.should_update(
            batch_id=1,
            processed=100,
            total=100,
            has_error=False,
            current_time=0.5,
        )

        self.assertTrue(should_update)

    def test_error_update_allowed(self) -> None:
        """Test that error updates are always allowed."""
        debouncer = ProgressDebouncer(min_interval=10.0, min_files=10)

        # First update
        debouncer.should_update(1, 1, 100, False, 0.0)

        # Error update (should be allowed even if time/files threshold not met)
        should_update = debouncer.should_update(
            batch_id=1,
            processed=5,
            total=100,
            has_error=True,
            current_time=0.5,
        )

        self.assertTrue(should_update)

    def test_time_based_debouncing(self) -> None:
        """Test that updates are debounced based on time interval."""
        debouncer = ProgressDebouncer(min_interval=3.0, min_files=10)

        # First update
        debouncer.should_update(1, 1, 100, False, 0.0)

        # Too soon (1 second later)
        should_update = debouncer.should_update(1, 2, 100, False, 1.0)
        self.assertFalse(should_update)

        # Still too soon (2.5 seconds later)
        should_update = debouncer.should_update(1, 3, 100, False, 2.5)
        self.assertFalse(should_update)

        # Enough time passed (3.5 seconds later)
        should_update = debouncer.should_update(1, 4, 100, False, 3.5)
        self.assertTrue(should_update)

    def test_file_count_based_debouncing(self) -> None:
        """Test that updates are debounced based on file count."""
        debouncer = ProgressDebouncer(min_interval=10.0, min_files=3)

        # First update at processed=1
        debouncer.should_update(1, 1, 100, False, 0.0)

        # Not enough files processed
        should_update = debouncer.should_update(1, 2, 100, False, 0.1)
        self.assertFalse(should_update)

        should_update = debouncer.should_update(1, 3, 100, False, 0.2)
        self.assertFalse(should_update)

        # Enough files processed (3 since last update: 1 -> 4)
        should_update = debouncer.should_update(1, 4, 100, False, 0.3)
        self.assertTrue(should_update)

    def test_multiple_batches_independent(self) -> None:
        """Test that different batches are debounced independently."""
        debouncer = ProgressDebouncer(min_interval=3.0, min_files=10)

        # Batch 1
        should_update_1 = debouncer.should_update(1, 1, 100, False, 0.0)
        self.assertTrue(should_update_1)

        # Batch 2 (different batch ID)
        should_update_2 = debouncer.should_update(2, 1, 100, False, 0.5)
        self.assertTrue(should_update_2)  # First update for batch 2

        # Batch 1 still debounced
        should_update_1 = debouncer.should_update(1, 5, 100, False, 1.0)
        self.assertFalse(should_update_1)

    def test_reset_clears_state(self) -> None:
        """Test that reset clears debouncer state for a batch."""
        debouncer = ProgressDebouncer(min_interval=10.0, min_files=10)

        # First update
        debouncer.should_update(1, 1, 100, False, 0.0)

        # Reset
        debouncer.reset(1)

        # After reset, should allow update immediately (treated as new batch)
        should_update = debouncer.should_update(1, 1, 100, False, 0.5)
        self.assertTrue(should_update)

    def test_no_spam_scenario(self) -> None:
        """Test that rapid progress updates are properly debounced."""
        debouncer = ProgressDebouncer(min_interval=3.0, min_files=3)

        updates_sent = []

        # Simulate processing 20 files rapidly (0.1s each)
        for i in range(1, 21):
            current_time = i * 0.1
            should_update = debouncer.should_update(
                batch_id=1,
                processed=i,
                total=20,
                has_error=False,
                current_time=current_time,
            )
            if should_update:
                updates_sent.append(i)

        # Should send:
        # - processed=1 (first)
        # - processed=4 (3 files since last)
        # - processed=7 (3 files since last)
        # - processed=10 (3 files since last)
        # - processed=13 (3 files since last)
        # - processed=16 (3 files since last)
        # - processed=19 (3 files since last)
        # - processed=20 (final)
        # Total: ~8 updates instead of 20

        self.assertLess(len(updates_sent), 10, f"Too many updates sent: {updates_sent}")
        self.assertIn(1, updates_sent, "First update should be sent")
        self.assertIn(20, updates_sent, "Final update should be sent")


if __name__ == "__main__":
    unittest.main()
