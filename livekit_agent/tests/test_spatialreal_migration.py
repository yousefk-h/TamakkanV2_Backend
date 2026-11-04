"""Migration of the Avatar agent to the NEW SpatialReal platform (https://docs.spatialreal.ai/resources/migration-guide):
livekit-plugins-spatialreal >= 1.8.4 (runs on the `spatialreal` Python SDK / api.spatialreal.com), no legacy AvatarKit
package, no legacy `spatialwalk.cloud` endpoint, and the rest of the stack (LiveKit Agents, OpenAI Realtime, Ultravox, Krisp)
exactly where it was. No network, no keys, no paid session."""

import importlib.util
import os
import re
from importlib.metadata import version
from pathlib import Path

os.environ.setdefault("LIVEKIT_URL", "wss://example.livekit.cloud")
os.environ.setdefault("LIVEKIT_API_KEY", "test-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret")

from livekit.plugins import spatialreal  # noqa: E402

from livekit_agent import agent as mariam  # noqa: E402

AGENT_DIR = Path(__file__).resolve().parents[1]
REQUIREMENTS = (AGENT_DIR / "requirements.txt").read_text(encoding="utf-8")
SOURCE = (AGENT_DIR / "agent.py").read_text(encoding="utf-8")
CONFIG = (AGENT_DIR / "config.py").read_text(encoding="utf-8")


def _code(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    return re.sub(r"(?m)^\s*#.*$", "", text)


def test_the_new_platform_plugin_and_sdk_are_installed_and_the_legacy_package_is_not() -> None:
    parts = [int(x) for x in version("livekit-plugins-spatialreal").split(".")]
    assert parts >= [1, 8, 4], "the migration guide requires livekit-plugins-spatialreal >= 1.8.4"
    assert version("spatialreal"), "the new SpatialReal Python SDK is a dependency of the plugin"
    assert importlib.util.find_spec("avatarkit") is None, "the legacy AvatarKit Python package must not be installed"


def test_requirements_pin_the_new_plugin_and_leave_the_rest_of_the_stack_alone() -> None:
    assert re.search(r"(?m)^livekit-plugins-spatialreal==1\.8\.4$", REQUIREMENTS)
    assert not re.search(r"(?mi)^\s*avatarkit", REQUIREMENTS)
    for pinned in (r"livekit-agents\[openai\]==1\.7\.1", r"livekit-plugins-ultravox==1\.7\.1", r"livekit-plugins-krisp==0\.3\.1"):
        assert re.search(rf"(?m)^{pinned}$", REQUIREMENTS), f"{pinned} must not change in this migration"
    assert version("livekit-agents") == "1.7.1"
    assert version("livekit-plugins-openai") == "1.7.1" and version("livekit-plugins-ultravox") == "1.7.1"


def test_no_legacy_spatialwalk_endpoint_or_override_is_configured_anywhere_in_the_agent() -> None:
    for name, text in (("agent.py", SOURCE), ("config.py", CONFIG)):
        code = _code(text)
        assert not re.search(r"spatialwalk|console_endpoint_url|ingress_endpoint_url|SPATIALREAL_CONSOLE_ENDPOINT|SPATIALREAL_INGRESS_ENDPOINT", code, re.I), name
    # the plugin's OWN defaults are the new platform's hosts
    import livekit.plugins.spatialreal.avatar as avatar

    assert "spatialwalk" not in avatar.DEFAULT_CONSOLE_ENDPOINT and "spatialwalk" not in avatar.DEFAULT_INGRESS_ENDPOINT
    assert "spatialreal" in avatar.DEFAULT_CONSOLE_ENDPOINT and "spatialreal" in avatar.DEFAULT_INGRESS_ENDPOINT


def test_the_avatar_session_is_still_built_from_the_three_settings_only() -> None:
    assert "spatialreal.AvatarSession(" in SOURCE
    block = SOURCE[SOURCE.index("spatialreal.AvatarSession(") : SOURCE.index("await avatar.start(")]
    assert re.findall(r"(\w+)=settings\.(\w+)", block) == [
        ("api_key", "SPATIALREAL_API_KEY"),
        ("app_id", "SPATIALREAL_APP_ID"),
        ("avatar_id", "SPATIALREAL_AVATAR_ID"),
    ]
    assert "await avatar.start(agent_session, room=ctx.room)" in SOURCE, "started before the agent session, as the docs require"


def test_the_plugin_accepts_that_construction_and_keeps_the_participant_identity_the_web_client_expects() -> None:
    session = spatialreal.AvatarSession(api_key="sk-test", app_id="app_test", avatar_id="avatar-1")
    assert session.avatar_identity == "spatialreal-avatar", "the Web SDK's default avatarIdentity"


def test_the_avatar_participant_is_still_marked_publish_on_behalf_so_lk_agent_state_is_read_from_the_tutor() -> None:
    import livekit.plugins.spatialreal.avatar as avatar

    code = Path(avatar.__file__).read_text(encoding="utf-8")
    assert "ATTRIBUTE_PUBLISH_ON_BEHALF" in code and "with_attributes(egress_attributes)" in code
