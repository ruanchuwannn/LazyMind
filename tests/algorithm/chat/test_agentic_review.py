from __future__ import annotations

import os
import sys


_ALGO = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'algorithm')
_LAZYLLM_ROOT = os.path.join(_ALGO, 'lazyllm')
if _ALGO not in sys.path:
    sys.path.insert(0, _ALGO)
if _LAZYLLM_ROOT not in sys.path:
    sys.path.insert(0, _LAZYLLM_ROOT)

for _module_name in list(sys.modules):
    if _module_name == 'lazyllm' or _module_name.startswith('lazyllm.'):
        del sys.modules[_module_name]

from lazymind.review.config import REVIEW_TOOLS  # noqa: E402
from lazymind.review.prompts import COMBINED_REVIEW_PROMPT  # noqa: E402


def test_combined_review_uses_three_tools_and_single_choice_prompt():
    assert REVIEW_TOOLS['combined'] == ['memory_editor', 'skill_editor', 'vocab_learn']
    assert 'vocab_learn' in COMBINED_REVIEW_PROMPT
    assert 'exactly three tool choices' in COMBINED_REVIEW_PROMPT
    assert 'at most one' in COMBINED_REVIEW_PROMPT
