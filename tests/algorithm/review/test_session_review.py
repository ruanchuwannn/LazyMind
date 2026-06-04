from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any


_ALGO = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'algorithm')
_LAZYLLM_ROOT = os.path.join(_ALGO, 'lazyllm')
if _ALGO not in sys.path:
    sys.path.insert(0, _ALGO)
if _LAZYLLM_ROOT not in sys.path:
    sys.path.insert(0, _LAZYLLM_ROOT)


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []
    return module


def _load_session_review_module():
    module_path = Path(_ALGO) / 'lazymind/review/memory/session_review.py'
    spec = importlib.util.spec_from_file_location('test_session_review_module', module_path)
    assert spec is not None
    assert spec.loader is not None

    fake_prompts = ModuleType('lazymind.review.prompts')
    fake_prompts.MEMORY_REVIEW_PROMPT = 'MEMORY REVIEW PROMPT'
    fake_modules = {
        'lazymind': _package('lazymind'),
        'lazymind.review': _package('lazymind.review'),
        'lazymind.review.prompts': fake_prompts,
    }
    original_modules = {name: sys.modules.get(name) for name in fake_modules}

    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules.update(fake_modules)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _install_runtime_modules(monkeypatch, *, tools: ModuleType, config: Any) -> None:
    monkeypatch.setitem(sys.modules, 'lazymind', _package('lazymind'))
    monkeypatch.setitem(sys.modules, 'lazymind.chat', _package('lazymind.chat'))
    monkeypatch.setitem(sys.modules, 'lazymind.chat.engine', _package('lazymind.chat.engine'))
    monkeypatch.setitem(sys.modules, 'lazymind.chat.engine.tools', tools)
    monkeypatch.setitem(sys.modules, 'lazymind.config', config)
    monkeypatch.setitem(
        sys.modules,
        'lazymind.model_config',
        SimpleNamespace(get_config_path=lambda: '/tmp/config.yaml'),
    )


def test_memory_review_prompt_excludes_preferences_and_workflows():
    session_review = _load_session_review_module()

    prompt = session_review.build_session_review_prompt(
        target='memory',
        current_content='',
    )

    assert 'ONLY for agent working memory' in prompt
    assert "memory_editor(target='memory'" in prompt
    assert 'operations' in prompt
    assert 'Prefer replace_text whenever current content is non-empty' in prompt
    assert 'Determine the language of new or rewritten memory/user profile content from current_content and llm_chat_history' in prompt
    assert "If current_content is empty, use the dominant language of the user's messages in llm_chat_history" in prompt
    assert 'do not switch to English just because these instructions are written in English' in prompt
    assert 'Use only target and operations' in prompt
    assert 'Environment context' not in prompt
    assert 'Do NOT save multi-step reusable workflows' in prompt
    assert 'reusable workflows' in prompt


def test_user_review_prompt_excludes_session_history():
    session_review = _load_session_review_module()

    prompt = session_review.build_session_review_prompt(
        target='user',
        current_content='',
    )

    assert 'ONLY for user profile / preference content' in prompt
    assert "memory_editor(target='user'" in prompt
    assert "Do not call memory_editor with target='memory'" in prompt


