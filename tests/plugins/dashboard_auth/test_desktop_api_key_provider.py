"""Tests for the DesktopApiKeyProvider plugin (non-interactive bearer secret).

Mirrors the drain provider tests. Exercises:
  * the shared entropy gate (assess_secret_strength) — fail-closed on weak keys,
  * constant-time verify_token returning a scoped TokenPrincipal,
  * the register(ctx) entry point's env/config resolution + skip reasons.

Unlike drain, this provider does NOT register a single token route — the key
is honoured universally on /api/* and /api/ws by web_server.py, so the route
assertion from the drain tests is replaced with "no route registered here".
"""
from __future__ import annotations

import secrets
from unittest.mock import MagicMock

import pytest

import plugins.dashboard_auth.desktop_api_key as api_key_plugin
from hermes_cli.dashboard_auth import TokenPrincipal, assert_protocol_compliance


@pytest.fixture(scope="module")
def plugin():
    return api_key_plugin


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_DESKTOP_API_KEY", raising=False)
    api_key_plugin.LAST_SKIP_REASON = ""
    yield


def _strong_key() -> str:
    # token_urlsafe(32) → 43 url-safe-b64 chars ≈ 256 bits.
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Entropy gate (shared module)
# ---------------------------------------------------------------------------


class TestEntropyGate:
    def test_strong_key_passes(self, plugin):
        from hermes_cli.dashboard_auth.secret_strength import assess_secret_strength

        assert assess_secret_strength(_strong_key()) is None

    def test_empty_rejected(self, plugin):
        from hermes_cli.dashboard_auth.secret_strength import assess_secret_strength

        assert assess_secret_strength("") is not None

    def test_too_short_rejected(self, plugin):
        from hermes_cli.dashboard_auth.secret_strength import assess_secret_strength

        assert assess_secret_strength("a1B2c3" * 7) is not None

    def test_long_but_repeated_rejected(self, plugin):
        from hermes_cli.dashboard_auth.secret_strength import assess_secret_strength

        assert assess_secret_strength("a" * 60) is not None


# ---------------------------------------------------------------------------
# Provider behaviour
# ---------------------------------------------------------------------------


class TestProvider:
    def test_protocol_compliance(self, plugin):
        assert_protocol_compliance(plugin.DesktopApiKeyProvider)

    def test_supports_token_flag(self, plugin):
        p = plugin.DesktopApiKeyProvider(secret=_strong_key())
        assert p.supports_token is True

    def test_is_non_interactive(self, plugin):
        p = plugin.DesktopApiKeyProvider(secret=_strong_key())
        assert p.supports_session is False

    def test_verify_token_accepts_matching_key(self, plugin):
        k = _strong_key()
        p = plugin.DesktopApiKeyProvider(secret=k, scope="desktop-api")
        principal = p.verify_token(token=k)
        assert isinstance(principal, TokenPrincipal)
        assert principal.principal == "desktop-api-key"
        assert principal.provider == "desktop-api-key"
        assert principal.scopes == ("desktop-api",)

    def test_verify_token_rejects_mismatch(self, plugin):
        p = plugin.DesktopApiKeyProvider(secret=_strong_key())
        assert p.verify_token(token="not-the-key") is None

    def test_verify_token_rejects_empty(self, plugin):
        p = plugin.DesktopApiKeyProvider(secret=_strong_key())
        assert p.verify_token(token="") is None

    def test_construction_rejects_weak_key(self, plugin):
        with pytest.raises(ValueError):
            plugin.DesktopApiKeyProvider(secret="weak")

    def test_interactive_methods_raise(self, plugin):
        p = plugin.DesktopApiKeyProvider(secret=_strong_key())
        with pytest.raises(NotImplementedError):
            p.start_login(redirect_uri="r")
        with pytest.raises(NotImplementedError):
            p.complete_login(code="c", state="s", code_verifier="v", redirect_uri="r")
        with pytest.raises(NotImplementedError):
            p.refresh_session(refresh_token="r")


# ---------------------------------------------------------------------------
# register() entry point
# ---------------------------------------------------------------------------


class TestRegister:
    def test_skips_when_no_key(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin, "_load_config_desktop_api_key_section", lambda: {})
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_dashboard_auth_provider.assert_not_called()
        assert "HERMES_DESKTOP_API_KEY" in plugin.LAST_SKIP_REASON

    def test_skips_and_fails_closed_on_weak_key(self, plugin, monkeypatch):
        monkeypatch.setenv("HERMES_DESKTOP_API_KEY", "tooweak")
        monkeypatch.setattr(plugin, "_load_config_desktop_api_key_section", lambda: {})
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_dashboard_auth_provider.assert_not_called()
        assert "rejected" in plugin.LAST_SKIP_REASON

    def test_registers_with_strong_env_key(self, plugin, monkeypatch):
        k = _strong_key()
        monkeypatch.setenv("HERMES_DESKTOP_API_KEY", k)
        monkeypatch.setattr(plugin, "_load_config_desktop_api_key_section", lambda: {})
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_dashboard_auth_provider.assert_called_once()
        provider = ctx.register_dashboard_auth_provider.call_args.args[0]
        assert isinstance(provider, plugin.DesktopApiKeyProvider)
        assert provider.verify_token(token=k) is not None
        assert plugin.LAST_SKIP_REASON == ""

    def test_config_scope_applied(self, plugin, monkeypatch):
        k = _strong_key()
        monkeypatch.setenv("HERMES_DESKTOP_API_KEY", k)
        monkeypatch.setattr(
            plugin,
            "_load_config_desktop_api_key_section",
            lambda: {"scope": "custom-scope"},
        )
        ctx = MagicMock()
        plugin.register(ctx)
        provider = ctx.register_dashboard_auth_provider.call_args.args[0]
        assert provider.verify_token(token=k).scopes == ("custom-scope",)

    def test_config_min_secret_chars_can_reject_otherwise_ok_key(
        self, plugin, monkeypatch
    ):
        k = _strong_key()  # 43 chars — fine by default, too short at 999
        monkeypatch.setenv("HERMES_DESKTOP_API_KEY", k)
        monkeypatch.setattr(
            plugin,
            "_load_config_desktop_api_key_section",
            lambda: {"min_secret_chars": 999},
        )
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_dashboard_auth_provider.assert_not_called()
        assert "rejected" in plugin.LAST_SKIP_REASON
