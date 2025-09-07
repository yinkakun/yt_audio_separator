import re

import validators


def sanitize_input(text: str, max_length: int) -> str:
    """Sanitize and validate user input using validators library"""
    if not validators.length(text, min=1, max=max_length):
        raise ValueError(f"Input must be 1-{max_length} characters long")

    # Remove potentially dangerous characters
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F\x7F-\x9F]', "", text.strip())

    if any(len(word) > 50 for word in sanitized.split()):
        raise ValueError("Input contains words that are too long")

    if not sanitized:
        raise ValueError("Input cannot be empty after sanitization")

    return sanitized


def validate_track_id(track_id: str) -> bool:
    """Validate that track_id is a valid UUID"""
    return validators.uuid(track_id) is True
