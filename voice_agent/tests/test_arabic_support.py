"""Optional, teacher-controlled Jordanian-Arabic clarification support
(cartoon Jordanian Arabic card).

`src/agent.py` imports `handlebars`/`livekit.agents` at module level (same as
every other test in this directory - see `test_reasoning.py`'s own
`_livekit_agent_module` helper), so even the pure-logic tests below go
through the same `pytest.importorskip` gate rather than a bare top-level
`import agent`: this module is skipped as a whole wherever those aren't
installed, instead of failing collection.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _livekit_agent_module():
    pytest.importorskip("livekit.agents")
    pytest.importorskip("livekit.plugins.ai_coustics")
    pytest.importorskip("handlebars")
    import importlib

    return importlib.import_module("agent")


def test_unset_field_preserves_the_exact_legacy_line():
    """A topic dispatched before this feature existed never has the key at
    all - `metadata.get` returns `None`, same as a topic whose own
    `arabic_support_enabled` is still `None`. Both must behave exactly as
    Voice Practice always did: the old line, unconditionally."""
    m = _livekit_agent_module()
    assert m._build_arabic_support_line({}) == m._LEGACY_ARABIC_LINE
    assert m._build_arabic_support_line({"arabic_support_enabled": None}) == m._LEGACY_ARABIC_LINE


def test_explicit_false_means_no_arabic_at_all():
    m = _livekit_agent_module()
    assert m._build_arabic_support_line({"arabic_support_enabled": False}) == ""
    # A stray intervention level must not resurrect the old/new line when disabled.
    assert m._build_arabic_support_line({"arabic_support_enabled": False, "arabic_intervention_level": "high"}) == ""


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_explicit_true_uses_the_matching_leveled_line(level):
    m = _livekit_agent_module()
    line = m._build_arabic_support_line({"arabic_support_enabled": True, "arabic_intervention_level": level})
    assert line == m._ARABIC_SUPPORT_LINES[level]
    assert line != m._LEGACY_ARABIC_LINE


def test_explicit_true_falls_back_to_low_when_level_missing_or_invalid():
    m = _livekit_agent_module()
    assert m._build_arabic_support_line({"arabic_support_enabled": True}) == m._ARABIC_SUPPORT_LINES["low"]
    assert (
        m._build_arabic_support_line({"arabic_support_enabled": True, "arabic_intervention_level": "nonsense"})
        == m._ARABIC_SUPPORT_LINES["low"]
    )


def test_each_level_mentions_jordanian_not_egyptian_or_msa():
    m = _livekit_agent_module()
    for text in m._ARABIC_SUPPORT_LINES.values():
        lowered = text.lower()
        assert "jordanian" in lowered or "ammani" in lowered
        assert "egyptian dialect" in lowered  # i.e. explicitly rules it out
        assert "modern standard arabic" in lowered  # i.e. explicitly rules it out


def test_levels_are_textually_distinct():
    m = _livekit_agent_module()
    assert len(set(m._ARABIC_SUPPORT_LINES.values())) == len(m._ARABIC_SUPPORT_LINES)


def _instructions(agent_module, metadata: dict) -> str:
    return agent_module.DefaultAgent(metadata=json.dumps(metadata)).instructions


def test_default_agent_uses_legacy_line_when_field_absent():
    agent_module = _livekit_agent_module()
    text = _instructions(agent_module, {"lesson_title": "Music", "level": "beginner", "lesson_prompt": "Discuss", "opening_line": "Hi!"})
    assert agent_module._LEGACY_ARABIC_LINE in text


def test_default_agent_omits_arabic_line_when_explicitly_disabled():
    agent_module = _livekit_agent_module()
    text = _instructions(
        agent_module,
        {"lesson_title": "Music", "level": "beginner", "lesson_prompt": "Discuss", "opening_line": "Hi!", "arabic_support_enabled": False},
    )
    assert agent_module._LEGACY_ARABIC_LINE not in text
    for line in agent_module._ARABIC_SUPPORT_LINES.values():
        assert line not in text


def test_default_agent_uses_leveled_line_when_explicitly_enabled():
    agent_module = _livekit_agent_module()
    text = _instructions(
        agent_module,
        {
            "lesson_title": "Music", "level": "beginner", "lesson_prompt": "Discuss", "opening_line": "Hi!",
            "arabic_support_enabled": True, "arabic_intervention_level": "high",
        },
    )
    assert agent_module._ARABIC_SUPPORT_LINES["high"] in text
    assert agent_module._LEGACY_ARABIC_LINE not in text
