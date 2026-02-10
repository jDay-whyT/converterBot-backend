def is_message_not_modified_error(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()
