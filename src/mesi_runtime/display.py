from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .constants import ABSENT_VERSION

VERSION_PREVIEW_LENGTH = 8
VERSION_KEYS = {"version", "old_version", "new_version"}


def short_version(value: object, *, length: int = VERSION_PREVIEW_LENGTH) -> str:
    if value is None:
        return ""

    text = str(value)
    if text == ABSENT_VERSION or len(text) <= length or not _looks_like_hash(text):
        return text
    return text[:length]


def format_version_pair(old_version: object, new_version: object) -> str:
    return f"{short_version(old_version)} -> {short_version(new_version)}".rstrip()


def shorten_version_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: short_version(item) if key in VERSION_KEYS else shorten_version_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [shorten_version_fields(item) for item in value]
    return value


def _looks_like_hash(text: str) -> bool:
    return len(text) >= 16 and all(char in "0123456789abcdefABCDEF" for char in text)
