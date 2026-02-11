import os
import re
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_editors: set[int]
    chat_id: int
    topic_source_id: int
    topic_converted_id: int
    converter_url: str
    converter_api_key: str
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
    return Settings(
        bot_token=_required("BOT_TOKEN"),
        allowed_editors=_parse_allowed(_required("ALLOWED_EDITORS")),
        chat_id=int(_required("CHAT_ID")),
        topic_source_id=int(_required("TOPIC_SOURCE_ID")),
        topic_converted_id=int(_required("TOPIC_CONVERTED_ID")),
        converter_url=_required("CONVERTER_URL").rstrip("/"),
        converter_api_key=_required("CONVERTER_API_KEY"),
        max_file_mb=int(os.getenv("MAX_FILE_MB", "40")),
        batch_window_seconds=int(os.getenv("BATCH_WINDOW_SECONDS", "120")),
        progress_update_every=int(os.getenv("PROGRESS_UPDATE_EVERY", "3")),
    )
