from __future__ import annotations

from typing import Optional

from .editors.memory_structure import _MAX_MEMORY_CONTENT_CHARS
from .operations import (
    BadRequestError,
    MemoryType,
    _compact_len,
)

_COMMON_OUTPUT_SPEC = (
    'Output requirements:\n'
    '1. Output only a JSON object; no markdown code blocks, no extra text.\n'
    '2. JSON structure must be {"content": "<new complete text>"}.\n'
    '3. content must be the final complete text after merging all valid input modification requests; do not provide only a patch.\n'  # noqa: E501
)

_EDIT_OUTPUT_SPEC = (
    'Output requirements:\n'
    '1. Output only a JSON object; no markdown code blocks, no extra text.\n'
    '2. Preferred JSON structure is {"operations": [...]}.\n'
    '3. Supported operations are only replace_text and replace_all.\n'
    '4. Do not output any operation except replace_text or replace_all.\n'
    '5. Prefer {"op":"replace_text","old":"<exact old text>","new":"<new text>"} for exact local edits when old is a non-empty substring copied verbatim from current content.\n'  # noqa: E501
    '6. You may output multiple replace_text operations; they will be applied in order.\n'
    '7. Before final output, mentally apply operations in order to the current content using exact plain string search.\n'  # noqa: E501
    '8. For every replace_text, old must be found exactly in the content state at the moment that operation runs.\n'  # noqa: E501
    '9. Keep every replace_text old value as short as safely possible: use one exact line for line deletion/replacement, or one exact phrase/sentence for wording edits.\n'  # noqa: E501
    '10. Never use a whole section, a heading plus body, multiple bullets/list items, or unrelated paragraphs as one replace_text old. Split the change into several smaller replace_text operations instead.\n'  # noqa: E501
    '11. replace_text always replaces the first matching occurrence only.\n'
    '12. For delete/remove requests, the target text may appear in old but MUST NOT appear in new. Do not add, restore, or reword text that user_instruct asks to delete.\n'  # noqa: E501
    '13. Do not output replace_text operations where old and new are identical.\n'
    '14. If the exact old text is absent, outdated, ambiguous, not copied verbatim, or not enough to apply all requested changes safely, output full {"content": "..."} instead of operations.\n'  # noqa: E501
    '15. You may also use {"op":"replace_all","content":"<new full text>"} for full replacement.\n'
    '16. If you use replace_all, it MUST be the only operation in the operations array; do not output replace_all together with any other operation.\n'  # noqa: E501
    '17. The final generated content must reflect the requested change.\n'
)

_MEMORY_EDIT_OUTPUT_SPEC = (
    'Output requirements:\n'
    '1. Output only a JSON object; no markdown code blocks, no extra text.\n'
    '2. JSON structure must be {"operations": [...]}.\n'
    '3. operations is a list of edit commands that will be applied inside the generate endpoint and then rendered back to full memory text.\n'  # noqa: E501
    '4. Supported operations are only replace_text and replace_all; do not output any custom operation.\n'  # noqa: E501
    '5. Prefer {"op":"replace_text","old":"<exact old text>","new":"<new text>"} for exact local edits; replace_text always replaces the first matching occurrence only.\n'  # noqa: E501
    '6. To add a new day, output one replace_text with old="" and new as a complete day block beginning with "- YYYY-MM-DD".\n'  # noqa: E501
    '7. A complete day block should include the date plus relevant fields such as 用户在做, 我们讨论了, 状态/冲突, and replacement summaries for sections that should supersede old text.\n'  # noqa: E501
    '8. To modify an existing day, prefer replacing one exact section line or one exact full day block with a corrected full day block.\n'  # noqa: E501
    '9. Use {"op":"replace_all","content":"<new full memory text>"} only as a last resort when the current memory cannot be edited safely with replace_text.\n'  # noqa: E501
)

