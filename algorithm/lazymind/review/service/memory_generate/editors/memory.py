from __future__ import annotations

import re
from typing import Any, Dict, Optional

from ..operations import (
    UnprocessableContentError,
    _apply_operations,
    _apply_replace_text,
)
from .memory_structure import _compact_memory_to_recent_week


def _extract_memory_day_date(block: str) -> Optional[str]:
    for line in block.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r'^-\s+(\d{4}-\d{2}-\d{2})(?:\s.*)?$', stripped)
        if match:
            return match.group(1)
        return None
    return None


def _find_memory_day_block(content: str, date: str) -> Optional[tuple[int, int]]:
    lines = content.splitlines(keepends=True)
    position = 0
    start: Optional[int] = None

    for line in lines:
        stripped = line.strip()
        match = re.match(r'^-\s+(\d{4}-\d{2}-\d{2})(?:\s.*)?$', stripped)
        if match:
            if start is not None:
                return (start, position)
            if match.group(1) == date:
                start = position
        position += len(line)

    if start is not None:
        return (start, len(content))
    return None


def _insert_or_replace_memory_day_block(content: str, day_block: str) -> str:
    day_block = day_block.strip()
    date = _extract_memory_day_date(day_block)
    if date is None:
        raise UnprocessableContentError(
            'replace_text with empty old requires new to be a complete memory day block beginning with "- YYYY-MM-DD".'  # noqa: E501
        )

    found = _find_memory_day_block(content, date)
    if found is None:
        base = content.strip()
        if not base:
            return day_block
        return f'{base}\n{day_block}'

    start, end = found
    prefix = content[:start].rstrip()
    suffix = content[end:].lstrip('\n')
    parts = [part for part in (prefix, day_block, suffix.strip()) if part]
    return '\n'.join(parts)


def _apply_memory_replace_text(current: str, old: str, new: str) -> str:
    if old == '' and new.strip():
        return _insert_or_replace_memory_day_block(current, new)
    if old not in current and _extract_memory_day_date(new):
        return _insert_or_replace_memory_day_block(current, new)
    return _apply_replace_text(current, old, new, entity_name='memory')


def _apply_memory_edit_operations(current_content: str, payload: Dict[str, Any]) -> str:
    compacted_content = _compact_memory_to_recent_week(current_content)
    edited_content = _apply_operations(
        compacted_content,
        payload,
        entity_name='memory',
        allow_empty_old=True,
        replace_text=_apply_memory_replace_text,
    )
    return _compact_memory_to_recent_week(edited_content)
