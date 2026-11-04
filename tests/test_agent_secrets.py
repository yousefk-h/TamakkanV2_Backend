"""Regression tests: `agent-sync` must never send an incomplete secret set to the hosted agent.

Failure being guarded: `lk agent update-secrets --overwrite` replaces the agent's WHOLE secret set. A partial file
(only the 3 callback secrets) deleted OPENAI_API_KEY & co. from the dev agent, and Mariam crashed at startup.
The agent's own env file is the only source; the backend's env file is read only to prove both agree.
"""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("agent_secrets", ROOT / "scripts" / "local" / "agent_secrets.py")
agent_secrets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_secrets)

TUNNEL = "https://example-random-words.trycloudflare.com"
AGENT = {
    "OPENAI_API_KEY": "sk-test-openai", "ULTRAVOX_API_KEY": "test-ultravox", "SPATIALREAL_API_KEY": "sk-test-spatial",
    "SPATIALREAL_APP_ID": "app_test", "SPATIALREAL_AVATAR_ID": "avatar-test", "MARIAM_IDLE_TIMEOUT_SECONDS": "120",
    "AGENT_INTERNAL_API_KEY": "dev-callback-key", "LIVEKIT_AGENT_NAME": "tamakkan-mariam-sandbox",
    "LIVEKIT_URL": "wss://dev.example", "LIVEKIT_API_KEY": "lk-key", "LIVEKIT_API_SECRET": "lk-secret",
}
BACKEND = {k: AGENT[k] for k in agent_secrets.SHARED_WITH_BACKEND}


def _write(path: Path, values: dict) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def test_complete_input_yields_all_nine_secrets() -> None:
    result = agent_secrets.build_secret_set(AGENT, BACKEND, TUNNEL)
    assert set(result) == set(agent_secrets.REQUIRED_ALL) and len(result) == 9
    assert result["BACKEND_API_URL"] == TUNNEL + "/api/v1"
    assert "LIVEKIT_URL" not in result, "LiveKit Cloud injects the project itself; it is never a secret of the hosted agent"


@pytest.mark.parametrize("missing", sorted(agent_secrets.REQUIRED_FROM_AGENT))
def test_any_missing_or_empty_agent_secret_aborts(missing: str) -> None:
    for broken in ({k: v for k, v in AGENT.items() if k != missing}, {**AGENT, missing: ""}):
        with pytest.raises(agent_secrets.SecretSetError) as e:
            agent_secrets.build_secret_set(broken, BACKEND, TUNNEL)
        assert missing in str(e.value)


def test_the_original_incident_callback_only_file_is_refused() -> None:
    """The exact failure: only callback secrets available -> must abort, not send 3 secrets with --overwrite."""
    only_callback = {"AGENT_INTERNAL_API_KEY": "dev-callback-key", "LIVEKIT_AGENT_NAME": "tamakkan-mariam-sandbox"}
    with pytest.raises(agent_secrets.SecretSetError) as e:
        agent_secrets.build_secret_set(only_callback, BACKEND, TUNNEL)
    assert "OPENAI_API_KEY" in str(e.value)


@pytest.mark.parametrize("bad", ["", "https://tamakkanbackend.tamakkan.online", "http://x.trycloudflare.com", "https://evil.example.com"])
def test_callback_url_must_be_the_temporary_tunnel(bad: str) -> None:
    with pytest.raises(agent_secrets.SecretSetError):
        agent_secrets.build_secret_set(AGENT, BACKEND, bad)


@pytest.mark.parametrize("name", ["mariam-agent", "tamakkan-mariam-v2", "englishteacher", "tamakkan-mariam-dev"])
def test_existing_agent_names_are_refused(name: str) -> None:
    with pytest.raises(agent_secrets.SecretSetError):
        agent_secrets.build_secret_set({**AGENT, "LIVEKIT_AGENT_NAME": name}, {**BACKEND, "LIVEKIT_AGENT_NAME": name}, TUNNEL)


@pytest.mark.parametrize("name", sorted(agent_secrets.SHARED_WITH_BACKEND))
def test_backend_and_agent_must_agree_on_every_shared_value(name: str) -> None:
    """A backend minting tokens for one SpatialReal app (or callback key, or LiveKit project) while the agent holds
    another would fail only at session time. Refuse up front, naming the variable."""
    with pytest.raises(agent_secrets.SecretSetError) as e:
        agent_secrets.build_secret_set(AGENT, {**BACKEND, name: "something-else"}, TUNNEL)
    assert name in str(e.value)


def test_hosted_agents_do_not_need_the_livekit_project_in_their_file() -> None:
    without_livekit = {k: v for k, v in AGENT.items() if not k.startswith(("LIVEKIT_URL", "LIVEKIT_API"))}
    assert len(agent_secrets.build_secret_set(without_livekit, BACKEND, TUNNEL)) == 9