def test_review_session_runs_agent_with_memory_editor_tool(monkeypatch):
    session_review = _load_session_review_module()
    ChatMessage = session_review.ChatMessage
    SessionReviewRequest = session_review.SessionReviewRequest

    calls = {}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            calls['model_args'] = (args, kwargs)

    class FakeReactAgent:
        def __init__(self, **kwargs):
            calls['agent_kwargs'] = kwargs

        def __call__(self, prompt, llm_chat_history=None):
            calls['prompt'] = prompt
            calls['history'] = llm_chat_history
            fake_lazyllm.locals['_lazyllm_agent'] = {
                'completed': [
                    {
                        'function': {'name': 'memory_editor'},
                        'tool_call_result': {
                            'success': True,
                            'result': {'persisted': 'memory_review'},
                        },
                    }
                ],
            }
            return '已保存。'

    fake_lazyllm = SimpleNamespace(
        AutoModel=FakeModel,
        globals={},
        locals={'_lazyllm_agent': {'completed': [{'stale': True}]}},
        tools=SimpleNamespace(agent=SimpleNamespace(ReactAgent=FakeReactAgent)),
    )
    fake_fs_module = SimpleNamespace(FS=object)
    fake_tools_pkg = ModuleType('lazymind.chat.engine.tools')

    def memory_editor(*args, **kwargs):
        return None

    fake_tools_pkg.memory_editor = memory_editor
    fake_config = SimpleNamespace(config={'core_api_url': 'http://core', 'review_max_retries': 2})
    monkeypatch.setitem(sys.modules, 'lazyllm', fake_lazyllm)
    monkeypatch.setitem(sys.modules, 'lazyllm.tools.fs.client', fake_fs_module)
    _install_runtime_modules(monkeypatch, tools=fake_tools_pkg, config=fake_config)

    result = session_review.review_session(
        SessionReviewRequest(
            target='user',
            session_id='sid-1',
            history=[ChatMessage(role='user', content='以后请用中文简洁回答')],
        )
    )

    assert result.submitted is True
    assert [tool.__name__ for tool in calls['agent_kwargs']['tools']] == ['memory_editor']
    assert calls['history'] == [{'role': 'user', 'content': '以后请用中文简洁回答'}]
    assert fake_lazyllm.globals['agentic_config']['session_id'] == 'sid-1'
    assert fake_lazyllm.globals['agentic_config']['user'] == ''
    assert fake_lazyllm.globals['agentic_config']['user_preference'] == ''


def test_review_session_reports_no_tool_submission(monkeypatch):
    session_review = _load_session_review_module()
    ChatMessage = session_review.ChatMessage
    SessionReviewRequest = session_review.SessionReviewRequest

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

    class FakeReactAgent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt, llm_chat_history=None):
            return 'Nothing to save.'

    fake_lazyllm = SimpleNamespace(
        AutoModel=FakeModel,
        globals={},
        locals={'_lazyllm_agent': {}},
        tools=SimpleNamespace(agent=SimpleNamespace(ReactAgent=FakeReactAgent)),
    )
    monkeypatch.setitem(sys.modules, 'lazyllm', fake_lazyllm)
    monkeypatch.setitem(sys.modules, 'lazyllm.tools.fs.client', SimpleNamespace(FS=object))
    fake_tools_pkg = ModuleType('lazymind.chat.engine.tools')

    def memory_editor(*args, **kwargs):
        return None

    fake_tools_pkg.memory_editor = memory_editor
    _install_runtime_modules(
        monkeypatch,
        tools=fake_tools_pkg,
        config=SimpleNamespace(config={'core_api_url': 'http://core', 'review_max_retries': 2}),
    )

    result = session_review.review_session(
        SessionReviewRequest(
            target='memory',
            session_id='sid-1',
            history=[ChatMessage(role='user', content='你好')],
        )
    )

    assert result.submitted is False
    assert result.agent_result == 'Nothing to save.'


def test_memory_editor_submission_can_be_read_from_tool_history():
    session_review = _load_session_review_module()

    assert session_review._memory_editor_submitted(
        {
            'history': [
                {
                    'role': 'tool',
                    'name': 'memory_editor',
                    'content': (
                        "{'success': True, "
                        "'result': {'persisted': 'memory_review'}}"
                    ),
                }
            ]
        }
    )


def test_failed_memory_editor_result_is_not_submitted():
    session_review = _load_session_review_module()

    assert not session_review._memory_editor_submitted(
        {
            'completed': [
                {
                    'function': {'name': 'memory_editor'},
                    'tool_call_result': {
                        'success': False,
                        'reason': 'session snapshot not found',
                    },
                }
            ]
        }
    )
