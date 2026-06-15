import sys
from types import ModuleType

import lazymind.chat.engine.tools.memory_editor as memory_mod
import lazymind.chat.engine.tools.skill_editor as skill_editor_mod


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


def test_skill_editor_create_modify_remove_write_skill_review_results(monkeypatch):
    records = []
    delete_calls = []

    def fake_insert_skill_review_result(**kwargs):
        records.append(kwargs)
        return {
            'id': f'review-{len(records)}',
            'review_status': 'pending',
            'type': kwargs['review_type'],
        }

    def fake_mark_skill_review_delete(**kwargs):
        delete_calls.append(kwargs)
        return {'id': 'review-delete', 'review_status': 'pending', 'type': 'delete'}

    def fake_apply_skill_edit_operations(current, operations):
        return (
            current.replace('Use this skill for tests.', 'Use this skill for focused tests.'),
            [dict(op) for op in operations],
        )

    monkeypatch.setattr(
        skill_editor_mod.lazyllm,
        'globals',
        {'agentic_config': {'user_id': 'user-1'}},
    )
    monkeypatch.setattr(skill_editor_mod, 'insert_skill_review_result', fake_insert_skill_review_result)
    monkeypatch.setattr(skill_editor_mod, 'mark_skill_review_delete', fake_mark_skill_review_delete)
    monkeypatch.setattr(skill_editor_mod, 'find_pending_skill_review', lambda category, name: None)
    monkeypatch.setattr(skill_editor_mod, 'apply_skill_edit_operations', fake_apply_skill_edit_operations)

    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'list_all_skill_entries',
        lambda _base_dir: {
            'writing/existing': {
                'name': 'existing',
                'category': 'writing',
                'path': '/tmp/skills/writing/existing',
                'source': 'remote',
                'content': existing_content,
            }
        },
    )

    content = (
        '---\n'
        'name: new_skill\n'
        'category: drafts\n'
        'description: A test skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
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
        operations=[
            {
                'op': 'replace_text',
                'old': 'Use this skill for tests.',
                'new': 'Use this skill for focused tests.',
            }
        ],
    )
    remove_result = skill_editor_mod.skill_editor('existing', 'remove', category='writing')

    assert create_result['success'] is True
    assert create_result['tool'] == 'skill_editor'
    assert modify_result['success'] is True
    assert modify_result['tool'] == 'skill_editor'
    assert remove_result['success'] is True
    assert remove_result['tool'] == 'skill_editor'
    assert create_result['result']['persisted'] == 'skill_review_results'
    assert modify_result['result']['persisted'] == 'skill_review_results'
    assert remove_result['result']['type'] == 'delete'
    assert records == [
        {
            'category': 'drafts',
            'skill_name': 'new_skill',
            'review_type': 'new',
            'skill_content': content,
            'user_id': 'user-1',
        },
        {
            'category': 'writing',
            'skill_name': 'existing',
            'review_type': 'patch',
            'skill_content': existing_content.replace(
                'Use this skill for tests.',
                'Use this skill for focused tests.',
            ),
            'user_id': 'user-1',
            'summary': 'skill_editor operations: 1',
        },
    ]
    assert delete_calls == [
        {
            'category': 'writing',
            'skill_name': 'existing',
            'user_id': 'user-1',
            'summary': None,
        }
    ]


def test_skill_editor_rejects_missing_skill_without_write(monkeypatch):
    calls = []

    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(skill_editor_mod, 'insert_skill_review_result', lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(skill_editor_mod, 'list_all_skill_entries', lambda _base_dir: {})

    result = skill_editor_mod.skill_editor(
        'missing',
        'modify',
        category='writing',
        operations=[{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
    )

    assert result == {
        'success': False,
        'tool': 'skill_editor',
        'error': {
            'reason': "Skill 'missing' does not exist in category 'writing'; use action='create' to add a new skill.",
        },
    }
    assert calls == []


def test_skill_editor_blocks_modify_and_remove_when_pending_review_exists(monkeypatch):
    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(
        skill_editor_mod,
        'list_all_skill_entries',
        lambda _base_dir: {
            'writing/existing': {
                'name': 'existing',
                'category': 'writing',
                'path': '/tmp/skills/writing/existing',
                'source': 'remote',
                'content': (
                    '---\n'
                    'name: existing\n'
                    'category: writing\n'
                    'description: Existing skill.\n'
                    '---\n'
                    'Use this skill for tests.\n'
                ),
            }
        },
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'find_pending_skill_review',
        lambda category, name: {'id': 'pending-1', 'category': category, 'skill_name': name},
    )

    modify_result = skill_editor_mod.skill_editor(
        'existing',
        'modify',
        category='writing',
        operations=[{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
    )
    remove_result = skill_editor_mod.skill_editor('existing', 'remove', category='writing')

    assert modify_result['success'] is False
    assert modify_result['tool'] == 'skill_editor'
    assert 'pending in skill_review_results' in modify_result['error']['reason']
    assert modify_result['meta'] == {'pending_record_id': 'pending-1'}
    assert remove_result['success'] is False
    assert remove_result['tool'] == 'skill_editor'
    assert 'pending in skill_review_results' in remove_result['error']['reason']
    assert remove_result['meta'] == {'pending_record_id': 'pending-1'}
