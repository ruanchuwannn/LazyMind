from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..operations import _compact_len

_MAX_MEMORY_CONTENT_CHARS = 1500
_MAX_OLDER_MEMORY_SUMMARY_CHARS = 500

_DATE_BULLET_RE = re.compile(r'^-\s+(.+?)(?::\s*(.*))?$')
_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_SECTION_HEADER_TO_KEY = OrderedDict((
    ('用户在做', 'doing'),
    ('我们讨论了', 'discussed'),
    ('状态/冲突', 'status'),
))
_SECTION_KEY_TO_HEADER = {v: k for k, v in _SECTION_HEADER_TO_KEY.items()}
_MEMORY_SECTION_KEYS = tuple(_SECTION_KEY_TO_HEADER.keys())


def _append_unique(existing: List[str], values: List[str]) -> List[str]:
    merged = list(existing)
    for value in values:
        if value not in merged:
            merged.append(value)
    return merged


def _new_day_record() -> Dict[str, List[str]]:
    return {key: [] for key in _MEMORY_SECTION_KEYS}


def _parse_existing_memory(content: str) -> 'OrderedDict[str, Dict[str, List[str]]]':
    days: 'OrderedDict[str, Dict[str, List[str]]]' = OrderedDict()
    current_date: Optional[str] = None
    current_section: Optional[str] = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        date_match = _DATE_BULLET_RE.match(stripped)
        if line.startswith('- ') and date_match:
            current_date = date_match.group(1).strip()
            current_section = None
            days.setdefault(current_date, _new_day_record())
            inline_text = (date_match.group(2) or '').strip()
            if inline_text:
                days[current_date]['discussed'] = _append_unique(
                    days[current_date]['discussed'],
                    [inline_text],
                )
            continue

        if current_date is None:
            continue

        header = stripped.rstrip(':')
        if header in _SECTION_HEADER_TO_KEY:
            current_section = _SECTION_HEADER_TO_KEY[header]
            continue

        bullet_value = stripped
        if stripped.startswith('- '):
            bullet_value = stripped[2:].strip()
        if current_section and bullet_value:
            days[current_date][current_section] = _append_unique(
                days[current_date][current_section],
                [bullet_value],
            )

    return days


def _render_memory(days: 'OrderedDict[str, Dict[str, List[str]]]') -> str:
    lines: List[str] = []
    for date, sections in days.items():
        has_content = any(sections.get(key) for key in _MEMORY_SECTION_KEYS)
        if not has_content:
            continue
        lines.append(f'- {date}')
        for key in _MEMORY_SECTION_KEYS:
            items = sections.get(key) or []
            if not items:
                continue
            lines.append(f'  {_SECTION_KEY_TO_HEADER[key]}:')
            for item in items:
                lines.append(f'  - {item}')
    return '\n'.join(lines).strip()


def _parse_iso_date(value: str) -> Optional[datetime]:
    if not _ISO_DATE_RE.match(value.strip()):
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d')
    except ValueError:
        return None


def _trim_text_to_chars(text: str, limit: int) -> str:
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + '…'


def _memory_day_summary(date: str, sections: Dict[str, List[str]]) -> str:
    parts: List[str] = []
    for key in _MEMORY_SECTION_KEYS:
        values = sections.get(key) or []
        if values:
            parts.append(f'{_SECTION_KEY_TO_HEADER[key]}：{"；".join(values)}')
    if not parts:
        return ''
    return f'{date}：{"；".join(parts)}'


def _compact_memory_to_recent_week(content: str) -> str:
    if _compact_len(content) <= _MAX_MEMORY_CONTENT_CHARS:
        return content.strip()

    days = _parse_existing_memory(content)
    dated_days = [
        (day, parsed)
        for day in days
        for parsed in [_parse_iso_date(day)]
        if parsed is not None
    ]
    if not dated_days:
        return content.strip()

    latest_day = max(parsed for _, parsed in dated_days)
    cutoff = latest_day - timedelta(days=6)
    older: 'OrderedDict[str, Dict[str, List[str]]]' = OrderedDict()
    recent: 'OrderedDict[str, Dict[str, List[str]]]' = OrderedDict()

    for day, sections in days.items():
        parsed = _parse_iso_date(day)
        if parsed is not None and parsed >= cutoff:
            recent[day] = sections
        else:
            older[day] = sections

    result_days: 'OrderedDict[str, Dict[str, List[str]]]' = OrderedDict()
    if older:
        older_summary = '；'.join(
            summary
            for day, sections in older.items()
            for summary in [_memory_day_summary(day, sections)]
            if summary
        )
        recent_text = _render_memory(recent)
        summary_budget = min(
            _MAX_OLDER_MEMORY_SUMMARY_CHARS,
            max(0, _MAX_MEMORY_CONTENT_CHARS - _compact_len(recent_text) - 20),
        )
        if older_summary and summary_budget > 0:
            result_days['一周前摘要'] = _new_day_record()
            result_days['一周前摘要']['discussed'] = [
                _trim_text_to_chars(older_summary, summary_budget)
            ]

    result_days.update(recent)
    result = _render_memory(result_days)
    return result or content.strip()
