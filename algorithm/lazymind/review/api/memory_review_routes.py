from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from lazyllm import LOG
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lazymind.review.service.memory_review import (
    ChatMessage,
    MemoryReviewResult,
    generate_memory_review,
)

router = APIRouter()


class MemoryReviewPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str = Field(..., description='Backend session ID')
    history: List[ChatMessage] = Field(
        default_factory=list,
        description='Chat history passed by backend for review',
    )
    memory: str = Field(
        default='',
        description='Current full agent memory content to edit',
    )
    user: str = Field(
        default='',
        description='Current full user profile content to edit',
    )
    llm_config: Dict[str, Any] = Field(
        ...,
        description='Required per-request model configuration loaded by core for the current user',
    )

    @model_validator(mode='after')
    def validate_payload(self) -> 'MemoryReviewPayload':
        if not self.session_id.strip():
            raise ValueError("'session_id' must be non-empty.")
        if not any(
            message.role == 'user' and message.content.strip()
            for message in self.history
        ):
            raise ValueError("'history' must contain at least one user message.")
        if not self.llm_config:
            raise ValueError("'llm_config' must be a non-empty object.")
        return self


@router.post(
    '/api/chat/memory_review',
    summary='Review backend-provided history for memory or user_preference edits',
    response_model=MemoryReviewResult,
)
async def memory_review(payload: MemoryReviewPayload):
    try:
        result = generate_memory_review(
            session_id=payload.session_id,
            history=payload.history,
            memory=payload.memory,
            user=payload.user,
            model_config=payload.llm_config,
        )
    except Exception as exc:
        LOG.exception(f'[MemoryReview] memory review failed: {exc}')
        return JSONResponse(status_code=500, content={'status': 'failed'})
    return result.model_dump()
