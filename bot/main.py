from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from aiohttp import web
from aiogram import Bot
from google.cloud import pubsub_v1

from config import Settings, load_settings


class ConversionBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot = Bot(token=settings.bot_token)


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


async def handle_telegram_webhook(request: web.Request) -> web.Response:
    bot_app = request.app.get("bot_app")
    if bot_app is None:
        return web.Response(status=503, text="bot not initialized")

    expected = bot_app.settings.tg_webhook_secret.strip()
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").strip()
    if not expected or got != expected:
        return web.Response(status=401, text="unauthorized")

    try:
        update_payload = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    if "update_id" not in update_payload:
        logging.info("webhook_received: no update_id in payload, ignoring")
        return web.Response(status=200, text="ok")

    file_id = None
    file_unique_id = None
    chat_id = None
    message_id = None
    message_thread_id = None
    from_user_id = None
    mime_type = None
    file_name = None

    try:
        message = update_payload.get("message", {})
        if message:
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")
            message_thread_id = message.get("message_thread_id")
            from_user_id = message.get("from", {}).get("id")

            document = message.get("document")
            if document:
                file_id = document.get("file_id")
                file_unique_id = document.get("file_unique_id")
                mime_type = document.get("mime_type")
                file_name = document.get("file_name")

            photos = message.get("photo", [])
            if photos and not file_id:
                photo = photos[-1]
                file_id = photo.get("file_id")
                file_unique_id = photo.get("file_unique_id")

        is_correct_chat = chat_id == bot_app.settings.chat_id
        is_source_topic = message_thread_id == bot_app.settings.topic_source_id
        is_allowed = from_user_id in bot_app.settings.allowed_editors
        if file_id and chat_id and message_id and is_correct_chat and is_source_topic:
            if not is_allowed:
                logging.info("webhook_ignored user_id=%s not in allowed_editors", from_user_id)
            else:
                publisher = request.app.get("pubsub_publisher")
                topic_path = request.app.get("pubsub_topic_path")

                if publisher and topic_path:
                    job_data = {
                        "file_id": file_id,
                        "file_unique_id": file_unique_id,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "mime_type": mime_type,
                        "file_name": file_name,
                        "update_id": update_payload.get("update_id"),
                    }

                    message_bytes = json.dumps(job_data).encode("utf-8")
                    future = publisher.publish(topic_path, message_bytes)
                    try:
                        await asyncio.to_thread(future.result, timeout=10)
                    except Exception as pub_exc:
                        logging.error(
                            "pubsub_publish_failed file_id=%s error=%s",
                            file_id, pub_exc,
                        )
                        return web.Response(status=500, text="publish failed")
                    logging.info(
                        "pubsub_published file_id=%s file_unique_id=%s chat_id=%s message_id=%s",
                        file_id, file_unique_id, chat_id, message_id,
                    )
                else:
                    logging.warning("pubsub_publisher or topic_path not configured")
        else:
            logging.debug("webhook_received: no file info found in update")

    except Exception as exc:
        logging.exception("Error processing webhook for Pub/Sub: %s", exc)

    return web.Response(status=200, text="ok")


async def start_health_server(
    host: str,
    port: int,
    bot_app: ConversionBot,
    pubsub_publisher: pubsub_v1.PublisherClient | None,
    pubsub_topic_path: str | None,
) -> web.AppRunner:
    """Start HTTP health check server for Cloud Run."""
    app = web.Application()
    app["bot_app"] = bot_app
    app["pubsub_publisher"] = pubsub_publisher
    app["pubsub_topic_path"] = pubsub_topic_path
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_post("/telegram/webhook", handle_telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logging.info("Health server listening on %s:%s", host, port)
    return runner


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)

    logging.info("Loading configuration...")
    try:
        settings = load_settings()
        logging.info("Configuration loaded successfully")
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc

    pubsub_publisher = None
    pubsub_topic_path = None
    try:
        pubsub_publisher = pubsub_v1.PublisherClient()
        pubsub_topic_path = pubsub_publisher.topic_path(settings.gcp_project, settings.pubsub_topic)
        logging.info("Pub/Sub publisher initialized: topic=%s", pubsub_topic_path)
    except Exception as exc:
        logging.exception("Failed to initialize Pub/Sub publisher: %s", exc)
        raise SystemExit(1) from exc

    app = ConversionBot(settings)
    logging.info("Bot initialized successfully")

    health_host = "0.0.0.0"
    health_port = int(os.getenv("PORT", "8080"))

    shutdown_event = asyncio.Event()

    def signal_handler(sig: int, frame: object) -> None:
        logging.info("Received signal %s, initiating graceful shutdown", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    health_runner = None
    try:
        health_runner = await start_health_server(
            health_host, health_port, app, pubsub_publisher, pubsub_topic_path
        )

        if settings.enable_webhook_setup:
            url = os.getenv("BOT_URL", "").rstrip("/")
            if not url:
                logging.error("BOT_URL is empty, skip setWebhook")
            else:
                webhook_url = f"{url}/telegram/webhook"
                try:
                    await app.bot.set_webhook(
                        url=webhook_url,
                        secret_token=settings.tg_webhook_secret,
                    )
                    logging.info("Telegram webhook configured: %s", webhook_url)
                except Exception:
                    logging.exception("Failed to configure Telegram webhook: %s", webhook_url)
        else:
            logging.info("ENABLE_WEBHOOK_SETUP=false, skipping webhook setup")

        await shutdown_event.wait()
        logging.info("Shutdown signal received, stopping services")
    except Exception as exc:
        logging.error("Error in main loop: %s", exc, exc_info=True)
    finally:
        if health_runner is not None:
            await health_runner.cleanup()

        await app.bot.session.close()

        if pubsub_publisher:
            pubsub_publisher.stop()

        logging.info("Graceful shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
