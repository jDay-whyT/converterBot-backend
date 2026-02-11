from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from pathlib import Path

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from batching import BatchProgress, BatchRegistry
from config import Settings, load_settings
from progress_utils import is_message_not_modified_error

SUPPORTED_EXTENSIONS = {".heic", ".dng", ".webp", ".tif", ".tiff"}


class ConverterClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.conversion_timeout_seconds)

    async def convert(self, path: Path, quality: int = 92, max_side: int | None = None) -> bytes:
        files = {"file": (path.name, path.read_bytes(), "application/octet-stream")}
        data: dict[str, str | int] = {"quality": quality}
        if max_side:
            data["max_side"] = max_side
        response = await self._client.post(
            f"{self.settings.converter_url}/convert",
            headers={"X-API-KEY": self.settings.converter_api_key},
            files=files,
            data=data,
        )
        response.raise_for_status()
        return response.content

    async def close(self) -> None:
        await self._client.aclose()


class ConversionBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot = Bot(token=settings.bot_token)
        self.dp = Dispatcher()
        self.registry = BatchRegistry(settings.batch_window_seconds)
        self.converter = ConverterClient(settings)
        self.semaphore = asyncio.Semaphore(2)
        self._bind_handlers()

    def _bind_handlers(self) -> None:
        self.dp.message.register(self._start, Command("start"))
        self.dp.message.register(self._handle_document, F.document)

    async def _start(self, message: Message) -> None:
        if not self._is_allowed_user(message):
            await message.answer("Нет доступа")
            return
        await message.answer("Готов конвертировать документы в JPG")

    def _is_allowed_user(self, message: Message) -> bool:
        user = message.from_user
        return bool(user and user.id in self.settings.allowed_editors)

    def _is_source_topic(self, message: Message) -> bool:
        return (
            message.chat.id == self.settings.chat_id
            and message.message_thread_id == self.settings.topic_source_id
        )

    async def _handle_document(self, message: Message) -> None:
        if not self._is_allowed_user(message):
            await message.answer("Нет доступа")
            return

        if not self._is_source_topic(message):
            return

        assert message.from_user and message.document
        document = message.document
        ext = Path(document.file_name or "").suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return

        size_limit = self.settings.max_file_mb * 1024 * 1024
        batch = self.registry.get_or_create(
            chat_id=message.chat.id,
            topic_id=message.message_thread_id or 0,
            user_id=message.from_user.id,
        )
        batch.total += 1

        if document.file_size and document.file_size > size_limit:
            await self._register_result(batch, False, document.file_name or "file", "слишком большой файл")
            return

        async with self.semaphore:
            try:
                jpg_bytes = await self._download_and_convert(message)
                target_name = f"{Path(document.file_name or 'file').stem}.jpg"
                await self.bot.send_document(
                    chat_id=self.settings.chat_id,
                    message_thread_id=self.settings.topic_converted_id,
                    document=BufferedInputFile(jpg_bytes, filename=target_name),
                )
                await self._register_result(batch, True, document.file_name or "file", None)
            except httpx.HTTPStatusError as exc:
                reason = exc.response.text[:120] if exc.response is not None else str(exc)
                await self._register_result(batch, False, document.file_name or "file", reason)
            except Exception as exc:  # noqa: BLE001
                await self._register_result(batch, False, document.file_name or "file", str(exc))

    async def _download_and_convert(self, message: Message) -> bytes:
        assert message.document
        with tempfile.TemporaryDirectory(prefix="tg-file-") as tmpdir:
            source = Path(tmpdir) / (message.document.file_name or "input.bin")
            file_info = await self.bot.get_file(message.document.file_id)
            await self.bot.download_file(file_info.file_path, destination=source)
            return await self.converter.convert(source, quality=self.settings.conversion_quality)

    async def _register_result(self, batch: BatchProgress, ok: bool, file_name: str, reason: str | None) -> None:
        async with batch.lock:
            batch.processed += 1
            if ok:
                batch.success += 1
            else:
                batch.failed += 1
                if reason:
                    batch.errors.append(f"ошибка на файле {file_name} ({reason})")

            needs_update = (
                batch.processed == 1
                or batch.processed == batch.total
                or batch.processed % max(1, self.settings.progress_update_every) == 0
                or (not ok)
            )
            if needs_update:
                await self._update_progress(batch)

    async def _update_progress(self, batch: BatchProgress) -> None:
        text_lines = [
            f"Пачка от user {batch.user_id}",
            f"Обработано: {batch.processed}/{batch.total}",
            f"Успешно: {batch.success}",
            f"Ошибок: {batch.failed}",
        ]
        if batch.errors:
            text_lines.extend(batch.errors[-5:])
        text = "\n".join(text_lines)

        if batch.progress_message_id is None:
            sent = await self.bot.send_message(
                chat_id=batch.chat_id,
                message_thread_id=batch.topic_id,
                text=text,
            )
            batch.progress_message_id = sent.message_id
            return

        try:
            await self.bot.edit_message_text(
                chat_id=batch.chat_id,
                message_id=batch.progress_message_id,
                text=text,
            )
        except TelegramBadRequest as exc:
            if not is_message_not_modified_error(exc):
                raise

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)


async def handle_root(request: web.Request) -> web.Response:
    """Health check endpoint for Cloud Run."""
    return web.Response(text="ok")


async def handle_healthz(request: web.Request) -> web.Response:
    """Detailed health check endpoint."""
    return web.json_response({"ok": True})


async def run_health_server(host: str, port: int) -> None:
    """Run HTTP health check server for Cloud Run."""
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_healthz)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logging.info(f"Health server listening on {host}:{port}")

    # Keep running indefinitely
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    app = ConversionBot(settings)

    # Cloud Run health server config
    health_host = "0.0.0.0"
    health_port = int(os.getenv("PORT", "8080"))

    # Setup graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(sig: int, frame: object) -> None:
        logging.info(f"Received signal {sig}, initiating graceful shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Create tasks for parallel execution
    polling_task = asyncio.create_task(app.run())
    health_task = asyncio.create_task(run_health_server(health_host, health_port))

    try:
        # Wait for shutdown signal
        await shutdown_event.wait()
        logging.info("Shutdown signal received, stopping services")
    except Exception as exc:
        logging.error(f"Error in main loop: {exc}")
    finally:
        # Cancel both tasks
        polling_task.cancel()
        health_task.cancel()

        # Wait for tasks to complete cancellation
        await asyncio.gather(polling_task, health_task, return_exceptions=True)

        # Cleanup resources
        await app.converter.close()
        await app.bot.session.close()

        logging.info("Graceful shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
