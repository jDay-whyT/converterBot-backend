import os
import re
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(
            f"Missing required environment variable: {name}\n"
            f"Please ensure this variable is set in your deployment configuration."
        )
    return value


def normalize_converter_url(raw: str) -> str:
    normalized = raw.strip()
    while normalized.endswith("/"):
        normalized = normalized[:-1]

    normalized = normalized.replace("/convert/convert", "/convert")
    if normalized.endswith("/convert"):
        return normalized
    return f"{normalized}/convert"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_url: str
    tg_webhook_secret: str
    allowed_editors: set[int]
    chat_id: int
    topic_source_id: int
    topic_converted_id: int
    converter_url: str
    converter_api_key: str
    gcp_project: str
    pubsub_topic: str
    enable_webhook_setup: bool = False
    max_file_mb: int = 40
    batch_window_seconds: int = 120
    progress_update_every: int = 3
    conversion_timeout_seconds: int = 120
    conversion_quality: int = 92



def _parse_allowed(raw: str) -> set[int]:
    """Parse ALLOWED_EDITORS from various formats: '|', ',', or whitespace-separated."""
    items = re.split(r"[,\s|]+", raw.strip())
    return {int(item) for item in items if item}


def load_settings() -> Settings:
    allowed_editors = _parse_allowed(_required("ALLOWED_EDITORS"))
    if not allowed_editors:
        raise ValueError(
            "ALLOWED_EDITORS must contain at least one user ID.\n"
            "Format: comma, pipe, or space-separated user IDs (e.g., '123456789,987654321')"
        )

    enable_webhook_setup_str = os.getenv("ENABLE_WEBHOOK_SETUP", "false").strip().lower()
    enable_webhook_setup = enable_webhook_setup_str == "true"

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        bot_url=_required("BOT_URL").rstrip("/"),
        tg_webhook_secret=_required("TG_WEBHOOK_SECRET"),
        allowed_editors=allowed_editors,
        chat_id=int(_required("CHAT_ID")),
        topic_source_id=int(_required("TOPIC_SOURCE_ID")),
        topic_converted_id=int(_required("TOPIC_CONVERTED_ID")),
        converter_url=normalize_converter_url(_required("CONVERTER_URL")),
        converter_api_key=_required("CONVERTER_API_KEY"),
        gcp_project=_required("GCP_PROJECT"),
        pubsub_topic=_required("PUBSUB_TOPIC"),
        enable_webhook_setup=enable_webhook_setup,
        max_file_mb=int(os.getenv("MAX_FILE_MB", "40")),
        batch_window_seconds=int(os.getenv("BATCH_WINDOW_SECONDS", "120")),
        progress_update_every=int(os.getenv("PROGRESS_UPDATE_EVERY", "3")),
    )
