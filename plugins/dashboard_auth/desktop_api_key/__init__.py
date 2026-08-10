"""DesktopApiKeyProvider — stable shared-bearer-secret for local clients.

A service-to-service auth provider for trusted local clients (e.g. Iris) that
need to drive the **desktop backend's** agent surface over loopback. Mirrors
the drain provider's shape (``supports_token`` / ``verify_token`` + the shared
fail-closed entropy gate) but, unlike drain, the key is honoured **universally**
on every ``/api/*`` HTTP route and the ``/api/ws`` WebSocket — not on a single
registered token route — because local clients call dynamic paths such as
``/api/sessions/{id}`` that the exact-match token-route registry can't cover.
That universal honoring is wired in ``hermes_cli/web_server.py``
(``_has_valid_api_key`` on the HTTP gate; an API-key branch in
``_ws_auth_reason`` on the WS gate), reusing this provider via the
``list_token_providers`` stack.

What it is
----------
A non-interactive shared bearer secret. The operator provisions one strong
(>=256-bit) secret into ``~/.hermes/.env`` as ``HERMES_DESKTOP_API_KEY``; this
provider verifies an inbound ``Authorization: bearer <key>`` (or ``?api_key=``)
against it with a constant-time compare and vouches for the caller as the
``desktop-api-key`` principal. It is NOT an interactive identity provider — no
login, cookie, session, or refresh.

Security properties
-------------------
* **Operator-chosen strong secret** — entropy-gated at registration; a
  weak/short/low-entropy value is rejected CLOSED (the plugin declines to
  register and records a skip reason). Bar: >= 256 bits / >= 43 url-safe-base64
  chars (``secrets.token_urlsafe(32)`` clears it).
* **Loopback trust model** — the desktop backend binds ``127.0.0.1``, so this
  key is a second factor for a local trusted client, not an internet-facing
  credential. Do NOT bind the dashboard to a public interface and rely on this
  key alone without the OAuth gate.
* **Constant-time compare** — ``hmac.compare_digest`` so the endpoint is not a
  timing oracle.

Configuration
-------------
The key is a CREDENTIAL, carried via env (the ``.env``-is-for-secrets-only rule):

    HERMES_DESKTOP_API_KEY   # >=43 url-safe-b64 chars

Behavioural knobs live in config.yaml:

    dashboard:
      desktop_api_key:
        scope: desktop-api        # capability label on the verified principal
        min_secret_chars: 43      # entropy bar (optional; default 43 ~= 256 bits)

When ``HERMES_DESKTOP_API_KEY`` is unset, the plugin is a no-op (records a
skip reason) — operators that don't run a local client just don't set it.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)
from hermes_cli.dashboard_auth.secret_strength import (
    DEFAULT_MIN_SECRET_CHARS,
    assess_secret_strength,
)

logger = logging.getLogger(__name__)

LAST_SKIP_REASON: str = ""


class DesktopApiKeyProvider(DashboardAuthProvider):
    """Non-interactive shared-bearer-secret provider for trusted local clients."""

    name = "desktop-api-key"
    display_name = "Desktop API key (local service credential)"
    supports_token = True
    supports_session = False

    def __init__(self, *, secret: str, scope: str = "desktop-api") -> None:
        # Defence in depth: construction also enforces the entropy bar, so a
        # caller that bypasses register()'s check still can't build a weak
        # provider. register() does the friendly skip-reason path; this raises.
        reason = assess_secret_strength(secret)
        if reason is not None:
            raise ValueError(f"desktop api key rejected: {reason}")
        self._secret = secret
        self._scope = scope or "desktop-api"

    # ---- token capability (the only thing this provider implements) --------

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        """Constant-time compare against the operator-provisioned secret.

        Returns a ``desktop-api-key`` principal on an exact match, else ``None``
        (the caller falls through / fails closed). Uses ``hmac.compare_digest``
        so a wrong token can't be recovered by timing.
        """
        if not token:
            return None
        if hmac.compare_digest(token.encode("utf-8"), self._secret.encode("utf-8")):
            return TokenPrincipal(
                principal="desktop-api-key",
                provider=self.name,
                scopes=(self._scope,),
            )
        return None

    # ---- interactive methods: unsupported (service credential only) --------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError(
            "DesktopApiKeyProvider is a non-interactive service credential; "
            "there is no login flow."
        )

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError(
            "DesktopApiKeyProvider is a non-interactive service credential."
        )

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        # Not a cookie-session provider — never mints a Session. Return None so
        # it stacks harmlessly in the cookie-verify loop.
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError(
            "DesktopApiKeyProvider is a non-interactive service credential."
        )

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def _load_config_desktop_api_key_section() -> dict:
    """Return ``dashboard.desktop_api_key`` from config.yaml, or ``{}``."""
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional
        logger.debug(
            "dashboard-auth-desktop-api-key: load_config() raised %s; "
            "falling back to env-only configuration",
            exc,
        )
        return {}
    section = cfg_get(cfg, "dashboard", "desktop_api_key", default=None)
    return section if isinstance(section, dict) else {}


def register(ctx) -> None:
    """Plugin entry — registers DesktopApiKeyProvider when a strong key is set.

    No-op (records a skip reason) when ``HERMES_DESKTOP_API_KEY`` is unset or
    fails the entropy gate. Does NOT register any single token route: the key is
    honored universally on /api/* and /api/ws by web_server.py via the
    list_token_providers stack, so it covers dynamic client paths.
    """
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""

    secret = os.environ.get("HERMES_DESKTOP_API_KEY", "").strip()
    if not secret:
        LAST_SKIP_REASON = (
            "HERMES_DESKTOP_API_KEY is not set. Set a >=256-bit key "
            "(e.g. `python -c \"import secrets; "
            "print(secrets.token_urlsafe(32))\"`) to let a trusted local client "
            "(e.g. Iris) drive the desktop backend; leave it unset to disable."
        )
        logger.debug("dashboard-auth-desktop-api-key: %s", LAST_SKIP_REASON)
        return

    section = _load_config_desktop_api_key_section()
    scope = (
        str(section.get("scope", "desktop-api") or "desktop-api").strip()
        or "desktop-api"
    )
    try:
        min_chars = int(section.get("min_secret_chars", DEFAULT_MIN_SECRET_CHARS))
    except (TypeError, ValueError):
        min_chars = DEFAULT_MIN_SECRET_CHARS

    reason = assess_secret_strength(secret, min_chars=min_chars)
    if reason is not None:
        LAST_SKIP_REASON = (
            f"HERMES_DESKTOP_API_KEY rejected — {reason}. "
            "The desktop API-key credential stays disabled (fail-closed)."
        )
        logger.warning("dashboard-auth-desktop-api-key: %s", LAST_SKIP_REASON)
        return

    try:
        provider = DesktopApiKeyProvider(secret=secret, scope=scope)
    except ValueError as exc:
        LAST_SKIP_REASON = f"DesktopApiKeyProvider construction failed: {exc}"
        logger.warning("dashboard-auth-desktop-api-key: %s", LAST_SKIP_REASON)
        return

    ctx.register_dashboard_auth_provider(provider)

    logger.info(
        "dashboard-auth-desktop-api-key: registered desktop API-key provider "
        "(scope=%s); honored on /api/* and /api/ws",
        scope,
    )
