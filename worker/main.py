from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from pathlib import Path
from time import perf_counter

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from config import Settings, load_settings

app = FastAPI(title="worker-service")

# Global state
_settings: Settings | None = None
_bot: Bot | None = None
_http_client: httpx.AsyncClient | None = None
_processed_jobs: set[str] = set()  # Simple in-memory deduplication


def format_ms(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return int(seconds * 1000)


@app.on_event("startup")
async def startup() -> None:
    global _settings, _bot, _http_client
    logging.basicConfig(level=logging.INFO)
    logging.info("Worker service starting up...")

    try:
        _settings = load_settings()
        logging.info("Configuration loaded successfully")
    except ValueError as exc:
        logging.error(f"Configuration error: {exc}")
        raise

    _bot = Bot(token=_settings.bot_token)
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(_settings.conversion_timeout_seconds),
        limits=httpx.Limits(
            max_connections=5,
            max_keepalive_connections=2,
        ),
    )
    logging.info("Worker service initialized successfully")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _bot, _http_client
    if _http_client:
        await _http_client.aclose()
    if _bot:
        await _bot.session.close()
    logging.info("Worker service shutdown complete")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pubsub/push")
async def pubsub_push(request: Request) -> JSONResponse:
    """Handle Pub/Sub push messages."""
    if _settings is None or _bot is None or _http_client is None:
        logging.error("Worker not initialized")
        raise HTTPException(status_code=503, detail="Worker not initialized")

    try:
        body = await request.json()
    except Exception as exc:
        logging.exception("Failed to parse request body: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    # Extract Pub/Sub message
    message = body.get("message", {})
    data_b64 = message.get("data")

    if not data_b64:
        logging.warning("No data in Pub/Sub message")
        return JSONResponse({"status": "ignored", "reason": "no_data"}, status_code=200)

    # Decode job data
    try:
        data_json = base64.b64decode(data_b64).decode("utf-8")
        job = json.loads(data_json)
    except Exception as exc:
        logging.exception("Failed to decode job data: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid job data") from exc

    file_id = job.get("file_id")
    file_unique_id = job.get("file_unique_id")
    chat_id = job.get("chat_id")
    message_id = job.get("message_id")
    file_name = job.get("file_name")
    mime_type = job.get("mime_type")

    if not file_id or not chat_id or not message_id:
        logging.warning("Missing required fields in job: %s", job)
        return JSONResponse({"status": "ignored", "reason": "missing_fields"}, status_code=200)

    # Idempotency check
    idempotency_key = file_unique_id or f"{chat_id}:{message_id}"
    if idempotency_key in _processed_jobs:
        logging.info("Job already processed: %s", idempotency_key)
        return JSONResponse({"status": "duplicate", "key": idempotency_key}, status_code=200)

    # Process the job
    try:
        await process_conversion_job(
            file_id=file_id,
            file_name=file_name or file_id,
            chat_id=chat_id,
            settings=_settings,
            bot=_bot,
            http_client=_http_client,
        )

        # Mark as processed
        _processed_jobs.add(idempotency_key)

        # Limit cache size to prevent memory leak
        if len(_processed_jobs) > 10000:
            # Remove oldest half (simple FIFO approximation)
            items = list(_processed_jobs)
            _processed_jobs.clear()
            _processed_jobs.update(items[5000:])

        logging.info("Job completed successfully: %s", idempotency_key)
        return JSONResponse({"status": "success", "key": idempotency_key}, status_code=200)

    except Exception as exc:
        logging.exception("Job processing failed: %s", exc)
        # Return 5xx to trigger Pub/Sub retry
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}") from exc


async def process_conversion_job(
    file_id: str,
    file_name: str,
    chat_id: int,
    settings: Settings,
    bot: Bot,
    http_client: httpx.AsyncClient,
) -> None:
    """Download, convert, and upload a file."""
    total_started = perf_counter()
    tg_download_s: float | None = None
    convert_s: float | None = None
    tg_upload_s: float | None = None
    in_bytes = 0
    out_bytes = 0

    try:
        # Download from Telegram
        with tempfile.TemporaryDirectory(prefix="worker-file-") as tmpdir:
            source = Path(tmpdir) / file_name

            download_started = perf_counter()
            file_info = await bot.get_file(file_id)
            await bot.download_file(file_info.file_path, destination=source)
            tg_download_s = perf_counter() - download_started
            in_bytes = source.stat().st_size

            logging.info(
                "tg_download file=%s file_id=%s size=%s download_ms=%s",
                file_name, file_id, in_bytes, format_ms(tg_download_s)
            )

            # Convert via converter service
            convert_started = perf_counter()
            files = {"file": (source.name, source.read_bytes(), "application/octet-stream")}
            data: dict[str, str | int] = {"quality": settings.conversion_quality}
            headers = {"X-API-KEY": settings.converter_api_key}

            response = await http_client.post(
                settings.converter_url,
                headers=headers,
                files=files,
                data=data,
            )
            convert_s = perf_counter() - convert_started

            if response.status_code != 200:
                body_preview = response.text[:2048]
                logging.error(
                    "converter_error status=%s body=%s",
                    response.status_code, body_preview
                )
                response.raise_for_status()

            jpg_bytes = response.content
            out_bytes = len(jpg_bytes)

            logging.info(
                "conversion_done file=%s in_bytes=%s out_bytes=%s convert_ms=%s",
                file_name, in_bytes, out_bytes, format_ms(convert_s)
            )

            # Validate output
            if not jpg_bytes or len(jpg_bytes) < 100:
                raise ValueError(f"Invalid conversion output: {len(jpg_bytes)} bytes")

            # Upload to Telegram
            target_name = f"{Path(file_name).stem}.jpg"
            upload_started = perf_counter()

            await bot.send_document(
                chat_id=settings.chat_id,
                message_thread_id=settings.topic_converted_id,
                document=BufferedInputFile(jpg_bytes, filename=target_name),
            )
            tg_upload_s = perf_counter() - upload_started

            total_s = perf_counter() - total_started

            logging.info(
                "job_success file=%s file_id=%s chat_id=%s tg_download_ms=%s "
                "convert_ms=%s tg_upload_ms=%s total_ms=%s in_bytes=%s out_bytes=%s",
                file_name, file_id, chat_id,
                format_ms(tg_download_s),
                format_ms(convert_s),
                format_ms(tg_upload_s),
                format_ms(total_s),
                in_bytes,
                out_bytes,
            )

    except Exception as exc:
        total_s = perf_counter() - total_started
        logging.error(
            "job_failed file=%s file_id=%s chat_id=%s tg_download_ms=%s "
            "convert_ms=%s tg_upload_ms=%s total_ms=%s error=%s",
            file_name, file_id, chat_id,
            format_ms(tg_download_s),
            format_ms(convert_s),
            format_ms(tg_upload_s),
            format_ms(total_s),
            str(exc),
        )
        raise
