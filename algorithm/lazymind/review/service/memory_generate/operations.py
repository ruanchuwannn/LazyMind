from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Literal, Optional

try:
    from json_repair import repair_json as _repair_json  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    _repair_json = None

MemoryType = Literal['skill', 'memory', 'user_preference']

_JSON_BLOCK_RE = re.compile(r'```json\s*(.*?)\s*```', re.DOTALL)
_THINK_BLOCK_RE = re.compile(r'<think>.*?</think\s*>', re.DOTALL | re.IGNORECASE)
_SINGLE_STRING_FIELD_RE = re.compile(
    r'^\{\s*"(?P<key>[^"\\]+)"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"\s*,?\s*\}\s*$',
    re.DOTALL,
)


class BadRequestError(ValueError):
    """Raised when request body fields are missing or malformed."""


class UnprocessableContentError(ValueError):
    """Raised when generated content is repeatedly invalid."""


def _extract_json_object(raw: Any) -> Dict[str, Any]:
    text = str(raw).strip()
    text = _THINK_BLOCK_RE.sub('', text).strip()

    match = _JSON_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()

    candidates: List[str] = [text]
    left = text.find('{')
    right = text.rfind('}')
    if left >= 0 and right > left:
        trimmed = text[left: right + 1]
        if trimmed != text:
            candidates.append(trimmed)

    parsed: Any = None
    last_error: Optional[json.JSONDecodeError] = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            last_error = exc
    else:
        try:
            if _repair_json is None:
                raise ImportError('json_repair is not installed')
            for candidate in candidates:
                repaired = _repair_json(candidate, return_objects=True)
                if isinstance(repaired, dict):
                    parsed = repaired
                    break
        except Exception:
            pass

    if parsed is None:
        for candidate in candidates:
            parsed = _extract_single_string_field_object(candidate)
            if isinstance(parsed, dict):
                break

    if parsed is None:
        if last_error is not None:
            raise UnprocessableContentError(
                f'Model output is not valid JSON: {last_error}'
            ) from last_error
        raise UnprocessableContentError('Model output is not valid JSON.')

    if not isinstance(parsed, dict):
        raise UnprocessableContentError('Model output must be a JSON object.')
    return parsed


def _extract_single_string_field_object(text: str) -> Optional[Dict[str, str]]:
    match = _SINGLE_STRING_FIELD_RE.match(text.strip())
    if not match:
        return None

    key = match.group('key').strip()
    raw_value = match.group('value').strip()
    if raw_value.endswith(','):
        raw_value = raw_value[:-1].rstrip()
    if len(raw_value) < 2 or not raw_value.startswith('"') or not raw_value.endswith('"'):
        return None

    inner = raw_value[1:-1]
    try:
        value = json.loads(f'"{inner}"')
    except json.JSONDecodeError:
        value = (
            inner.replace('\\"', '"')
            .replace('\\\\', '\\')
            .replace('\\r', '\r')
            .replace('\\n', '\n')
            .replace('\\t', '\t')
        )
    return {key: value}


def _compact_len(text: Any) -> int:
    return len(''.join(str(text).split()))


def _parse_edit_operations(
    payload: Dict[str, Any],
    *,
    entity_name: str,
    allow_empty_old: bool = False,
) -> List[Dict[str, Any]]:
    if 'content' in payload and 'operations' not in payload:
        content = payload.get('content')
        if not isinstance(content, str):
            raise UnprocessableContentError("Generated field 'content' must be a string.")
        return [{'op': 'replace_all', 'content': content.strip()}]

    operations = payload.get('operations')
    if not isinstance(operations, list) or not operations:
        raise UnprocessableContentError(
            f"Model output for {entity_name} must contain a non-empty 'operations' array."
        )

    normalized_ops: List[Dict[str, Any]] = []
    for idx, raw_op in enumerate(operations):
        if not isinstance(raw_op, dict):
            raise UnprocessableContentError(f"'operations[{idx}]' must be an object.")
        op_name = str(raw_op.get('op') or '').strip()
        if op_name == 'replace_all':
            content = raw_op.get('content')
            if not isinstance(content, str):
                raise UnprocessableContentError("replace_all requires a string field 'content'.")
            if len(operations) != 1:
                raise UnprocessableContentError('replace_all must be the only operation when used.')
            return [{'op': 'replace_all', 'content': content.strip()}]
        if op_name == 'replace_text':
            old = raw_op.get('old')
            new = raw_op.get('new')
            if not isinstance(old, str):
                raise UnprocessableContentError("replace_text requires a string field 'old'.")
            if not isinstance(new, str):
                raise UnprocessableContentError("replace_text requires a string field 'new'.")
            if old == '' and new != '' and not allow_empty_old:
                raise UnprocessableContentError(
                    "replace_text with an empty 'old' is only allowed when 'new' is also empty."
                )
            normalized_ops.append({
                'op': 'replace_text',
                'old': old,
                'new': new,
            })
            continue
        raise UnprocessableContentError(
            f"Unsupported {entity_name} operation {op_name!r}; expected 'replace_text' or 'replace_all'."
        )
    return normalized_ops


def _normalize_numbered_lists(content: str) -> str:
    lines = content.splitlines()
    normalized: List[str] = []
    expected: Optional[int] = None
    last_indent: Optional[str] = None
    item_re = re.compile(r'^(\s*)(\d+)\.\s+(.*)$')

    for line in lines:
        match = item_re.match(line)
        if not match:
            normalized.append(line)
            if line.strip():
                expected = None
                last_indent = None
            continue

        indent, number, body = match.groups()
        if expected is None or indent != last_indent:
            expected = int(number)
            last_indent = indent
        normalized.append(f'{indent}{expected}. {body}')
        expected += 1

    return '\n'.join(normalized)


def _apply_replace_text(current: str, old: str, new: str, *, entity_name: str) -> str:
    if old not in current:
        raise UnprocessableContentError(
            f'replace_text old text was not found in current {entity_name} content.'
        )
    return current.replace(old, new, 1)


def _apply_operations(
    current_content: str,
    payload: Dict[str, Any],
    *,
    entity_name: str,
    allow_empty_old: bool = False,
    replace_text: Optional[Callable[[str, str, str], str]] = None,
    normalize_numbered_lists_after_delete: bool = False,
) -> str:
    operations = _parse_edit_operations(
        payload,
        entity_name=entity_name,
        allow_empty_old=allow_empty_old,
    )
    if operations[0]['op'] == 'replace_all':
        return operations[0]['content']

    current = current_content
    applied_delete = False
    apply_replace_text = replace_text or (
        lambda text, old, new: _apply_replace_text(
            text,
            old,
            new,
            entity_name=entity_name,
        )
    )

    for op in operations:
        if op['op'] != 'replace_text' or op['old'] == op['new']:
            continue
        current = apply_replace_text(current, op['old'], op['new'])
        if not op['new'].strip():
            applied_delete = True

    if normalize_numbered_lists_after_delete and applied_delete:
        current = _normalize_numbered_lists(current)
    return current.strip()
