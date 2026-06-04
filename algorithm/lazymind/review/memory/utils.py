from __future__ import annotations

import ast
from typing import Any, Dict, Iterable


def iter_tool_traces(agent_state: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    completed = agent_state.get('completed') or []
    if isinstance(completed, list):
        for item in completed:
            if isinstance(item, dict):
                yield item

    workspace = agent_state.get('workspace') or {}
    if isinstance(workspace, dict):
        trace = workspace.get('tool_call_trace') or []
        if isinstance(trace, list):
            for item in trace:
                if isinstance(item, dict):
                    yield item

    history = agent_state.get('history') or []
    if isinstance(history, list):
        for item in history:
            if not isinstance(item, dict) or item.get('role') != 'tool':
                continue
            yield {
                'function': {'name': item.get('name')},
                'tool_call_result': parse_tool_result(item.get('content')),
            }


def parse_tool_result(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value


def is_successful_memory_editor_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get('success') is not True:
        return False

    payload = result.get('result')
    if isinstance(payload, dict):
        return payload.get('persisted') in {'core_api', 'memory_review'}
    return result.get('persisted') in {'core_api', 'memory_review'}


def memory_editor_submitted(agent_state: Dict[str, Any]) -> bool:
    for trace in iter_tool_traces(agent_state):
        function = trace.get('function') or {}
        if not isinstance(function, dict) or function.get('name') != 'memory_editor':
            continue
        if is_successful_memory_editor_result(trace.get('tool_call_result')):
            return True
    return False


def reset_agent_tool_trace(lazyllm: Any) -> None:
    lazyllm.locals['_lazyllm_agent'] = {}


__all__ = [
    'is_successful_memory_editor_result',
    'iter_tool_traces',
    'memory_editor_submitted',
    'parse_tool_result',
    'reset_agent_tool_trace',
]
