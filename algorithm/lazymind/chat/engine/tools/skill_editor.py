from typing import Any, Dict, List, Literal, Optional

import lazyllm
from typing_extensions import TypedDict

from lazymind.chat.engine.tools.infra import (
    build_skill_identity,
    handle_tool_errors,
    is_writable_skill_source,
    list_all_skill_entries,
    normalize_skill_category,
    tool_error,
    tool_success,
    validate_skill_content,
    validate_skill_name,
)
from lazymind.chat.engine.tools.infra.skill_review_store import (
    SKILL_REVIEW_TYPE_NEW,
    SKILL_REVIEW_TYPE_PATCH,
    find_pending_skill_review,
    insert_skill_review_result,
    mark_skill_review_delete,
)
from lazymind.config import config as _cfg


class SkillEditOperation(TypedDict, total=False):
    """JSON edit operation applied to current SKILL.md content."""

    op: str
    old: str
    new: str
    content: str


@handle_tool_errors
def skill_editor(
    name: str,
    action: Literal['create', 'modify', 'remove'],
    category: Optional[str],
    content: Optional[str] = None,
    operations: Optional[List[SkillEditOperation]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Manage skills by creating, modifying, or removing a skill entry.

    Args:
        name: Skill name.
        action: Skill workflow to run. Use 'create' to submit a new SKILL.md
            content row for review, 'modify' to edit an existing remote skill
            using the 'operations' argument and submit the edited content for
            review, or 'remove' to mark an existing remote skill for deletion.
            For 'modify' and 'remove', a pending review row for the same
            category/name blocks the request.
        category: Skill category directory used to locate category/name/SKILL.md.
        content: Full SKILL.md content, including YAML frontmatter with
            name/category/description. ONLY for action='create'. Do NOT pass
            for action='modify' or 'remove'.
        operations: Ordered JSON edit operations. ONLY for action='modify'.
            Do NOT pass for action='create' or 'remove'. Supported operations:

            - ``{"op": "replace_text", "old": "...", "new": "..."}``:
              replace the first exact ``old`` substring with ``new``.
              Prefer multiple small replace_text operations for local edits.
            - ``{"op": "replace_all", "content": "..."}``: replace the
              full original SKILL.md content with ``content``. Use this only
              when exact local replacement is not safe enough.
        reason: Why the skill should be removed. ONLY for action='remove'.
    """
    lazyllm.LOG.info(
        '[skill_editor] called '
        f'name={name!r} action={action!r} '
        f'category={category!r} content_len={len(content) if content else 0} '
        f'operations_count={len(operations) if operations else 0}'
    )

    name_error = validate_skill_name(name)
    if name_error:
        return tool_error('skill_editor', name_error, log_message=f'[skill_editor] fail reason={name_error!r}')

    agentic_config = lazyllm.globals['agentic_config']
    user_id = str(agentic_config.get('user_id') or '').strip()

    normalized_category = normalize_skill_category(category)
    if normalized_category is None:
        return tool_error(
            'skill_editor',
            f'Category {category!r} is invalid; it must be a single '
            "ASCII-safe path segment (only letters, digits, '-', '_' "
            "and '.'; no spaces, no Chinese, no '/')."
        )

    existing_skills = list_all_skill_entries(_cfg['skill_fs_url'])
    skill_id = build_skill_identity(normalized_category or '', name)
    existing_skill = existing_skills.get(skill_id)
    lazyllm.LOG.info(
        '[skill_editor] lookup '
        f'skill_id={skill_id!r} '
        f'found={existing_skill is not None} '
        f'existing_keys={list(existing_skills.keys())!r}'
    )

    if action == 'create':
        content_error = validate_skill_content(content or '')
        if content_error:
            return tool_error(
                'skill_editor',
                content_error,
                log_message=f'[skill_editor] fail reason={content_error!r}',
            )
        if operations:
            return tool_error('skill_editor', "action='create' must not include 'operations'.")
        if existing_skill:
            source = existing_skill.get('source', 'file')
            if not is_writable_skill_source(source):
                return tool_error(
                    'skill_editor',
                    f'Skill {name!r} already exists in category {normalized_category!r} '
                    f'with read-only source {source!r}; skill_editor can only write remote skills.'
                )
            return tool_error(
                'skill_editor',
                f'Skill {name!r} already exists in category {normalized_category!r}; '
                "use action='modify' to edit it or action='remove' to delete it first."
            )

        record = insert_skill_review_result(
            category=normalized_category,
            skill_name=name,
            review_type=SKILL_REVIEW_TYPE_NEW,
            skill_content=content or '',
            user_id=user_id,
        )
        return tool_success('skill_editor', {
            'name': name,
            'action': action,
            'category': normalized_category,
            'status': 'success',
            'persisted': 'skill_review_results',
            'record_id': record.get('id'),
            'requestid': record.get('requestid'),
            'review_status': record.get('review_status', 'pending'),
            'type': record.get('type', SKILL_REVIEW_TYPE_NEW),
        })

    if action == 'modify':
        if content is not None:
            return tool_error('skill_editor', "action='modify' must not include 'content'; use 'operations'.")
        if not operations:
            return tool_error('skill_editor', "action='modify' requires a non-empty 'operations' list.")
        if not existing_skill:
            return tool_error(
                'skill_editor',
                f'Skill {name!r} does not exist in category {normalized_category!r}; '
                "use action='create' to add a new skill."
            )
        source = existing_skill.get('source', 'file')
        lazyllm.LOG.info(
            '[skill_editor] modify_check '
            f'source={source!r} '
            f'writable={is_writable_skill_source(source)}'
        )
        if not is_writable_skill_source(source):
            return tool_error(
                'skill_editor',
                f'Skill {name!r} in category {normalized_category!r} has read-only source '
                f'{source!r}; skill_editor can only modify remote skills.'
            )

        pending = find_pending_skill_review(normalized_category, name)
        if pending:
            return _pending_review_error(name, normalized_category, pending)

        try:
            from lazymind.rewrite.base import UnprocessableContentError
            from lazymind.rewrite.skill import _apply_skill_edit_operations

            current_content = existing_skill.get('content') or ''
            operation_payload = [dict(op) for op in operations]
            edited_content = _apply_skill_edit_operations(
                current_content,
                {'operations': operation_payload},
            )
            if edited_content.strip() == current_content.strip():
                raise UnprocessableContentError(
                    'Edited SKILL.md content is unchanged from current content. '
                    'A review row must contain at least one real content change.'
                )
        except UnprocessableContentError as exc:
            return tool_error('skill_editor', str(exc))

        content_error = validate_skill_content(edited_content)
        if content_error:
            return tool_error('skill_editor', content_error)

        record = insert_skill_review_result(
            category=normalized_category,
            skill_name=name,
            review_type=SKILL_REVIEW_TYPE_PATCH,
            skill_content=edited_content,
            user_id=user_id,
            summary=f'skill_editor operations: {len(operation_payload)}',
        )
        return tool_success('skill_editor', {
            'name': name,
            'action': action,
            'category': normalized_category,
            'status': 'success',
            'operation_count': len(operation_payload),
            'persisted': 'skill_review_results',
            'record_id': record.get('id'),
            'requestid': record.get('requestid'),
            'review_status': record.get('review_status', 'pending'),
            'type': record.get('type', SKILL_REVIEW_TYPE_PATCH),
        })

    if action == 'remove':
        if content is not None or operations:
            return tool_error('skill_editor', "action='remove' must not include 'content' or 'operations'.")
        if not existing_skill:
            return tool_error(
                'skill_editor',
                f'Skill {name!r} does not exist in category {normalized_category!r}; '
                'nothing to remove.'
            )
        source = existing_skill.get('source', 'file')
        if not is_writable_skill_source(source):
            return tool_error(
                'skill_editor',
                f'Skill {name!r} in category {normalized_category!r} has read-only source '
                f'{source!r}; skill_editor can only remove remote skills.'
            )

        pending = find_pending_skill_review(normalized_category, name)
        if pending:
            return _pending_review_error(name, normalized_category, pending)

        record = mark_skill_review_delete(
            category=normalized_category,
            skill_name=name,
            user_id=user_id,
            summary=(reason or '').strip() or None,
        )
        if not record:
            return tool_error(
                'skill_editor',
                f'Skill {name!r} in category {normalized_category!r} has no skill_review_results row to mark delete.'
            )
        return tool_success('skill_editor', {
            'name': name,
            'action': action,
            'category': normalized_category,
            'reason': reason,
            'status': 'success',
            'persisted': 'skill_review_results',
            'record_id': record.get('id'),
            'requestid': record.get('requestid'),
            'review_status': record.get('review_status', 'pending'),
            'type': record.get('type'),
        })

    return tool_error(
        'skill_editor',
        f"Unknown action {action!r}; expected one of 'create', 'modify', 'remove'."
    )


def _pending_review_error(
    name: str,
    category: str,
    pending: Dict[str, Any],
) -> Dict[str, Any]:
    return tool_error(
        'skill_editor',
        f'Skill {name!r} in category {category!r} is pending in skill_review_results; '
        'please process the pending skill review before submitting another change.',
        meta={'pending_record_id': pending.get('id')},
    )
