from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

from lazyllm import AutoModel
from lazymind.chat.engine.tools.infra import validate_skill_content

try:
    from json_repair import repair_json as _repair_json  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    _repair_json = None

MemoryType = Literal['skill', 'memory', 'user_preference']

_MAX_GENERATE_ATTEMPTS = 3
_MAX_MANAGED_CONTENT_CHARS = 1500
_MAX_OLDER_MEMORY_SUMMARY_CHARS = 500
_JSON_BLOCK_RE = re.compile(r'```json\s*(.*?)\s*```', re.DOTALL)
_THINK_BLOCK_RE = re.compile(r'<think>.*?</think\s*>', re.DOTALL | re.IGNORECASE)
_SINGLE_STRING_FIELD_RE = re.compile(
    r'^\{\s*"(?P<key>[^"\\]+)"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"\s*,?\s*\}\s*$',
    re.DOTALL,
)
_DATE_BULLET_RE = re.compile(r'^-\s+(.+?)(?::\s*(.*))?$')
_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_SECTION_HEADER_TO_KEY = OrderedDict((
    ('用户在做', 'doing'),
    ('我们讨论了', 'discussed'),
    ('状态/冲突', 'status'),
))
_SECTION_KEY_TO_HEADER = {v: k for k, v in _SECTION_HEADER_TO_KEY.items()}
_MEMORY_SECTION_KEYS = tuple(_SECTION_KEY_TO_HEADER.keys())


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


def _validate_generated_content(memory_type: MemoryType, content: Any) -> str:
    if not isinstance(content, str):
        raise UnprocessableContentError("Generated field 'content' must be a string.")

    if memory_type == 'skill':
        validation_error = validate_skill_content(content)
        if validation_error:
            raise UnprocessableContentError(
                f'Generated SKILL.md is invalid: {validation_error}'
            )
    elif memory_type in ('memory', 'user_preference'):
        compact_content = ''.join(content.split())
        content_length = len(compact_content)
        if content_length > _MAX_MANAGED_CONTENT_CHARS:
            raise UnprocessableContentError(
                f'Generated content exceeds {_MAX_MANAGED_CONTENT_CHARS} characters '
                f'after removing whitespace; current length is {content_length}. '
                f'Reduce the content length to {_MAX_MANAGED_CONTENT_CHARS} characters '
                'or less after removing whitespace, keeping only the most important '
                'concise entries.'
            )
    return content


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


def _normalize_user_instruct(raw_user_instruct: Any) -> Optional[str]:
    if raw_user_instruct is None:
        return None
    if not isinstance(raw_user_instruct, str):
        raise BadRequestError("'user_instruct' must be a string when provided.")

    normalized = raw_user_instruct.strip()
    return normalized or None


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


def _compact_len(text: Any) -> int:
    return len(''.join(str(text).split()))


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
        f'- The final content must be within {_MAX_MANAGED_CONTENT_CHARS} characters after removing all whitespace; if needed, reduce low-value details and keep only the most important concise entries.\n'  # noqa: E501
        f'{_managed_content_governance_note(content, _MAX_MANAGED_CONTENT_CHARS)}'
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
        '[Length control]\n'
        f'- The final content must be within {_MAX_MANAGED_CONTENT_CHARS} characters after removing all whitespace; if needed, reduce low-value details and keep only the most important concise entries.\n'  # noqa: E501
        f'{_managed_content_governance_note(content, _MAX_MANAGED_CONTENT_CHARS)}'
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


class MemoryGeneratePipeline:
    def __init__(self) -> None:
        self.llm = AutoModel(model='llm')

    def generate(
        self,
        memory_type: MemoryType,
        content: Any,
        user_instruct: Any,
    ) -> str:
        if not isinstance(content, str):
            raise BadRequestError("'content' is required and must be a string.")

        normalized_user_instruct = _normalize_user_instruct(user_instruct)
        if normalized_user_instruct is None:
            raise BadRequestError("'user_instruct' must be a non-empty string.")

        if memory_type == 'memory':
            content = _compact_memory_to_recent_week(content)

        error: Optional[str] = None
        for _ in range(_MAX_GENERATE_ATTEMPTS):
            prompt = _build_generate_prompt(
                memory_type=memory_type,
                content=content,
                user_instruct=normalized_user_instruct,
                previous_error=error,
            )
            raw = self.llm(prompt)
            try:
                parsed = _extract_json_object(raw)
                if memory_type == 'memory':
                    edited_content = _apply_memory_edit_operations(content, parsed)
                elif memory_type == 'user_preference':
                    edited_content = _apply_user_preference_edit_operations(content, parsed)
                else:
                    edited_content = _apply_skill_edit_operations(content, parsed)
                return _validate_generated_content(memory_type, edited_content)
            except UnprocessableContentError as exc:
                error = str(exc)

        raise UnprocessableContentError(
            f'Failed to generate valid content after {_MAX_GENERATE_ATTEMPTS} attempts: {error}'
        )


memory_generate_pipeline = MemoryGeneratePipeline()


def generate_memory_content(
    memory_type: MemoryType,
    content: Any,
    user_instruct: Any,
) -> str:
    return memory_generate_pipeline.generate(
        memory_type=memory_type,
        content=content,
        user_instruct=user_instruct,
    )