def test_errors_never_contain_secret_values() -> None:
    with pytest.raises(agent_secrets.SecretSetError) as e:
        agent_secrets.build_secret_set({**AGENT, "OPENAI_API_KEY": ""}, {**BACKEND, "SPATIALREAL_API_KEY": "other-secret"}, TUNNEL)
    assert not any(v in str(e.value) for v in list(AGENT.values()) + ["other-secret"] if v)


def test_cli_writes_nothing_when_incomplete(tmp_path: Path) -> None:
    agent, backend, out = _write(tmp_path / "a.env", {"SPATIALREAL_APP_ID": "app_test"}), _write(tmp_path / "b.env", BACKEND), tmp_path / "out.env"
    rc = agent_secrets.main(["--agent", str(agent), "--backend", str(backend), "--tunnel-url", TUNNEL, "--out", str(out)])
    assert rc == 2 and not out.exists()


def test_cli_writes_the_full_set_when_complete(tmp_path: Path) -> None:
    agent, backend, out = _write(tmp_path / "a.env", AGENT), _write(tmp_path / "b.env", BACKEND), tmp_path / "out.env"
    assert agent_secrets.main(["--agent", str(agent), "--backend", str(backend), "--tunnel-url", TUNNEL, "--out", str(out)]) == 0
    assert {ln.split("=", 1)[0] for ln in out.read_text(encoding="utf-8").splitlines()} == set(agent_secrets.REQUIRED_ALL)


def test_cli_check_mode_reports_agreement_and_disagreement_without_writing(tmp_path: Path) -> None:
    agent = _write(tmp_path / "a.env", AGENT)
    assert agent_secrets.main(["--agent", str(agent), "--backend", str(_write(tmp_path / "b.env", BACKEND)), "--check"]) == 0
    assert agent_secrets.main(["--agent", str(agent), "--backend", str(_write(tmp_path / "c.env", {**BACKEND, "SPATIALREAL_APP_ID": "x"})), "--check"]) == 2


def test_start_script_only_overwrites_through_the_validated_builder() -> None:
    """tamakkan-local.ps1 must build the file with agent_secrets.py and check the result; no hand-built partial pairs."""
    ps1 = (ROOT / "scripts" / "local" / "tamakkan-local.ps1").read_text(encoding="utf-8")
    sync = ps1[ps1.index("function Sync-Agent"): ps1.index("function Show-Status")]
    assert sync.count("update-secrets") == 1
    assert sync.index("agent_secrets.py") < sync.index("update-secrets"), "builder must run before any lk call"
    assert "$pairs" not in sync and "[ordered]@{" not in sync, "no partial hand-built secret sets"
    after = sync[sync.index("update-secrets"):]
    assert "lk agent secrets" in after and re.search(r"REQUIRED|required", after), "must verify all secrets exist afterwards"


def test_the_local_tooling_reads_only_the_component_env_files() -> None:
    """One env source per component: backend/.env, livekit_agent/.env, frontend/.env. Nothing under config/ is read."""
    for name in ("tamakkan-local.ps1", "agent_secrets.py", "callback_gateway.py"):
        text = (ROOT / "scripts" / "local" / name).read_text(encoding="utf-8")
        assert not re.search(r"local-platform|hosted-agent|\\config\\|'config'|\.tamakkan[/\\]+local[/\\]+backend\.env", text), name


def test_the_runner_warns_when_gemini_is_not_configured() -> None:
    """The local API used to run with an empty GEMINI_API_KEY and silently produce no AI reports."""
    ps1 = (ROOT / "scripts" / "local" / "tamakkan-local.ps1").read_text(encoding="utf-8")
    ensure = ps1[ps1.index("function Ensure-EnvFiles"): ps1.index("# ---------- app processes")]
    assert "GEMINI_API_KEY" in ensure and "WARNING" in ensure


def test_the_runner_can_never_write_the_production_agents_secrets() -> None:
    """tamakkan-mariam-dev IS the production Avatar agent. The runner once pushed a laptop tunnel URL and a local key into
    it on every start, so production callbacks never reached the production backend."""
    ps1 = (ROOT / "scripts" / "local" / "tamakkan-local.ps1").read_text(encoding="utf-8")
    assert "CA_8Em9pPqozc5b" in ps1 and "function Test-AgentIsProduction" in ps1
    sync = ps1[ps1.index("function Sync-Agent"): ps1.index("function Show-Status")]
    assert sync.index("Test-AgentIsProduction") < sync.index("update-secrets"), "the production check comes before any lk call"
    for caller in ("if ($Mariam) { Start-Gateway; Start-Tunnel;", "Start-Gateway; Start-Tunnel; if (Test-AgentIsProduction)"):
        assert "Test-AgentIsProduction" in ps1[ps1.index(caller): ps1.index(caller) + 260], "start/tunnel skip the sync for the production agent"
