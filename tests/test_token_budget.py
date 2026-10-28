from core.clients.client import token_limit_kwarg


def test_small_demo_budget_preserves_room_for_reasoning():
    assert token_limit_kwarg("gpt-5.4-mini", 4096) == {"max_completion_tokens": 40000}
    assert token_limit_kwarg("o3", 800) == {"max_completion_tokens": 40000}


def test_larger_budget_is_preserved():
    assert token_limit_kwarg("gpt-5.4-mini", 60000) == {"max_completion_tokens": 60000}


def test_non_reasoning_budget_is_unchanged():
    assert token_limit_kwarg("gpt-4o", 4096) == {"max_tokens": 4096}
