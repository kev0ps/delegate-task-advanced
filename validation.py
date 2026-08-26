"""Input validation for delegate-task-advanced."""

from __future__ import annotations

import unicodedata
from typing import Any

MAX_LIST_ITEMS = 8
MAX_ITEM_CHARS = 128
MAX_DISPLAY_NAME_CHARS = 80
_ALLOWED_DISPLAY_PUNCTUATION = frozenset(" -_.()")
_ALLOWED_IDENTIFIER_PUNCTUATION = frozenset("-_/:.")


def validate_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string.")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("name must not contain control characters.")
    name = " ".join(value.strip().split())
    if not name or len(name) > MAX_DISPLAY_NAME_CHARS:
        raise ValueError("name must contain 1 to 80 characters.")
    for char in name:
        if not (char.isalnum() or char in _ALLOWED_DISPLAY_PUNCTUATION):
            raise ValueError(
                "name may contain letters, numbers, spaces, hyphens, underscores, dots, and parentheses only."
            )
    return name


def normalize_name_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of names.")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{field} accepts at most {MAX_LIST_ITEMS} entries.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings.")
        name = item.strip()
        if not name or len(name) > MAX_ITEM_CHARS:
            raise ValueError(
                f"{field} entries must contain 1 to {MAX_ITEM_CHARS} characters."
            )
        if name.startswith(("/", ".")) or ".." in name:
            raise ValueError(f"Invalid {field} entry: {name!r}.")
        if any(
            not (char.isalnum() or char in _ALLOWED_IDENTIFIER_PUNCTUATION)
            for char in name
        ):
            raise ValueError(f"Invalid {field} entry: {name!r}.")
        if name not in seen:
            normalized.append(name)
            seen.add(name)
    return normalized


def validate_identifier(value: Any, field: str, *, max_chars: int = 128) -> str | None:
    """Validate one optional Hermes/provider identifier without interpreting it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    identifier = value.strip()
    if not identifier or len(identifier) > max_chars:
        raise ValueError(f"{field} must contain 1 to {max_chars} characters.")
    if identifier.startswith(("/", ".")) or ".." in identifier:
        raise ValueError(f"Invalid {field}: {identifier!r}.")
    if any(
        not (char.isalnum() or char in _ALLOWED_IDENTIFIER_PUNCTUATION)
        for char in identifier
    ):
        raise ValueError(f"Invalid {field}: {identifier!r}.")
    return identifier


def validate_text(value: Any, field: str, *, required: bool, max_chars: int) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    text = value.strip() if required else value
    if required and not text:
        raise ValueError(f"{field} must not be empty.")
    if len(text) > max_chars:
        raise ValueError(f"{field} exceeds the {max_chars}-character limit.")
    return text
