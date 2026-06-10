from __future__ import annotations

from typing import Any, Dict, List, Literal
from uuid import uuid4

import lazyllm
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lazymind.model_config import inject_model_config
from lazymind.review.memory_review.prompts import build_memory_review_prompt


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra='allow')

    role: str = Field(
        ...,
        description='Message role, such as user, assistant, tool, or system',
    )
    content: str = Field(default='', description='Message content')


class MemoryReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str = Field(..., description='Backend session ID being reviewed')
    history: List[ChatMessage] = Field(
        default_factory=list,
        description='Chat history passed by backend for review',
    )
    memory: str = Field(
        default='',
        description='Current full agent memory text to edit',
    )
    user: str = Field(
        default='',
        description='Current full user profile text to edit',
    )

    @model_validator(mode='after')
    def validate_history(self) -> 'MemoryReviewRequest':
        if not self.session_id.strip():
            raise ValueError("'session_id' must be non-empty.")
        if not any(
            message.role == 'user' and message.content.strip()
            for message in self.history
        ):
            raise ValueError("'history' must contain at least one user message.")
        return self


class MemoryReviewResult(BaseModel):
    model_config = ConfigDict(extra='forbid')

    status: Literal['success', 'failed']


def review_memory(request: MemoryReviewRequest) -> MemoryReviewResult:
    from lazyllm import AutoModel
    from lazyllm.tools.fs.client import FS

    from lazymind.chat.engine.tools import memory_editor
    from lazymind.chat.service.component.history import normalize_history_for_agent
    from lazymind.config import config as _cfg
    from lazymind.model_config import get_config_path

    prompt = build_memory_review_prompt(
        memory=request.memory,
        user=request.user,
    )

    config = {
        'session_id': request.session_id,
        'core_api_url': _cfg['core_api_url'],
        'memory': request.memory,
        'user': request.user,
    }
    lazyllm.globals['agentic_config'] = config

    llm = AutoModel(model='llm', config=get_config_path())
    review_agent = lazyllm.tools.agent.ReactAgent(
        llm=llm,
        tools=[memory_editor],
        max_retries=_cfg['review_max_retries'],
        return_trace=False,
        prompt=' ',
        keep_full_turns=3,
        fs=FS,
        enable_builtin_tools=False,
        force_summarize=True,
    )
    lazyllm.locals['_lazyllm_agent'] = {}
    review_agent(
        prompt,
        llm_chat_history=normalize_history_for_agent(
            [message.model_dump() for message in request.history]
        ),
    )
    return MemoryReviewResult(status='success')


def generate_memory_review(
    *,
    session_id: str,
    history: List[ChatMessage],
    memory: str,
    user: str,
    model_config: Dict[str, Any],
) -> MemoryReviewResult:
    sid = f'memory_review_{session_id.strip() or uuid4().hex}'
    lazyllm.globals._init_sid(sid=sid)
    lazyllm.locals._init_sid(sid=sid)
    inject_model_config(model_config)
    request = MemoryReviewRequest(
        session_id=session_id,
        history=history,
        memory=memory,
        user=user,
    )
    return review_memory(request)
