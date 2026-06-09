from typing import Any, Dict, List, Literal

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
    agentic_config = lazyllm.globals['agentic_config']
    session_id = str(agentic_config['session_id']).strip()
    storage_target = 'user_preference' if raw_target == 'user' else raw_target
    if raw_target == 'user':
        current_content = (
            agentic_config.get('user')
            or agentic_config.get('user_preference')
            or agentic_config.get('current_content')
            or ''
        )
    else:
        current_content = (
            agentic_config.get(raw_target)
            or agentic_config.get('current_content')
            or ''
        )
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