_USER_PREFERENCE_EDIT_OUTPUT_SPEC = (
    'Output requirements:\n'
    '1. Output only a JSON object; no markdown code blocks, no extra text.\n'
    '2. JSON structure must be {"operations": [...]}.\n'
    '3. operations is a list of edit commands that will be applied inside the generate endpoint and then rendered back to full user_preference text.\n'  # noqa: E501
    '4. When the current text is free-form or not clearly section-structured, prefer {"op":"replace_text","old":"<exact old text>","new":"<new text>"} for local edits.\n'  # noqa: E501
    '5. replace_text always replaces the first matching occurrence only. If that is not safe enough, use replace_all instead of trying to target a later match.\n'  # noqa: E501
    '6. You may output multiple replace_text operations and they will be applied in order.\n'
    '7. Use {"op":"replace_all","content":"<new full user_preference text>"} only as a last resort when the current text is too malformed or legacy to edit safely with local text replacement operations.\n'  # noqa: E501
)

_COMMON_LANGUAGE_RULES = (
    '[Language]\n'
    '- Determine the output language from the language used in current content and user_instruct.\n'
    '- If the majority of the input is in Chinese (简体中文), write the generated content in Chinese.\n'
    '- If the majority of the input is in English, write the generated content in English.\n'
    '- Be consistent: do not mix languages within the generated content.\n'
)


def _format_preservation_rules(entity: str) -> str:
    return (
        '[Content preservation rules (CRITICAL)]\n'
        f'- You MUST preserve ALL existing {entity} that are NOT explicitly targeted by user_instruct.\n'  # noqa: E501
        f'- When user_instruct only affects one {entity}, keep all others IDENTICAL to the original (same wording, same order).\n'  # noqa: E501
        '- Do NOT rephrase, reformat, or reorganize anything that is not being changed.\n'
        '- If nothing in the current content needs to change for a particular part, copy it VERBATIM into your output.\n'  # noqa: E501
        '- Only remove content that is explicitly marked as outdated or explicitly contradicted by user_instruct.\n'  # noqa: E501
    )


def _format_prompt_tail(
    content: str,
    user_instruct: str,
    output_spec: str = _COMMON_OUTPUT_SPEC,
    previous_error: Optional[str] = None,
) -> str:
    return (
        f'{_format_retry_note(previous_error)}'
        f'{_format_inputs_block(content, user_instruct)}'
        f'{output_spec}'
    )


def _format_inputs_block(
    content: str,
    user_instruct: str,
) -> str:
    return (
        'Input information:\n'
        '1) Current content (full old text):\n'
        f'{content}\n\n'
        f'2) user_instruct (direct user instruction):\n{user_instruct}\n\n'
    )


def _format_retry_note(previous_error: Optional[str]) -> str:
    if not previous_error:
        return ''
    if 'replace_text could not find' in previous_error or "field 'old'" in previous_error:
        return (
            f'\nPrevious output was invalid, error: {previous_error}\n'
            'Correction requirement: do not retry with any replace_text operation unless each old value is copied '
            'verbatim from current content and can be found by exact plain string search. If you cannot guarantee '
            'a safe local edit, output full {"content": "..."} instead of operations.\n'
        )
    return f'\nPrevious output was invalid, error: {previous_error}\nPlease correct and regenerate.\n'


def _managed_content_governance_note(
    content: str,
    limit: int,
) -> str:
    current_length = _compact_len(content)
    remaining = limit - current_length
    return (
        f'- Current content length after removing whitespace: {current_length} characters.\n'
        f'- Remaining budget before applying user_instruct: {remaining} characters.\n'
        '- Treat existing content as a bounded, continuously maintained store, not an append-only log.\n'  # noqa: E501
        '- Outdated=TRUE is only one stale signal when it appears inside user_instruct; also remove or rewrite existing content that is proven outdated, wrong, conflicting, redundant, overly specific, or low-value based on user_instruct or current context.\n'  # noqa: E501
        '- Even when the limit is not exceeded, proactively compress, consolidate, or delete stale information instead of preserving it by default.\n'  # noqa: E501
        '- Add new information only after resolving stale or conflicting old information; keep the final content concise and useful.\n'  # noqa: E501
    )


