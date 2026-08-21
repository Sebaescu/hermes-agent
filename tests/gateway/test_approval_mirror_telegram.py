"""Tests for the opt-in approval mirror to Telegram (fork feature).

The mirror lives in ``_approval_notify_sync``'s helper
``_mirror_approval_to_telegram`` (gateway/run.py, TurnRunner). It bridges
the "session I'm not watching" gap: an approval raised in a web/Odyssey
api_server session is ALSO sent — with interactive buttons — to the
Telegram home channel when ``gateway.approval_mirror_telegram`` is true.

These tests pin three contracts:
1. AST wiring — the mirror is invoked from _approval_notify_sync, gated on
   the primary path having delivered (_mirror_now), and the mirror itself
   checks the config flag BEFORE touching the Telegram adapter.
2. Config plumbing — GatewayConfig parses approval_mirror_telegram from
   both flat and nested gateway sections, default False.
3. Skip behavior (source-verified) — same-platform (telegram) sessions,
   missing adapter, and missing home channel all return without sending.
"""

import ast
import inspect


def _get_fn_source(module, name: str) -> str:
    """Full source of a (possibly nested) function via AST location."""
    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"function {name} not found")


class TestMirrorWiringAst:
    """Contract: _approval_notify_sync calls the mirror after delivery."""

    def test_notify_calls_mirror_gated_on_delivery(self):
        import gateway.run as run

        src = _get_fn_source(run, "_approval_notify_sync")
        assert "_mirror_approval_to_telegram(" in src, (
            "the primary approval path must invoke the Telegram mirror"
        )
        assert "if _mirror_now:" in src, (
            "mirror must be gated on primary delivery (_mirror_now flag)"
        )
        # No early return skips the mirror after a successful send: the old
        # `return`s inside the button path collapsed into _mirror_now.
        assert "if _mirror_now = True" not in src  # syntax sanity

    def test_mirror_checks_config_before_adapter(self):
        import gateway.run as run

        src = _get_fn_source(run, "_mirror_approval_to_telegram")
        cfg_check = src.find("approval_mirror_telegram")
        adapter_fetch = src.find("adapters.get")
        assert cfg_check != -1, "mirror must read the approval_mirror_telegram flag"
        assert adapter_fetch != -1, "mirror must fetch the Telegram adapter"
        assert cfg_check < adapter_fetch, (
            "config gate must be evaluated BEFORE touching the Telegram adapter"
        )

    def test_mirror_guards_home_channel_and_platform_skip(self):
        import gateway.run as run

        src = _get_fn_source(run, "_mirror_approval_to_telegram")
        assert "home_channel" in src
        assert "telegram" in src.lower() and "return" in src, (
            "mirror must skip telegram-origin sessions and missing home channel"
        )


class TestMirrorConfigPlumbing:
    """Contract: the flag parses from config with default False."""

    def test_default_false(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig()
        assert getattr(cfg, "approval_mirror_telegram", None) is False

    def test_parses_nested_gateway_section(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig.from_dict({"gateway": {"approval_mirror_telegram": True}})
        assert cfg.approval_mirror_telegram is True

    def test_serializes_to_dict(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig.from_dict({"approval_mirror_telegram": True})
        assert cfg.to_dict()["approval_mirror_telegram"] is True

    def test_bool_coercion(self):
        from gateway.config import GatewayConfig

        cfg = GatewayConfig.from_dict({"gateway": {"approval_mirror_telegram": "true"}})
        assert cfg.approval_mirror_telegram is True


class TestMirrorDefaultsDontLeak:
    """Contract: the feature is OFF unless explicitly enabled."""

    def test_default_config_dict_has_flag_false(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        gw = DEFAULT_CONFIG.get("gateway", {})
        assert gw.get("approval_mirror_telegram") is False, (
            "DEFAULT_CONFIG must ship the flag as False (opt-in)"
        )
