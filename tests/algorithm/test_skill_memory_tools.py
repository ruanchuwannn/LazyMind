import sys
from types import ModuleType

import lazymind.chat.engine.tools.memory_editor as memory_mod
from lazymind.chat.engine.tools import skill_editor as skill_editor_mod
from lazymind.chat.engine.tools.infra.suggestion import Suggestion


def test_memory_editor_operations_write_memory_review(monkeypatch):
    assert not hasattr(memory_mod, 'memory')

    class FakeUnprocessableContentError(ValueError):
        pass

    fake_rewrite_pkg = ModuleType('lazymind.rewrite')
    fake_rewrite_pkg.__path__ = []
    fake_rewrite_base = ModuleType('lazymind.rewrite.base')
    fake_rewrite_base.UnprocessableContentError = FakeUnprocessableContentError
    fake_rewrite_base._validate_generated_content = (
        lambda memory_type, content: content
    )
    fake_rewrite_memory = ModuleType('lazymind.rewrite.memory')
    fake_rewrite_memory._apply_memory_edit_operations = (
        lambda current, payload: current.replace('old', payload['operations'][0]['new'])
    )
    fake_rewrite_preference = ModuleType('lazymind.rewrite.preference')
    fake_rewrite_preference._apply_user_preference_edit_operations = (
        lambda current, payload: current.replace('old', payload['operations'][0]['new'])
    )

    records = []
    fake_memory_db = ModuleType('lazymind.review.memory_review.db')

    def fake_insert_memory_review_record(**kwargs):
        records.append(kwargs)
        return {'id': 'review-1', 'review_status': 'pending'}

    fake_memory_db.insert_memory_review_record = fake_insert_memory_review_record

    monkeypatch.setitem(
        sys.modules,
        'lazymind.rewrite',
        fake_rewrite_pkg,
    )
    monkeypatch.setitem(
        sys.modules,
        'lazymind.rewrite.base',
        fake_rewrite_base,
    )
    monkeypatch.setitem(sys.modules, 'lazymind.rewrite.memory', fake_rewrite_memory)
    monkeypatch.setitem(sys.modules, 'lazymind.rewrite.preference', fake_rewrite_preference)
    monkeypatch.setitem(sys.modules, 'lazymind.review.memory_review.db', fake_memory_db)
    monkeypatch.setattr(
        memory_mod.lazyllm,
        'globals',
        {'agentic_config': {'user_id': 'user-1', 'memory': 'old', 'user': 'old'}},
    )

    memory_result = memory_mod.memory_editor(
        'memory',
        [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
    )
    user_result = memory_mod.memory_editor(
        'user',
        [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
    )

    assert memory_result['success'] is True
    assert memory_result['tool'] == 'memory_editor'
    assert memory_result['result']['target'] == 'memory'
    assert memory_result['result']['persisted'] == 'memory_review'
    assert user_result['success'] is True
    assert user_result['tool'] == 'memory_editor'
    assert user_result['result']['target'] == 'user'
    assert user_result['result']['storage_target'] == 'user_preference'
    assert records == [
        {
            'target': 'memory',
            'user_id': 'user-1',
            'source_content': 'old',
            'content': 'new',
            'operations': [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
        },
        {
            'target': 'user_preference',
            'user_id': 'user-1',
            'source_content': 'old',
            'content': 'new',
            'operations': [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
        },
    ]


def test_skill_editor_create_modify_remove_use_core_api_paths(monkeypatch):
    calls = []

    def fake_post_core_api(path, payload):
        calls.append((path, payload))
        return {'persisted': 'core_api', 'url': f'http://core{path}'}

    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {'session_id': 'sid-1'}})
    monkeypatch.setattr(skill_editor_mod, 'post_core_api', fake_post_core_api)
    monkeypatch.setattr(
        skill_editor_mod,
        'list_all_skill_entries',
        lambda _base_dir: {
            'writing/existing': {
                'name': 'existing',
                'category': 'writing',
                'path': '/tmp/skills/writing/existing',
                'source': 'remote',
            }
        },
    )

    content = (
        '---\n'
        'name: new_skill\n'
        'description: A test skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    suggestion = Suggestion(title='Update instructions', content='Tighten the wording.')

    create_result = skill_editor_mod.skill_editor(
        'new_skill',
        'create',
        category='drafts',
        content=content,
    )
    modify_result = skill_editor_mod.skill_editor(
        'existing',
        'modify',
        category='writing',
        suggestions=[suggestion],
    )
    remove_result = skill_editor_mod.skill_editor('existing', 'remove', category='writing')

    assert create_result['success'] is True
    assert create_result['tool'] == 'skill_editor'
    assert modify_result['success'] is True
    assert modify_result['tool'] == 'skill_editor'
    assert remove_result['success'] is True
    assert remove_result['tool'] == 'skill_editor'
    assert calls == [
        (
            '/skill/create',
            {
                'session_id': 'sid-1',
                'category': 'drafts',
                'skill_name': 'new_skill',
                'content': content,
            },
        ),
        (
            '/skill/suggestion',
            {
                'session_id': 'sid-1',
                'skill_name': 'existing',
                'category': 'writing',
                'suggestions': [{'title': 'Update instructions', 'content': 'Tighten the wording.'}],
            },
        ),
        (
            '/skill/remove',
            {'session_id': 'sid-1', 'skill_name': 'existing', 'category': 'writing', 'reason': ''},
        ),
    ]


def test_skill_editor_rejects_missing_skill_without_post(monkeypatch):
    calls = []

    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {'session_id': 'sid-1'}})
    monkeypatch.setattr(skill_editor_mod, 'post_core_api', lambda path, payload: calls.append((path, payload)))
    monkeypatch.setattr(skill_editor_mod, 'list_all_skill_entries', lambda _base_dir: {})

    result = skill_editor_mod.skill_editor(
        'missing',
        'modify',
        category='writing',
        suggestions=[{'title': 'Update instructions', 'content': 'Tighten the wording.'}],
    )

    assert result == {
        'success': False,
        'tool': 'skill_editor',
        'error': {
            'reason': "Skill 'missing' does not exist in category 'writing'; use action='create' to add a new skill.",
        },
    }
    assert calls == []