def _build_skill_prompt(
    content: str,
    user_instruct: str,
    previous_error: Optional[str] = None,
) -> str:
    return (
        'You are a SKILL.md editor. Generate a JSON draft update based on the input; no explanations or summaries.\n'  # noqa: E501
        'memory type: skill\n'
        'SKILL.md is an abstract SOP (Standard Operating Procedure) that guides the agent to complete tasks '
        'using a unified methodology when the description scope is satisfied.\n'
        '\n'
        '[Format requirements]\n'
        '1. Must start with YAML frontmatter containing at least name and description fields, '
        'followed by a blank line, then the markdown body.\n'
        '2. Keep the existing name value; do not rename unless user_instruct explicitly requests it.\n'
        '3. description should describe the applicable scope and trigger conditions in one sentence; '
        'this is the sole basis for routing/recalling this skill.\n'
        '\n'
        '[Scope and description linkage (important)]\n'
        '- When user_instruct involves expanding/narrowing/adjusting the skill scope, trigger scenarios, or coverage, '  # noqa: E501
        'update the frontmatter description accordingly to accurately reflect the new scope.\n'
        '- When changes only affect methodology details in the body without changing the scope, keep description unchanged.\n'  # noqa: E501
        '- When the requested change is only deleting or editing one body line, do NOT update frontmatter description, title, tags, version, author, created, or updated.\n'  # noqa: E501
        '\n'
        '[Body content rules]\n'
        '- replace_text is the primary edit path for skill drafts. Prefer multiple small replace_text operations over full replacement whenever the exact targets exist in current content.\n'  # noqa: E501
        '- Apply only the exact target explicitly requested by user_instruct. Do not infer related cleanup in other sections.\n'  # noqa: E501
        '- When user_instruct quotes a line, phrase, or word to remove/edit, modify only that quoted target and any necessary numbered-list renumbering.\n'  # noqa: E501
        '- Do not rewrite Usage, Examples, or neighboring sections unless a suggestion explicitly targets text in those sections.\n'  # noqa: E501
        '- Use replace_text only for exact local edits whose old text is copied verbatim from current content.\n'
        '- Keep every replace_text old value as short as safely possible: use one exact full line for line deletion/replacement, or one exact phrase/sentence for wording edits.\n'  # noqa: E501
        '- Hard limit for replace_text old: prefer 1 line; never include more than 1 newline; never exceed 200 characters unless the exact single line itself is longer.\n'  # noqa: E501
        '- Never use a whole section, a heading plus body, or multiple bullets/list items as one replace_text old. Edit each affected line or phrase separately.\n'  # noqa: E501
        '- Do not make a replace_text old value span multiple markdown sections, headings, or unrelated paragraphs. Split the change into several smaller replace_text operations instead.\n'  # noqa: E501
        '- For numbered-list deletion or insertion, use one replace_text to delete/insert the target line and separate replace_text operations to renumber each affected line; after deleting item N, renumber N+1 to N, N+2 to N+1, and so on. Never leave numbering gaps.\n'  # noqa: E501
        '- For delete/remove requests, the quoted target text may appear in old but MUST NOT appear in new. Do not add, restore, or reword text that user_instruct asks to delete.\n'  # noqa: E501
        '- Only when the user explicitly asks to delete, clear, or remove all skill content, output an empty draft via full {"content": ""} or a single replace_all operation with empty content.\n'  # noqa: E501
        '- For a request like "delete/remove this line", use replace_text only if you can copy the exact line from current content as old; otherwise use replace_all instead of fabricating old text.\n'  # noqa: E501
        '- The body must be an abstract SOP: steps, decision criteria, checklists, general rules, output format requirements, etc.\n'  # noqa: E501
        '- Do not include specific cases, project names, specific data, conversation snippets, or one-time examples in the SKILL.md body; '  # noqa: E501
        'if examples are needed, use only highly abstract placeholder illustrations.\n'
        '- If user_instruct contains specific cases, abstract the reusable experience into general rules '
        'before writing to the body; do not copy cases verbatim.\n'
        '- Recommended body structure: Applicable conditions / Steps / Judgment & validation / Common pitfalls / Output spec (trim as needed).\n'  # noqa: E501
        '\n'
        f'{_COMMON_LANGUAGE_RULES}'
        '\n'
        f'{_format_preservation_rules("body content")}'
        '\n'
        '[Length control]\n'
        '- Total length of SKILL.md (including frontmatter) must be within 2000 characters; keep it concise.\n'
        f'{_managed_content_governance_note(content, 2000)}'
        '\n'
        f'{_format_prompt_tail(content, user_instruct, _EDIT_OUTPUT_SPEC, previous_error)}'
    )


