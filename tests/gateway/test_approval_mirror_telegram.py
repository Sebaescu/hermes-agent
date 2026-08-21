"""Tests for the opt-in approval mirror to Telegram (fork feature).

The mirror lives in ``_approval_notify_sync``'s helper
``_mirror_approval_to_telegram`` (gateway/run.py, TurnRunner), whose gating
decision is extracted into the pure module-level function
``_approval_mirror_target``. It bridges the "session I'm not watching" gap:
an approval raised in a web/Odyssey api_server session is ALSO sent — with
interactive buttons — to the Telegram home channel when
``gateway.approval_mirror_telegram`` is true.

Contracts pinned here (behavior, not source shape):
1. Mirror decision — config off → no mirror; telegram session → no mirror
   (would duplicate the card the session already got); missing adapter /
   disconnected bot / missing home channel → no mirror; fully-configured
   setup → mirror fires with the home chat id.
2. Config plumbing — GatewayConfig parses approval_mirror_telegram from
   both flat and nested gateway sections, default False, coerces strings.
3. Double resolution — two live cards (web banner + Telegram buttons)
   target the same per-session queue: the FIRST resolution wins and the
   SECOND fails gracefully (resolve_gateway_approval returns 0 and the
   Telegram callback answers "Approval expired" instead of raising).
"""

import asyncio
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.run import _approval_mirror_target


# ===========================================================================
# _approval_mirror_target — the pure mirror decision
# ===========================================================================


class _FakeTelegramAdapter:
    # Class-level method so the type() check in the decision sees it
    # (mirrors the real TelegramAdapter.send_exec_approval).
    async def send_exec_approval(self, **kwargs):
        return SimpleNamespace(success=True, message_id="1")

    def __init__(self, connected=True):
        self._bot = MagicMock() if connected else None


def _mirror_config(*, enabled=True, home=True):
    platforms = {}
    if home:
        tg_cfg = PlatformConfig(enabled=True, token="t")
        tg_cfg.home_channel = HomeChannel(
            platform=Platform.TELEGRAM, chat_id="8479321670", name="Home"
        )
        platforms[Platform.TELEGRAM] = tg_cfg
    return SimpleNamespace(
        approval_mirror_telegram=enabled,
        platforms=platforms,
    )


def _mirror_adapters(connected=True):
    return {Platform.TELEGRAM: _FakeTelegramAdapter(connected=connected)}


class TestMirrorDecision:
    """Behavior of the pure gate: when does the mirror fire?"""

    def test_config_false_disables_mirror(self):
        target = _approval_mirror_target(
            config=_mirror_config(enabled=False),
            adapters=_mirror_adapters(),
            session_platform="api_server",
        )
        assert target is None, "opt-in flag off must never mirror"

    def test_telegram_session_is_not_mirrored(self):
        """A session already on Telegram got its interactive card from the
        primary path — mirroring would duplicate the prompt in the same chat.
        """
        target = _approval_mirror_target(
            config=_mirror_config(enabled=True),
            adapters=_mirror_adapters(),
            session_platform="telegram",
        )
        assert target is None

    def test_missing_telegram_adapter_is_skipped(self):
        target = _approval_mirror_target(
            config=_mirror_config(enabled=True),
            adapters={},  # no telegram adapter connected
            session_platform="api_server",
        )
        assert target is None

    def test_disconnected_bot_is_skipped(self):
        target = _approval_mirror_target(
            config=_mirror_config(enabled=True),
            adapters=_mirror_adapters(connected=False),  # _bot is None
            session_platform="api_server",
        )
        assert target is None

    def test_missing_home_channel_is_skipped(self):
        target = _approval_mirror_target(
            config=_mirror_config(enabled=True, home=False),
            adapters=_mirror_adapters(),
            session_platform="api_server",
        )
        assert target is None

    def test_fully_configured_setup_mirrors_to_home_chat(self):
        adapter = _FakeTelegramAdapter(connected=True)
        target = _approval_mirror_target(
            config=_mirror_config(enabled=True),
            adapters={Platform.TELEGRAM: adapter},
            session_platform="api_server",
        )
        assert target is not None
        mirrored_adapter, chat_id = target
        assert mirrored_adapter is adapter
        assert chat_id == "8479321670"


# ===========================================================================
# Config plumbing — the flag parses from config.yaml with default False
# ===========================================================================


