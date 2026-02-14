import os
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
    chat_id: int
    topic_converted_id: int
    converter_url: str
    converter_api_key: str
    max_file_mb: int = 40
    conversion_timeout_seconds: int = 600
    conversion_quality: int = 92


def load_settings() -> Settings:
    return Settings(
        bot_token=_required("BOT_TOKEN"),
        chat_id=int(_required("CHAT_ID")),
        topic_converted_id=int(_required("TOPIC_CONVERTED_ID")),
        converter_url=normalize_converter_url(_required("CONVERTER_URL")),
        converter_api_key=_required("CONVERTER_API_KEY"),
        max_file_mb=int(os.getenv("MAX_FILE_MB", "40")),
        conversion_timeout_seconds=int(os.getenv("CONVERSION_TIMEOUT_SECONDS", "600")),
        conversion_quality=int(os.getenv("CONVERSION_QUALITY", "92")),
    )
