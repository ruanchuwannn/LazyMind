from __future__ import annotations

from typing import Any, Optional

from lazyllm import AutoModel
from lazymind.chat.engine.tools.infra import validate_skill_content

from .editors.memory import _apply_memory_edit_operations
from .editors.memory_structure import (
    _MAX_MEMORY_CONTENT_CHARS,
    _compact_memory_to_recent_week,
)
from .operations import (
    BadRequestError,
    MemoryType,
    UnprocessableContentError,
    _apply_operations,
    _compact_len,
    _extract_json_object,
)
from .prompts import _build_generate_prompt

_MAX_GENERATE_ATTEMPTS = 3


def _validate_generated_content(memory_type: MemoryType, content: Any) -> str:
    if not isinstance(content, str):
        raise UnprocessableContentError("Generated field 'content' must be a string.")

    if memory_type == 'skill':
        validation_error = validate_skill_content(content)
        if validation_error:
            raise UnprocessableContentError(
                f'Generated SKILL.md is invalid: {validation_error}'
            )
    elif memory_type == 'memory':
        content_length = _compact_len(content)
        if content_length > _MAX_MEMORY_CONTENT_CHARS:
            raise UnprocessableContentError(
                f'Generated content exceeds {_MAX_MEMORY_CONTENT_CHARS} characters '
                f'after removing whitespace; current length is {content_length}. '
                f'Reduce the content length to {_MAX_MEMORY_CONTENT_CHARS} characters '
                'or less after removing whitespace, keeping only the most important '
                'concise entries.'
            )
    return content


def _normalize_user_instruct(raw_user_instruct: Any) -> Optional[str]:
    if raw_user_instruct is None:
        return None
    if not isinstance(raw_user_instruct, str):
        raise BadRequestError("'user_instruct' must be a string when provided.")

    normalized = raw_user_instruct.strip()
    return normalized or None


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
                    edited_content = _apply_operations(
                        content,
                        parsed,
                        entity_name='user_preference',
                    )
                else:
                    edited_content = _apply_operations(
                        content,
                        parsed,
                        entity_name='skill',
                        normalize_numbered_lists_after_delete=True,
                    )
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
