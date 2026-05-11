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


def _parse_allowed(raw: str) -> set[int]:
    """Parse ALLOWED_EDITORS from various formats: '|', ',', or whitespace-separated."""
    items = re.split(r"[,\s|]+", raw.strip())
    return {int(item) for item in items if item}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    tg_webhook_secret: str
    allowed_editors: frozenset[int]
    chat_id: int
    topic_source_id: int
    gcp_project: str
    pubsub_topic: str
    enable_webhook_setup: bool = False


def load_settings() -> Settings:
    allowed_editors = _parse_allowed(_required("ALLOWED_EDITORS"))
    if not allowed_editors:
        raise ValueError(
            "ALLOWED_EDITORS must contain at least one user ID.\n"
            "Format: comma, pipe, or space-separated user IDs (e.g., '123456789,987654321')"
        )

    enable_webhook_setup = os.getenv("ENABLE_WEBHOOK_SETUP", "false").strip().lower() == "true"

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        tg_webhook_secret=_required("TG_WEBHOOK_SECRET"),
        allowed_editors=frozenset(allowed_editors),
        chat_id=int(_required("CHAT_ID")),
        topic_source_id=int(_required("TOPIC_SOURCE_ID")),
        gcp_project=_required("GCP_PROJECT"),
        pubsub_topic=_required("PUBSUB_TOPIC"),
        enable_webhook_setup=enable_webhook_setup,
    )
