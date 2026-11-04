"""The dev callback gateway must forward exactly the agent's two callbacks - for real (UUID) session ids."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("callback_gateway", Path(__file__).resolve().parents[1] / "scripts" / "local" / "callback_gateway.py")
gateway = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gateway)

UUID = "68e78a32-0913-4ecf-aa58-61358d92d623"


@pytest.mark.parametrize("path", [f"/api/v1/sessions/{UUID}/mark-connected", f"/api/v1/sessions/{UUID}/mark-ended",
                                  "/api/v1/sessions/000000000000000000000000/mark-ended"])
def test_allowed_callbacks(path: str) -> None:
    assert gateway.ALLOWED.match(path)


@pytest.mark.parametrize("path", ["/api/v1/users", "/api/v1/health", "/docs", f"/api/v1/sessions/{UUID}", f"/api/v1/sessions/{UUID}/force-end",
                                  f"/api/v1/sessions/{UUID}/mark-ended/extra", "/api/v1/sessions/../users/mark-ended",
                                  "/api/v1/sessions/abc/mark-ended", f"/api/v1/sessions/{UUID};x/mark-ended"])
def test_everything_else_is_blocked(path: str) -> None:
    assert not gateway.ALLOWED.match(path)
