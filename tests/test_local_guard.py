"""LOCAL_DEV_MODE must never be able to point at production data or LiveKit."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.local_guard import frontend_api_url_violations, local_mode_violations

GOOD = dict(mongodb_uri="mongodb://127.0.0.1:27039", mongodb_db_name="tamakkan_integration", environment="development",
            livekit_agent_name="tamakkan-mariam-dev")


def test_good_local_config_has_no_violations() -> None:
    assert local_mode_violations(**GOOD) == []


@pytest.mark.parametrize("name", ["tamakkan", "Tamakkan", "TAMAKKAN"])
def test_production_database_names_are_rejected(name: str) -> None:
    assert any("database" in p.lower() for p in local_mode_violations(**{**GOOD, "mongodb_db_name": name}))


def test_database_must_have_local_prefix() -> None:
    assert local_mode_violations(**{**GOOD, "mongodb_db_name": "scratch"})


@pytest.mark.parametrize("uri", ["mongodb+srv://u:p@cluster0.example.mongodb.net/db", "mongodb://db.example.com:27017",
                                 "mongodb://user:pw@203.0.113.9:27017", "mongodb://127.0.0.1:27017,db.example.com:27017"])
def test_non_loopback_mongo_is_rejected(uri: str) -> None:
    assert local_mode_violations(**{**GOOD, "mongodb_uri": uri})


@pytest.mark.parametrize("uri", ["mongodb://localhost:27017", "mongodb://user:pw@127.0.0.1:27039/?authSource=admin"])
def test_loopback_mongo_is_accepted(uri: str) -> None:
    assert local_mode_violations(**{**GOOD, "mongodb_uri": uri}) == []


def test_v2_docker_mongo_requires_an_explicit_local_opt_in() -> None:
    uri = "mongodb://tamakkan-v2-mongo:27017"
    assert local_mode_violations(**{**GOOD, "mongodb_uri": uri})
    assert local_mode_violations(**{**GOOD, "mongodb_uri": uri, "allow_docker_local_mongo": True}) == []


@pytest.mark.parametrize("url", ["wss://tamakkan-agent-m4zc52ik.livekit.cloud", "wss://ielets-dajbgqlm.livekit.cloud"])
def test_production_livekit_projects_are_rejected(url: str) -> None:
    assert local_mode_violations(**{**GOOD, "livekit_url": url})


def test_other_livekit_project_is_allowed_for_opt_in_agent_tests() -> None:
    assert local_mode_violations(**{**GOOD, "livekit_url": "wss://some-dev-project.livekit.cloud"}) == []


@pytest.mark.parametrize("agent", ["mariam-agent", "tamakkan-mariam-v2"])
def test_production_agent_names_are_rejected(agent: str) -> None:
    assert local_mode_violations(**{**GOOD, "livekit_agent_name": agent})


def test_non_development_environment_and_smtp_are_rejected() -> None:
    assert local_mode_violations(**{**GOOD, "environment": "production"})
    assert local_mode_violations(**{**GOOD, "email_delivery_mode": "smtp"})


def test_frontend_may_only_call_a_loopback_api() -> None:
    assert frontend_api_url_violations("http://127.0.0.1:4673/api/v1") == []
    assert frontend_api_url_violations("https://tamakkanbackend.tamakkan.online/api/v1")


def test_settings_refuse_to_load_a_production_database_in_local_mode() -> None:
    with pytest.raises(ValidationError, match="LOCAL_DEV_MODE safeguards"):
        Settings(_env_file=None, LOCAL_DEV_MODE=True, MONGODB_URI="mongodb://127.0.0.1:27017", MONGODB_DB_NAME="tamakkan")


def test_settings_accept_a_local_config_in_local_mode() -> None:
    s = Settings(_env_file=None, LOCAL_DEV_MODE=True, MONGODB_URI="mongodb://127.0.0.1:27039",
                 MONGODB_DB_NAME="tamakkan_integration", LIVEKIT_AGENT_NAME="tamakkan-mariam-dev")
    assert s.LOCAL_DEV_MODE


def test_guard_is_inactive_outside_local_mode() -> None:
    Settings(_env_file=None, MONGODB_DB_NAME="tamakkan")  # production-style settings still load normally
