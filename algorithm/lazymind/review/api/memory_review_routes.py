from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lazymind.review.memory.session_review import (
    ChatMessage,
    ReviewTarget,
    generate_session_review,
)

router = APIRouter()


class SessionReviewPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str = Field(..., description='Backend session ID')
    target: ReviewTarget = Field(..., description='Review target: memory or user')
    history: List[ChatMessage] = Field(
        default_factory=list,
        description='Chat history passed by backend for review',
    )
    current_content: str = Field(
        default='',
        description='Current full target content to edit',
    )
    llm_config: Dict[str, Any] = Field(
        ...,
        description='Required per-request model configuration loaded by core for the current user',
    )

    @model_validator(mode='after')
    def validate_payload(self) -> 'SessionReviewPayload':
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


def _ok(data: Dict[str, Any]) -> Dict[str, Any]:
    return {'code': 0, 'msg': 'ok', 'data': data}


def _fail(status_code: int, msg: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={'code': status_code, 'msg': msg, 'data': None},
    )


def _run_review(payload: SessionReviewPayload):
    try:
        result = generate_session_review(
            target=payload.target,
            session_id=payload.session_id,
            history=payload.history,
            current_content=payload.current_content,
            model_config=payload.llm_config,
        )
        return _ok(result.model_dump())
    except ValueError as exc:
        return _fail(422, str(exc))
    except Exception as exc:
        return _fail(500, f'session review failed: {exc}')


@router.post(
    '/api/chat/memory_review',
    summary='Review backend-provided history for memory or user_preference edits',
)
async def memory_review(payload: SessionReviewPayload):
    return _run_review(payload)
