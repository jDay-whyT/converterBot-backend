from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from time import perf_counter
from pathlib import Path

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from batching import BatchProgress, BatchRegistry
from config import Settings, load_settings
from telegram_retry import ProgressDebouncer, TelegramFileSemaphore, telegram_api_retry

SUPPORTED_EXTENSIONS = {
    ".heic",
    ".heif",
    ".dng",
    ".webp",
    ".tif",
    ".tiff",
    ".cr2",
    ".cr3",
}
SUPPORTED_MIME_TYPES = {
    "image/heif",
    "image/heic",
    "image/x-canon-cr2",
    "image/x-canon-cr3",
}
MIME_TO_EXT = {
    "image/heif": ".heif",
    "image/heic": ".heic",
    "image/x-canon-cr2": ".cr2",
    "image/x-canon-cr3": ".cr3",
}


def format_ms(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return int(seconds * 1000)


class ConverterClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def convert(
        self, path: Path, client: httpx.AsyncClient, quality: int = 92, max_side: int | None = None
    ) -> tuple[bytes, int | None]:
        files = {"file": (path.name, path.read_bytes(), "application/octet-stream")}
        data: dict[str, str | int] = {"quality": quality}
        if max_side:
            data["max_side"] = max_side

        headers = {"X-API-KEY": self.settings.converter_api_key}
        final_url = self.settings.converter_url
        file_size = path.stat().st_size
        logging.info("converter_post url=%s filename=%s size=%s", final_url, path.name, file_size)

        request_started = perf_counter()
        response = await client.post(
            final_url,
            headers=headers,
            files=files,
            data=data,
        )
        request_elapsed = perf_counter() - request_started
        logging.info(
            "converter_request file=%s status=%s request_ms=%s",
            path.name,
            response.status_code,
            format_ms(request_elapsed),
        )
        if response.status_code != 200:
            body_preview = response.text[:2048]
            logging.error(
                "converter_response_error url=%s status_code=%s body=%s",
                final_url,
                response.status_code,
                body_preview,
            )
        response.raise_for_status()

        # Validate response content size
        content_size = len(response.content)
        logging.info(f"Converted {path.name}: received {content_size} bytes")

        if content_size < 100:
            raise ValueError(f"Converted file too small: {content_size} bytes (expected at least 100)")

        return response.content, response.status_code


class ConversionBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot = Bot(token=settings.bot_token)
        self.dp = Dispatcher()
        self.registry = BatchRegistry(settings.batch_window_seconds)
        self.converter = ConverterClient(settings)
        self.semaphore = asyncio.Semaphore(2)
        self.file_send_lock = TelegramFileSemaphore()
        self.progress_debouncer = ProgressDebouncer(min_interval=3.0, min_files=3)
        self._http_client: httpx.AsyncClient | None = None
        self._bind_handlers()

    def _bind_handlers(self) -> None:
        self.dp.message.register(self._start, Command("start"))
        self.dp.message.register(self._handle_document, F.document)

    async def start(self) -> None:
        """Initialize resources before starting the bot."""
        logging.info("Initializing httpx.AsyncClient with connection pooling")
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.conversion_timeout_seconds),
            limits=httpx.Limits(
                max_connections=10,  # Maximum total connections
                max_keepalive_connections=5,  # Keep-alive pool size
            ),
        )
        logging.info("httpx.AsyncClient initialized successfully")

    async def stop(self) -> None:
        """Cleanup resources during graceful shutdown."""
        if self._http_client is not None:
            logging.info("Closing httpx.AsyncClient")
            await self._http_client.aclose()
            self._http_client = None
            logging.info("httpx.AsyncClient closed successfully")

    async def _start(self, message: Message) -> None:
        if not self._is_allowed_user(message):
            await telegram_api_retry(
                message.answer,
                "Нет доступа",
                max_retries=2,
            )
            return
        await telegram_api_retry(
            message.answer,
            "Switch with heic, dng, webp, tif, tiff to JPG",
            max_retries=2,
        )

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
            await telegram_api_retry(
                message.answer,
                "Нет доступа",
                max_retries=2,
            )
            return

        if not self._is_source_topic(message):
            return

        assert message.from_user and message.document
        document = message.document
        ext = Path(document.file_name or "").suffix.lower()
        mime_type = (document.mime_type or "").lower()
        if not ext and mime_type in SUPPORTED_MIME_TYPES:
            ext = MIME_TO_EXT.get(mime_type, "")
        if ext not in SUPPORTED_EXTENSIONS:
            logging.info(
                "ignored_file filename=%s ext=%s mime=%s size=%s",
                document.file_name or "",
                ext,
                document.mime_type or "",
                document.file_size,
            )
            return

        file_name = document.file_name or document.file_id
        if not Path(file_name).suffix:
            file_name = f"{file_name}{ext}"

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
            user_id = message.from_user.id
            chat_id = message.chat.id
            in_bytes = 0
            out_bytes = 0
            tg_download_s: float | None = None
            convert_s: float | None = None
            tg_upload_s: float | None = None
            total_started = perf_counter()
            converter_status_code: int | None = None
            result = "ok"
            ok = False
            reason: str | None = None
            try:
                if self._http_client is None:
                    raise RuntimeError("httpx.AsyncClient not initialized. Call start() before processing documents.")

                with tempfile.TemporaryDirectory(prefix="tg-file-") as tmpdir:
                    source = Path(tmpdir) / file_name
                    file_info = await telegram_api_retry(
                        self.bot.get_file,
                        document.file_id,
                        max_retries=2,
                    )

                    download_started = perf_counter()
                    await telegram_api_retry(
                        self.bot.download_file,
                        file_info.file_path,
                        destination=source,
                        max_retries=2,
                    )
                    tg_download_s = perf_counter() - download_started
                    in_bytes = source.stat().st_size

                    convert_started = perf_counter()
                    jpg_bytes, converter_status_code = await self.converter.convert(
                        source,
                        self._http_client,
                        quality=self.settings.conversion_quality,
                    )
                    convert_s = perf_counter() - convert_started
                    out_bytes = len(jpg_bytes)

                # Validate jpg_bytes before sending
                if not jpg_bytes or len(jpg_bytes) < 100:
                    raise ValueError(f"Invalid jpg data: {len(jpg_bytes) if jpg_bytes else 0} bytes")

                target_name = f"{Path(file_name).stem}.jpg"
                logging.info(f"Sending {target_name} to Telegram: {len(jpg_bytes)} bytes")

                upload_started = perf_counter()
                async with self.file_send_lock:
                    await telegram_api_retry(
                        self.bot.send_document,
                        chat_id=self.settings.chat_id,
                        message_thread_id=self.settings.topic_converted_id,
                        document=BufferedInputFile(jpg_bytes, filename=target_name),
                        max_retries=2,
                    )
                tg_upload_s = perf_counter() - upload_started
                ok = True
            except httpx.HTTPStatusError as exc:
                converter_status_code = exc.response.status_code if exc.response is not None else None
                reason = exc.response.text[:120] if exc.response is not None else str(exc)
                result = "error"
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
                result = "error"
            finally:
                await self._register_result(batch, ok, document.file_name or "file", reason)
                total_s = perf_counter() - total_started
                short_reason = (reason or "").replace("\n", " ")[:120] or "-"
                logging.info(
                    "file_pipeline file=%s user_id=%s chat_id=%s tg_download_ms=%s convert_ms=%s "
                    "tg_upload_ms=%s total_ms=%s in_bytes=%s out_bytes=%s converter_status_code=%s "
                    "result=%s reason=%s",
                    file_name,
                    user_id,
                    chat_id,
                    format_ms(tg_download_s),
                    format_ms(convert_s),
                    format_ms(tg_upload_s),
                    format_ms(total_s),
                    in_bytes,
                    out_bytes,
                    converter_status_code,
                    result,
                    short_reason,
                )

    async def _register_result(self, batch: BatchProgress, ok: bool, file_name: str, reason: str | None) -> None:
        async with batch.lock:
            batch.processed += 1
            if ok:
                batch.success += 1
            else:
                batch.failed += 1
                if reason:
                    batch.errors.append(f"ошибка на файле {file_name} ({reason})")

            # Use progress debouncer to avoid spamming Telegram API
            batch_id = id(batch)
            current_time = perf_counter()
            needs_update = self.progress_debouncer.should_update(
                batch_id=batch_id,
                processed=batch.processed,
                total=batch.total,
                has_error=not ok,
                current_time=current_time,
            )
            if needs_update:
                try:
                    await self._update_progress(batch)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("Failed to update progress message: %s", exc)

            # Clean up debouncer state when batch is complete
            if batch.processed == batch.total:
                self.progress_debouncer.reset(batch_id)

    async def _update_progress(self, batch: BatchProgress) -> None:
        text_lines = [
            f"Обработано: {batch.processed}/{batch.total}",
            f"Успешно: {batch.success}",
            f"Ошибок: {batch.failed}",
        ]
        if batch.errors:
            text_lines.extend(batch.errors[-5:])
        text = "\n".join(text_lines)

        if batch.progress_message_id is None:
            sent = await telegram_api_retry(
                self.bot.send_message,
                chat_id=batch.chat_id,
                message_thread_id=batch.topic_id,
                text=text,
                max_retries=2,
            )
            batch.progress_message_id = sent.message_id
            return

        await telegram_api_retry(
            self.bot.edit_message_text,
            chat_id=batch.chat_id,
            message_id=batch.progress_message_id,
            text=text,
            max_retries=2,
            ignore_message_not_modified=True,
        )

    async def run(self) -> None:
        await self.dp.start_polling(self.bot)


