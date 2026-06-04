from __future__ import annotations

from typing import Any, Dict, List, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lazymind.review.memory.prompts import build_session_review_prompt
from lazymind.review.memory.utils import (
    memory_editor_submitted,
    reset_agent_tool_trace,
)

ReviewTarget = Literal['memory', 'user']


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra='allow')

    role: str = Field(
        ...,
        description='Message role, such as user, assistant, tool, or system',
    )
    content: str = Field(default='', description='Message content')


class SessionReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str = Field(..., description='Backend session ID being reviewed')
    target: ReviewTarget = Field(..., description='Review target')
    history: List[ChatMessage] = Field(
        default_factory=list,
        description='Chat history passed by backend for review',
    )
    current_content: str = Field(
        default='',
        description='Current full memory or user profile text to edit',
    )

    @model_validator(mode='after')
    def validate_history(self) -> 'SessionReviewRequest':
        if not self.session_id.strip():
            raise ValueError("'session_id' must be non-empty.")
        if not any(
            message.role == 'user' and message.content.strip()
            for message in self.history
        ):
            raise ValueError("'history' must contain at least one user message.")
        return self


class SessionReviewResult(BaseModel):
    model_config = ConfigDict(extra='forbid')

    target: ReviewTarget
    session_id: str
    submitted: bool = False
    agent_result: str = ''


def review_session(request: SessionReviewRequest) -> SessionReviewResult:
    import lazyllm
    from lazyllm import AutoModel
    from lazyllm.tools.fs.client import FS

    from lazymind.chat.engine.tools import memory_editor
    from lazymind.config import config as _cfg
    from lazymind.model_config import get_config_path

    prompt = build_session_review_prompt(
        target=request.target,
        current_content=request.current_content,
    )

    config = {
        'session_id': request.session_id,
        'core_api_url': _cfg['core_api_url'],
        'current_content': request.current_content,
    }
    if request.target == 'memory':
        config['memory'] = request.current_content
    else:
        config['user'] = request.current_content
        config['user_preference'] = request.current_content
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
    reset_agent_tool_trace(lazyllm)
    raw = review_agent(
        prompt,
        llm_chat_history=[message.model_dump() for message in request.history],
    )
    agent_result = raw if isinstance(raw, str) else str(raw)
    agent_state = lazyllm.locals.get('_lazyllm_agent', {})
    return SessionReviewResult(
        target=request.target,
        session_id=request.session_id,
        submitted=memory_editor_submitted(agent_state if isinstance(agent_state, dict) else {}),
        agent_result=agent_result,
    )


def _init_review_session(
    session_id: str,
    target: ReviewTarget,
    model_config: Dict[str, Any],
) -> None:
    import lazyllm
    from lazymind.model_config import inject_model_config

    sid = f'{target}_review_{session_id.strip() or uuid4().hex}'
    lazyllm.globals._init_sid(sid=sid)
    lazyllm.locals._init_sid(sid=sid)
    inject_model_config(model_config)


def generate_session_review(
    *,
    target: ReviewTarget,
    session_id: str,
    history: List[ChatMessage],
    current_content: str,
    model_config: Dict[str, Any],
) -> SessionReviewResult:
    _init_review_session(session_id, target, model_config)
    request = SessionReviewRequest(
        target=target,
        session_id=session_id,
        history=history,
        current_content=current_content,
    )
    return review_session(request)
