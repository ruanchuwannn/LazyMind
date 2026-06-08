from __future__ import annotations

from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, HTTPException
import lazyllm
from pydantic import BaseModel, ConfigDict, Field
from pydantic import model_validator

from lazymind.model_config import inject_model_config
from lazymind.review.service.memory_generate import (
    BadRequestError,
    MemoryType,
    UnprocessableContentError,
    generate_memory_content,
)

router = APIRouter()


class GeneratePayload(BaseModel):
    model_config = ConfigDict(extra='forbid')

    content: str = Field(..., description='Current full text of the target content')
    user_instruct: str = Field(..., description='Natural language instruction directly from the user')
    llm_config: Dict[str, Any] = Field(
        ...,
        description='Per-request model configuration loaded by core for the current user',
    )

    @model_validator(mode='after')
    def validate_generation_inputs(self) -> 'GeneratePayload':
        has_user_instruct = bool(self.user_instruct and self.user_instruct.strip())
        if not has_user_instruct:
            raise ValueError("'user_instruct' must be a non-empty string.")
        return self


def _init_generate_session(memory_type: MemoryType, model_config: Dict[str, Any]) -> None:
    session_id = f'{memory_type}_generate_{uuid4().hex}'
    lazyllm.globals._init_sid(sid=session_id)
    lazyllm.locals._init_sid(sid=session_id)
    inject_model_config(model_config)


def _handle_generate(memory_type: MemoryType, payload: GeneratePayload):
    try:
        _init_generate_session(memory_type, payload.llm_config)
        generated = generate_memory_content(
            memory_type=memory_type,
            content=payload.content,
            user_instruct=payload.user_instruct,
        )
        return {'content': generated}
    except BadRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnprocessableContentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'generate failed: {exc}') from exc


@router.post('/api/chat/skill/generate', summary='Generate new skill content')
async def generate_skill(payload: GeneratePayload):
    return _handle_generate('skill', payload)


@router.post('/api/chat/memory/generate', summary='Generate new memory content')
async def generate_memory(payload: GeneratePayload):
    return _handle_generate('memory', payload)


@router.post('/api/chat/user_preference/generate', summary='Generate new user_preference content')
async def generate_user_preference(payload: GeneratePayload):
    return _handle_generate('user_preference', payload)
