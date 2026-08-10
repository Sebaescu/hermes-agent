"""Wiring tests for the desktop API key on the dashboard HTTP + WS gates.

Covers what the provider test cannot:
  * ``_ws_auth_reason``/``_ws_auth_ok`` accepts a valid API key from the
    Authorization header (and ``?api_key=``) in BOTH loopback and gated modes,
    rejects an invalid key, and still honours the legacy token path.
  * ``auth_middleware`` accepts ``Authorization: Bearer <key>`` on a dynamic
    ``/api/*`` path (``/api/sessions/{id}``) — the reason the key is honored
    universally rather than via exact-match token-route registration.
  * ``_write_desktop_backend_port_file`` writes the stable discovery file only
    when ``HERMES_DESKTOP=1``.
"""
from __future__ import annotations

import json
import secrets
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from plugins.dashboard_auth.desktop_api_key import DesktopApiKeyProvider


def _strong_key() -> str:
    return secrets.token_urlsafe(32)


def _fake_ws(*, query=None, headers=None, client_host="127.0.0.1", path="/api/ws"):
    """Stand-in for starlette.WebSocket good enough for _ws_auth_ok.

    Supports both query params and headers (the api-key branch reads the
    Authorization header on the WS upgrade).
    """

    class _QP:
        def __init__(self, q):
            self._q = q or {}

        def get(self, k, default=""):
            return self._q.get(k, default)

    class _Headers:
        def __init__(self, h):
            # Starlette headers are case-insensitive — normalise on store.
            self._h = {k.lower(): v for k, v in (h or {}).items()}

        def get(self, k, default=""):
            return self._h.get(k.lower(), default)

    return SimpleNamespace(
        query_params=_QP(query),
        headers=_Headers(headers),
        client=SimpleNamespace(host=client_host),
        url=SimpleNamespace(path=path),
    )


@pytest.fixture
def api_key_provider():
    """Register a DesktopApiKeyProvider against a fresh key for the duration."""
    key = _strong_key()
    provider = DesktopApiKeyProvider(secret=key)
    from hermes_cli.dashboard_auth import register_provider

    register_provider(provider)
    yield key
    clear_providers()


@pytest.fixture
def loopback_state():
    """Flip web_server.app into loopback mode; restore on teardown."""
    prev = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    yield
    web_server.app.state.auth_required = prev


@pytest.fixture
def gated_state():
    prev = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = True
    yield
    web_server.app.state.auth_required = prev


# ---------------------------------------------------------------------------
# _ws_auth_ok — API key branch
# ---------------------------------------------------------------------------


class TestWsApiKey:
    def test_valid_key_in_header_accepted_loopback(self, api_key_provider, loopback_state):
        ws = _fake_ws(headers={"Authorization": f"Bearer {api_key_provider}"})
        assert web_server._ws_auth_ok(ws) is True

    def test_valid_key_in_query_accepted_loopback(self, api_key_provider, loopback_state):
        ws = _fake_ws(query={"api_key": api_key_provider})
        assert web_server._ws_auth_ok(ws) is True

    def test_valid_key_accepted_gated(self, api_key_provider, gated_state):
        # The key is universal — works even when the OAuth gate is engaged.
        ws = _fake_ws(headers={"Authorization": f"Bearer {api_key_provider}"})
        assert web_server._ws_auth_ok(ws) is True

    def test_invalid_key_rejected_loopback(self, api_key_provider, loopback_state):
        ws = _fake_ws(headers={"Authorization": "Bearer not-the-key"})
        assert web_server._ws_auth_ok(ws) is False

    def test_invalid_key_does_not_shadow_legacy_token(
        self, api_key_provider, loopback_state
    ):
        # The renderer's ?token=<session> path still works when no api key is
        # presented — the two credentials are independent.
        ws = _fake_ws(query={"token": web_server._SESSION_TOKEN})
        assert web_server._ws_auth_ok(ws) is True

    def test_invalid_key_with_no_other_credential_rejected(
        self, api_key_provider, loopback_state
    ):
        ws = _fake_ws(headers={"Authorization": "Bearer wrong"})
        assert web_server._ws_auth_ok(ws) is False


# ---------------------------------------------------------------------------
# auth_middleware — API key on a dynamic /api/* path (HTTP)
# ---------------------------------------------------------------------------


@pytest.fixture
def loopback_client():
    prev = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:8080")
    yield client
    web_server.app.state.auth_required = prev


class TestHttpApiKey:
    def test_bearer_key_passes_gate(self, api_key_provider, loopback_client):
        # Dynamic path (/api/sessions/{id}) — covered by universal honoring,
        # not exact-match token-route registration.
        r = loopback_client.get(
            "/api/sessions/abc",
            headers={"Authorization": f"Bearer {api_key_provider}"},
        )
        # Auth gate passed (not 401). The handler may 404 the fake id; that's
        # fine — we only assert the gate did not reject the credential.
        assert r.status_code != 401

    def test_no_credential_rejected(self, loopback_client):
        r = loopback_client.get("/api/sessions/abc")
        assert r.status_code == 401

    def test_wrong_key_rejected(self, api_key_provider, loopback_client):
        r = loopback_client.get(
            "/api/sessions/abc",
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# _write_desktop_backend_port_file
# ---------------------------------------------------------------------------


class TestPortFile:
    def test_written_when_desktop_spawned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        web_server._write_desktop_backend_port_file(12345)
        target = tmp_path / "runtime" / "desktop-backend.json"
        assert target.exists()
        payload = json.loads(target.read_text())
        assert payload["port"] == 12345
        assert isinstance(payload["pid"], int)

    def test_not_written_when_not_desktop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_DESKTOP", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        web_server._write_desktop_backend_port_file(12345)
        assert not (tmp_path / "runtime" / "desktop-backend.json").exists()

    def test_overwrites_on_each_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        web_server._write_desktop_backend_port_file(11111)
        web_server._write_desktop_backend_port_file(22222)
        payload = json.loads((tmp_path / "runtime" / "desktop-backend.json").read_text())
        assert payload["port"] == 22222