async def handle_root(request: web.Request) -> web.Response:
    """Health check endpoint for Cloud Run."""
    bot_app = request.app.get("bot_app")
    if bot_app is None:
        return web.Response(status=503, text="bot not initialized")
    return web.Response(text="ok")


async def handle_healthz(request: web.Request) -> web.Response:
    """Detailed health check endpoint."""
    bot_app = request.app.get("bot_app")
    if bot_app is None:
        return web.json_response({"ok": False, "bot_ready": False}, status=503)
    return web.json_response({"ok": True, "bot_ready": True})


async def run_health_server(host: str, port: int, bot_app: ConversionBot) -> None:
    """Run HTTP health check server for Cloud Run."""
    app = web.Application()
    app["bot_app"] = bot_app  # Store reference to initialized bot
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

    # Load settings FIRST - fail fast if config is invalid
    logging.info("Loading configuration...")
    try:
        settings = load_settings()
        logging.info("Configuration loaded successfully")
    except ValueError as exc:
        logging.error(f"Configuration error: {exc}")
        logging.error("Bot cannot start due to missing or invalid environment variables")
        raise SystemExit(1) from exc

    # Initialize bot
    app = ConversionBot(settings)
    logging.info("Bot initialized successfully")

    # Initialize httpx client and other resources
    await app.start()

    # Cloud Run health server config - start AFTER bot is ready
    health_host = "0.0.0.0"
    health_port = int(os.getenv("PORT", "8080"))

    # Setup graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(sig: int, frame: object) -> None:
        logging.info(f"Received signal {sig}, initiating graceful shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Initialize task references
    health_task = None
    polling_task = None

    try:
        # Start health server with initialized bot reference
        health_task = asyncio.create_task(run_health_server(health_host, health_port, app))
        await asyncio.sleep(0.5)  # Give health server a moment to start listening
        logging.info("Health server started")

        # Start bot polling
        polling_task = asyncio.create_task(app.run())
        logging.info("Bot polling started")

        # Wait for shutdown signal
        await shutdown_event.wait()
        logging.info("Shutdown signal received, stopping services")
    except Exception as exc:
        logging.error(f"Error in main loop: {exc}", exc_info=True)
    finally:
        # Cancel and gather only existing tasks
        tasks = [t for t in [health_task, polling_task] if t is not None]
        for task in tasks:
            task.cancel()

        # Wait for tasks to complete cancellation
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Cleanup resources in proper order
        await app.stop()  # Close httpx client
        await app.bot.session.close()  # Close bot session last

        logging.info("Graceful shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
