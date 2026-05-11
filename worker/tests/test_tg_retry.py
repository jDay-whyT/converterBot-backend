from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from main import _tg_retry


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=seconds)


class TgRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_value(self) -> None:
        fn = AsyncMock(return_value="ok")
        result = await _tg_retry(fn, "arg1", key="val")
        self.assertEqual(result, "ok")
        fn.assert_called_once_with("arg1", key="val")

    async def test_retries_once_then_succeeds(self) -> None:
        fn = AsyncMock(side_effect=[_retry_after(5), "ok"])
        with patch("main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await _tg_retry(fn, max_retries=3)
        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 2)
        mock_sleep.assert_awaited_once_with(6)  # retry_after + 1

    async def test_sleep_duration_is_retry_after_plus_one(self) -> None:
        fn = AsyncMock(side_effect=[_retry_after(10), _retry_after(3), "ok"])
        with patch("main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await _tg_retry(fn, max_retries=3)
        self.assertEqual(mock_sleep.await_count, 2)
        mock_sleep.assert_any_await(11)
        mock_sleep.assert_any_await(4)

    async def test_raises_after_max_retries_exceeded(self) -> None:
        fn = AsyncMock(side_effect=_retry_after(1))
        with patch("main.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(TelegramRetryAfter):
                await _tg_retry(fn, max_retries=2)
        self.assertEqual(fn.call_count, 3)  # initial + 2 retries

    async def test_multiple_retries_before_success(self) -> None:
        fn = AsyncMock(side_effect=[_retry_after(1), _retry_after(1), _retry_after(1), "ok"])
        with patch("main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await _tg_retry(fn, max_retries=3)
        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 4)
        self.assertEqual(mock_sleep.await_count, 3)

    async def test_non_rate_limit_exception_passes_through(self) -> None:
        fn = AsyncMock(side_effect=ValueError("boom"))
        with self.assertRaises(ValueError):
            await _tg_retry(fn)
        fn.assert_called_once()

    async def test_telegram_bad_request_passes_through(self) -> None:
        fn = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="chat not found"))
        with self.assertRaises(TelegramBadRequest):
            await _tg_retry(fn)
        fn.assert_called_once()

    async def test_default_max_retries_is_three(self) -> None:
        fn = AsyncMock(side_effect=_retry_after(0))
        with patch("main.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(TelegramRetryAfter):
                await _tg_retry(fn)
        self.assertEqual(fn.call_count, 4)  # initial + 3 retries


if __name__ == "__main__":
    unittest.main()
