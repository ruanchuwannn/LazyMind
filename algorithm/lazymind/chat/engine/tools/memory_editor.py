from typing import Any, Dict, List, Literal, Optional

import lazyllm
from typing_extensions import TypedDict

from lazymind.chat.engine.tools.infra import (
    handle_tool_errors,
    tool_error,
    tool_success,
)


class EditOperation(TypedDict, total=False):
    """JSON edit operation applied to current memory or user profile text.

    Fields:
        op (str, required): either ``replace_text`` or ``replace_all``.
        old (str, required for replace_text): exact substring to replace.
        new (str, required for replace_text): replacement text.
        content (str, required for replace_all): full replacement content.
    """

    op: str
    old: str
    new: str
    content: str


MemoryEditorTarget = Literal['memory', 'user']


def _agentic_config() -> Dict[str, Any]:
    config = lazyllm.globals.get('agentic_config') or {}
    return config if isinstance(config, dict) else {}


def _session_id(agentic_config: Optional[Dict[str, Any]] = None) -> str:
    config = agentic_config if isinstance(agentic_config, dict) else _agentic_config()
    return str(config.get('session_id') or getattr(lazyllm.globals, '_sid', '') or '').strip()


def _current_content_for_target(agentic_config: Dict[str, Any], target: str) -> str:
    keys = (
        ('user', 'user_preference')
        if target == 'user'
        else (target,)
    )
    for key in keys:
        value = agentic_config.get(key)
        if isinstance(value, str):
            return value
    fallback = agentic_config.get('current_content')
    if isinstance(fallback, str):
        return fallback
    return ''


def _storage_target(target: str) -> str:
    return 'user_preference' if target == 'user' else target


@handle_tool_errors
def memory_editor(
    target: MemoryEditorTarget,
    operations: List[EditOperation],
) -> Dict[str, Any]:
    """Apply edit operations to memory or user profile and submit a review row.

    Call this tool only after comparing the conversation with the current full
    target text. The tool applies the supplied JSON edit operations to that
    original text, validates the edited full text, and writes one pending row to
    the algorithm-side ``memory_review`` table. It returns status metadata only;
    it does not return the edited content.

    Args:
        target: Which buffer the edit operations belong to. ``'memory'`` is the
            agent's own working memory about the user's ongoing context and
            prior discussions; ``'user'`` is the user profile / preference text.
        operations: Ordered JSON edit operations. Supported operations:

            - ``{"op": "replace_text", "old": "...", "new": "..."}``:
              replace the first exact ``old`` substring with ``new``. Prefer
              this whenever the current content is non-empty, including when
              adding a new entry to an existing section.
            - ``{"op": "replace_all", "content": "..."}``: replace the
              full original target text with ``content``. Use this only when
              the current content is empty, no exact substring can safely
              anchor the edit, or the update needs global deduplication,
              conflict resolution, or broader reorganization.
    """
    raw_target = str(target).strip()
    if raw_target not in {'memory', 'user'}:
        return tool_error(
            'memory_editor',
            f"Unknown target {target!r}; expected one of 'memory', 'user'."
        )
    if not operations:
        return tool_error('memory_editor', "'operations' must be a non-empty list.")

    agentic_config = _agentic_config()
    session_id = _session_id(agentic_config)
    if not session_id:
        return tool_error('memory_editor', "'session_id' is required in agentic_config.")

    storage_target = _storage_target(raw_target)
    current_content = _current_content_for_target(agentic_config, raw_target)
    operation_payload = [dict(op) for op in operations]
    from lazymind.review.service.memory_generate import (
        UnprocessableContentError,
        _apply_memory_edit_operations,
        _apply_user_preference_edit_operations,
        _validate_generated_content,
    )

    try:
        apply_operations = (
            _apply_user_preference_edit_operations
            if storage_target == 'user_preference'
            else _apply_memory_edit_operations
        )
        edited_content = apply_operations(current_content, {'operations': operation_payload})
        if edited_content.strip() == current_content.strip():
            raise UnprocessableContentError(
                f'Generated {storage_target} content is unchanged from current content. '
                'A review row must contain at least one real content change.'
            )
        edited_content = _validate_generated_content(storage_target, edited_content)
    except UnprocessableContentError as exc:
        return tool_error('memory_editor', str(exc))

    from lazymind.review.memory.db import insert_memory_review_record

    record = insert_memory_review_record(
        target=storage_target,
        session_id=session_id,
        source_content=current_content,
        content=edited_content,
        operations=operation_payload,
    )
    return tool_success('memory_editor', {
        'target': raw_target,
        'storage_target': storage_target,
        'status': 'success',
        'operation_count': len(operation_payload),
        'persisted': 'memory_review',
        'record_id': record.get('id'),
        'review_status': record.get('review_status', 'pending'),
    })