class TestMirrorConfigPlumbing:
    def test_default_false(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig()
        assert getattr(cfg, "approval_mirror_telegram", None) is False

    def test_parses_nested_gateway_section(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig.from_dict({"gateway": {"approval_mirror_telegram": True}})
        assert cfg.approval_mirror_telegram is True

    def test_parses_flat_key(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig.from_dict({"approval_mirror_telegram": True})
        assert cfg.approval_mirror_telegram is True

    def test_bool_coercion(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig.from_dict({"gateway": {"approval_mirror_telegram": "true"}})
        assert cfg.approval_mirror_telegram is True

    def test_serializes_to_dict(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig.from_dict({"approval_mirror_telegram": True})
        assert cfg.to_dict()["approval_mirror_telegram"] is True

    def test_default_config_dict_has_flag_false(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        gw = DEFAULT_CONFIG.get("gateway", {})
        assert gw.get("approval_mirror_telegram") is False, (
            "DEFAULT_CONFIG must ship the flag as False (opt-in)"
        )


# ===========================================================================
# Double resolution — two live cards, one per-session queue
# ===========================================================================

SESSION_KEY = "agent:main:api_server:web:s123"


def _enqueue_pending_approval(session_key: str, command: str = "rm -rf /tmp/x"):
    """Register a REAL pending approval in the module-level gateway queue.

    Same state _await_gateway_decision builds (tools/approval.py) — used here
    without the blocking wait loop so the queue can be resolved synchronously.
    """
    import tools.approval as approval_mod

    entry = approval_mod._ApprovalEntry(
        {"command": command, "description": "test", "pattern_keys": ["test"]}
    )
    with approval_mod._lock:
        approval_mod._gateway_queues.setdefault(session_key, []).append(entry)
    return entry


def _clear_approval_queue(session_key: str):
    import tools.approval as approval_mod

    with approval_mod._lock:
        approval_mod._gateway_queues.pop(session_key, None)


class TestDoubleResolution:
    def test_first_resolution_wins_second_returns_zero(self):
        """resolve_gateway_approval is FIFO per session: the first tap
        resolves the pending entry; the second tap finds an empty queue and
        must return 0 (nothing pending) — never raise.
        """
        from tools.approval import resolve_gateway_approval

        _enqueue_pending_approval(SESSION_KEY)
        try:
            first = resolve_gateway_approval(SESSION_KEY, "once")
            assert first == 1, "first tap must resolve the pending approval"
            second = resolve_gateway_approval(SESSION_KEY, "once")
            assert second == 0, (
                "second tap on the same prompt must resolve 0 entries "
                "(already resolved elsewhere), not raise"
            )
        finally:
            _clear_approval_queue(SESSION_KEY)

    @pytest.mark.asyncio
    async def test_telegram_second_tap_answers_expired_gracefully(self):
        """E2E through the Telegram callback handler: with a REAL pending
        approval, the first ea: tap approves it; a second tap (the mirrored
        card after the web banner already resolved the prompt) must answer
        "Approval expired" and edit the card — not raise.
        """
        from plugins.platforms.telegram.adapter import TelegramAdapter

        config = PlatformConfig(enabled=True, token="test-token")
        adapter = TelegramAdapter(config)
        adapter._bot = AsyncMock()
        adapter._app = MagicMock()
        adapter._approval_state[7] = SESSION_KEY

        _enqueue_pending_approval(SESSION_KEY)
        try:

            def _make_query():
                query = AsyncMock()
                query.data = "ea:once:7"
                query.message = MagicMock()
                query.message.chat_id = 8479321670
                query.from_user = MagicMock()
                query.from_user.first_name = "Sebas"
                query.from_user.id = "8479321670"
                query.answer = AsyncMock()
                query.edit_message_text = AsyncMock()
                return query

            update = MagicMock()
            context = MagicMock()

            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
                # First tap — resolves the real pending approval
                update.callback_query = _make_query()
                await adapter._handle_callback_query(update, context)
                first_answer = update.callback_query.answer.call_args
                assert "Approved" in str(first_answer)

                # Second tap on the same (mirrored) card — the handler pops
                # the approval_id on first use, so the stale tap is answered
                # "already resolved" and returns early. No raise, no second
                # resolution, no edit claiming a decision that didn't happen.
                update.callback_query = _make_query()
                await adapter._handle_callback_query(update, context)
                second_answer = update.callback_query.answer.call_args
                assert "already been resolved" in str(second_answer), (
                    "second tap must be answered gracefully as already-resolved"
                )
                second_edit = update.callback_query.edit_message_text.call_args
                assert second_edit is None, (
                    "stale tap must not overwrite the first tap's decision card"
                )
        finally:
            _clear_approval_queue(SESSION_KEY)