def _build_memory_prompt(
    content: str,
    user_instruct: str,
    previous_error: Optional[str] = None,
) -> str:
    return (
        'You are an agent memory editor. Generate the complete new memory content based on the input; no explanations or summaries.\n'  # noqa: E501
        'memory type: memory\n'
        "memory stores the agent's own working memory about the user across sessions, such as: when a discussion happened, "  # noqa: E501
        'what the user and agent discussed, what the user was working on, ongoing context the agent may need to recall later, and other concise session-history facts.\n'  # noqa: E501
        'The user_instruct may contain direct user edits or approved review suggestions. Treat them as candidate memory events, not final text patches.\n'  # noqa: E501
        '\n'
        '[Content boundaries]\n'
        '- Only record concise working-memory entries with future recall value; do not write raw chat logs, full transcript summaries, pure emotional expressions, or unrelated small talk.\n'  # noqa: E501
        '- Do not record user profile information (identity, role, long-term preferences, communication style, etc.) here; those belong to user_preference.\n'  # noqa: E501
        '- Each entry should be self-contained and easy to scan: prefer a time anchor when known, then state what was discussed, what the user was doing, or what active context the agent should remember.\n'  # noqa: E501
        '\n'
        '[Writing and merging rules]\n'
        '- You do NOT output final memory text directly unless you must use replace_all. Normally you output edit operations that will be applied to the existing memory inside the generate endpoint.\n'  # noqa: E501
        '- If the current memory has local wording problems, slight format drift, or a user-edited phrase that should be corrected without rewriting the whole day structure, you may use `replace_text` for a local fix.\n'  # noqa: E501
        '- Preferred final format after editing: group by day. Use one top-level bullet per day, ideally `- YYYY-MM-DD`, then summarize that day under concise sub-lines such as `用户在做:`, `我们讨论了:`, and `状态/冲突:` when needed.\n'  # noqa: E501
        '- If the exact date is unknown, use the best available time anchor such as month, week, or relative session marker, but still merge nearby events together when they clearly belong to the same day or session window.\n'  # noqa: E501
        '- Treat each approved suggestion inside user_instruct as one atomic memory event to absorb into the day summary, not as a ready-made final line that must be copied verbatim.\n'  # noqa: E501
        '- For day-level additions, use replace_text with old="" and new as one complete day block with date, doing, discussed, status, and any section replacement summary.\n'  # noqa: E501
        '- For day-level updates, either replace one exact section line or replace the exact full day block with a corrected full day block. Use replacement summaries when new information supersedes the old summary for that day.\n'  # noqa: E501
        '- When many events happen in one day, merge them into one daily entry and keep only the main threads, decisions, and follow-up context. Do not create a long bullet list of every small action.\n'  # noqa: E501
        '- When merging, deduplicate and consolidate: combine same or similar working-memory items into a more accurate statement; do not stack duplicates.\n'  # noqa: E501
        '- Conflict handling: if a new suggestion clearly supersedes an older memory on the same topic, keep only the new conclusion and record it under `状态/冲突:` as `已更新:` or `已废弃旧方案:` when useful.\n'  # noqa: E501
        '- If conflicting information is still unresolved, keep only the current best summary and mark it as `待定:` or `当前倾向:` under `状态/冲突:`.\n'  # noqa: E501
        '- Keep language concise and objective; compress aggressively so memory remains a compact aide-memoire rather than a diary.\n'  # noqa: E501
        '- `replace_text` always replaces the first matching occurrence only. If first-match replacement is unsafe or too ambiguous, use `replace_all` instead of trying to target a later occurrence.\n'  # noqa: E501
        '- Use `replace_all` only when the current content cannot be edited safely with replace_text operations.\n'  # noqa: E501
        '\n'
        f'{_COMMON_LANGUAGE_RULES}'
        '\n'
        f'{_format_preservation_rules("entries")}'
        '\n'
        '[Length control]\n'
        f'- The final content must be within {_MAX_MEMORY_CONTENT_CHARS} characters after removing all whitespace; if needed, reduce low-value details and keep only the most important concise entries.\n'  # noqa: E501
        f'{_managed_content_governance_note(content, _MAX_MEMORY_CONTENT_CHARS)}'
        '- If memory exceeds the limit, older entries before the most recent week will be summarized into a concise "一周前摘要" section after operations are applied.\n'  # noqa: E501
        '\n'
        f'{_format_prompt_tail(content, user_instruct, _MEMORY_EDIT_OUTPUT_SPEC, previous_error)}'
    )