def _parse_edit_operations(payload: Dict[str, Any], *, entity_name: str) -> List[Dict[str, Any]]:
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
            if old == '' and new != '':
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


def _apply_replace_text_operation(current: str, old: str, new: str, *, entity_name: str) -> str:
    replacement = '' if not new.strip() else new
    if not replacement:
        lines = current.splitlines()
        for idx, line in enumerate(lines):
            if line == old:
                return '\n'.join(lines[:idx] + lines[idx + 1:])
    return _apply_replace_text(current, old, replacement, entity_name=entity_name)


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


def _apply_skill_edit_operations(current_content: str, payload: Dict[str, Any]) -> str:
    operations = _parse_edit_operations(payload, entity_name='skill')
    if operations[0]['op'] == 'replace_all':
        return operations[0]['content']

    current = current_content
    applied_delete = False
    for op in operations:
        if op['old'] == op['new']:
            continue
        current = _apply_replace_text_operation(
            current,
            op['old'],
            op['new'],
            entity_name='skill',
        )
        if not op['new'].strip():
            applied_delete = True
    if applied_delete:
        current = _normalize_numbered_lists(current)
    return current.strip()


def _normalize_string_list(raw: Any, *, field_name: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise UnprocessableContentError(f"Operation field '{field_name}' must be an array of strings.")

    normalized: List[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise UnprocessableContentError(
                f"Operation field '{field_name}[{idx}]' must be a non-empty string."
            )
        value = item.strip()
        if value not in normalized:
            normalized.append(value)
    return normalized


def _parse_memory_operations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if 'content' in payload and 'operations' not in payload:
        content = payload.get('content')
        if not isinstance(content, str):
            raise UnprocessableContentError("Generated field 'content' must be a string.")
        return [{'op': 'replace_all', 'content': content.strip()}]

    operations = payload.get('operations')
    if not isinstance(operations, list) or not operations:
        raise UnprocessableContentError("Model output for memory must contain a non-empty 'operations' array.")

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
            normalized_ops.append({
                'op': 'replace_text',
                'old': old,
                'new': new,
            })
            continue
        raise UnprocessableContentError(
            f"Unsupported memory operation {op_name!r}; expected 'replace_text' or 'replace_all'."
        )

    return normalized_ops


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
    if _compact_len(content) <= _MAX_MANAGED_CONTENT_CHARS:
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
            max(0, _MAX_MANAGED_CONTENT_CHARS - _compact_len(recent_text) - 20),
        )
        if older_summary and summary_budget > 0:
            result_days['一周前摘要'] = _new_day_record()
            result_days['一周前摘要']['discussed'] = [
                _trim_text_to_chars(older_summary, summary_budget)
            ]

    result_days.update(recent)
    result = _render_memory(result_days)
    return result or content.strip()


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


def _apply_memory_replace_text_operation(current: str, old: str, new: str) -> str:
    if old == '' and new.strip():
        return _insert_or_replace_memory_day_block(current, new)
    if old not in current and _extract_memory_day_date(new):
        return _insert_or_replace_memory_day_block(current, new)
    return _apply_replace_text_operation(current, old, new, entity_name='memory')


def _apply_memory_edit_operations(current_content: str, payload: Dict[str, Any]) -> str:
    operations = _parse_memory_operations(payload)
    if operations[0]['op'] == 'replace_all':
        return _compact_memory_to_recent_week(operations[0]['content'])

    current = _compact_memory_to_recent_week(current_content)
    for op in operations:
        if op['op'] == 'replace_text':
            if op['old'] == op['new']:
                continue
            current = _apply_memory_replace_text_operation(current, op['old'], op['new'])

    return _compact_memory_to_recent_week(current.strip())


def _parse_user_preference_operations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if 'content' in payload and 'operations' not in payload:
        content = payload.get('content')
        if not isinstance(content, str):
            raise UnprocessableContentError("Generated field 'content' must be a string.")
        return [{'op': 'replace_all', 'content': content.strip()}]

    operations = payload.get('operations')
    if not isinstance(operations, list) or not operations:
        raise UnprocessableContentError("Model output for user_preference must contain a non-empty 'operations' array.")

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
            if old == '' and new != '':
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
            f"Unsupported user_preference operation {op_name!r}; expected 'replace_text' or 'replace_all'."
        )
    return normalized_ops


def _apply_user_preference_edit_operations(current_content: str, payload: Dict[str, Any]) -> str:
    operations = _parse_user_preference_operations(payload)
    if operations[0]['op'] == 'replace_all':
        return operations[0]['content']

    current = current_content
    for op in operations:
        if op['op'] == 'replace_text':
            if op['old'] == op['new']:
                continue
            current = _apply_replace_text_operation(
                current,
                op['old'],
                op['new'],
                entity_name='user_preference',
            )
    return current.strip()


def _apply_replace_text(current: str, old: str, new: str, *, entity_name: str) -> str:
    if old not in current:
        raise UnprocessableContentError(
            f"replace_text could not find the requested 'old' substring in current {entity_name} content. "
            'Please correct the old text or use replace_all if necessary.'
        )
    return current.replace(old, new, 1)
