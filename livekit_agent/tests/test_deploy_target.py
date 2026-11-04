"""Guards the LiveKit Cloud deploy target: Tamakkan's own project and agent,
never the historical agent shared with another product. No network, no keys."""

import re
from pathlib import Path

TOML = Path(__file__).resolve().parents[1] / "livekit.toml"
LEGACY_IDS = ("CA_L7qaqyCwtoMe", "ielets-dajbgqlm")


def test_livekit_toml_targets_dedicated_tamakkan_agent() -> None:
    text = TOML.read_text(encoding="utf-8")
    assert re.search(r'subdomain\s*=\s*"tamakkan-agent-m4zc52ik"', text)
    assert re.search(r'id\s*=\s*"CA_LBTsvNPdKaHR"', text)
    for legacy in LEGACY_IDS:
        assert legacy not in text, f"livekit.toml must not reference the shared legacy agent ({legacy})"
