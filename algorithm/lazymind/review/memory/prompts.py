from __future__ import annotations

from lazymind.review.prompts import MEMORY_REVIEW_PROMPT


def build_session_review_prompt(
    *,
    target: str,
    current_content: str,
) -> str:
    if target == 'memory':
        target_instruction = (
            "This backend-triggered review is ONLY for agent working memory. "
            "If saving is warranted, call memory_editor(target='memory', operations=[...]). "
            "Do not call memory_editor with target='user'."
        )
    else:
        target_instruction = (
            "This backend-triggered review is ONLY for user profile / preference content. "
            "If saving is warranted, call memory_editor(target='user', operations=[...]). "
            "Do not call memory_editor with target='memory'."
        )

    existing_label = (
        'Current agent working memory'
        if target == 'memory'
        else 'Current user profile'
    )
    return (
        f'{MEMORY_REVIEW_PROMPT}\n\n'
        '# Backend-triggered target constraint\n'
        f'{target_instruction}\n'
        'For this endpoint, do not call skill_editor, get_skill, vocab_learn, '
        'or any tool except memory_editor. Use only target and operations.\n\n'
        '# Language\n'
        '- Determine the language of new or rewritten memory/user profile content '
        'from current_content and llm_chat_history.\n'
        '- If current_content is non-empty, preserve that language unless the user '
        'explicitly asks for another language.\n'
        "- If current_content is empty, use the dominant language of the user's "
        'messages in llm_chat_history; Chinese user messages should produce '
        'Chinese memory/user profile content.\n'
        '- Apply this to replace_text.new and replace_all.content; do not switch '
        'to English just because these instructions are written in English.\n\n'
        'Do NOT save multi-step reusable workflows, troubleshooting procedures, '
        'lessons learned, tool usage patterns, implementation recipes, SOPs, '
        'or general task conventions as memory or user profile content. Those belong '
        'in skills, but this endpoint must only submit memory edit operations.\n\n'
        '# Required memory edit operation output\n'
        'When a durable update is warranted, output exactly one memory_editor tool call '
        'with an operations array. Supported operations are:\n'
        '- replace_text: {"op": "replace_text", "old": "...", "new": "..."}; '
        "'old' MUST be an exact substring copied from the current content.\n"
        '- replace_all: {"op": "replace_all", "content": "..."}; use this '
        'only when current content is empty, or when the update truly requires '
        'rewriting the full target text.\n'
        'Prefer replace_text whenever current content is non-empty. For adding '
        'a new entry to existing content, replace the smallest exact existing '
        'section or block with the same block plus the new entry. Do not use '
        'replace_all merely because you are adding one item. Use replace_all '
        'only if no exact substring can safely anchor the edit, or the content '
        'needs global deduplication/conflict resolution/reorganization.\n'
        'The operations are applied to the current content below, and the edited '
        'full text is written to the memory_review table for human review. '
        'If no durable update is warranted, do not call memory_editor; reply with '
        '`Nothing to save` and a brief reason.\n\n'
        '--- CURRENT CONTENT ---\n'
        f'## {existing_label}\n{current_content or ""}\n'
        '--- END CURRENT CONTENT ---\n\n'
        'The conversation to review is provided as llm_chat_history by the caller. '
        'Use that history as the source of truth.'
    )


__all__ = ['build_session_review_prompt']