def _build_user_preference_prompt(
    content: str,
    user_instruct: str,
    previous_error: Optional[str] = None,
) -> str:
    return (
        'You are a user_preference editor. Generate the complete new user_preference content based on the input; no explanations or summaries.\n'  # noqa: E501
        'memory type: user_preference\n'
        'user_preference stores long-term stable user profile information, such as: user identity / role / domain, '
        'long-term preferences (communication tone, output format, language, level of detail), taboos, common workflow preferences, default context assumptions, etc.\n'  # noqa: E501
        '\n'
        '[Content boundaries]\n'
        '- Only record long-term stable profile information that can be reused in every future interaction.\n'
        '- Do not record specific experiences, specific project knowledge, or one-time events here; those belong to memory.\n'  # noqa: E501
        '- Do not write as chat logs or journals; organize as itemized profile entries that the agent can quickly read.\n'  # noqa: E501
        '\n'
        '[Writing and merging rules]\n'
        '- You do NOT output final user_preference text directly unless you must use replace_all. Normally you output edit operations that will be applied to the existing user_preference inside the generate endpoint.\n'  # noqa: E501
        "- If the current text is free-form, paragraph-based, or otherwise not clearly section-structured, prefer `replace_text` with an exact old substring and the desired new substring so the user's own writing structure is preserved.\n"  # noqa: E501
        '- `replace_text` always replaces the first matching occurrence only. If first-match replacement is unsafe or not enough, use `replace_all` instead.\n'  # noqa: E501
        '- Prefer small, local `replace_text` edits over rewriting the whole text. Keep untouched user-authored wording and structure exactly as-is whenever possible.\n'  # noqa: E501
        '- When preferences conflict, the new preference should replace the old text directly, and user_instruct takes precedence.\n'  # noqa: E501
        '- Keep language concise and neutral; no anthropomorphic comments; only state factual user profile entries.\n'
        '- Use `replace_all` only when the current content cannot be edited safely with local text replacement operations.\n'  # noqa: E501
        '\n'
        f'{_COMMON_LANGUAGE_RULES}'
        '\n'
        f'{_format_preservation_rules("profile entries")}'
        '\n'
        f'{_format_prompt_tail(content, user_instruct, _USER_PREFERENCE_EDIT_OUTPUT_SPEC, previous_error)}'
    )


_PROMPT_BUILDERS = {
    'skill': _build_skill_prompt,
    'memory': _build_memory_prompt,
    'user_preference': _build_user_preference_prompt,
}


def _build_generate_prompt(
    memory_type: MemoryType,
    content: str,
    user_instruct: str,
    previous_error: Optional[str] = None,
) -> str:
    try:
        builder = _PROMPT_BUILDERS[memory_type]
    except KeyError as exc:
        raise BadRequestError(f'Unsupported memory type: {memory_type!r}') from exc
    return builder(
        content=content,
        user_instruct=user_instruct,
        previous_error=previous_error,
    )
