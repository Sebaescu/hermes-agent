#!/usr/bin/env python3
"""Test T1: bloque de contexto temporal en _get_system_prompt_for_channel."""
import sys

sys.path.insert(0, "/home/sebaescu/.hermes/hermes-agent")

from zoneinfo import ZoneInfo
from datetime import datetime

from gateway.run import GatewayRunner
import gateway.run as run_mod


def _runner(cfg):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._ephemeral_system_prompt = "PROMPT_BASE"
    runner._config = cfg
    run_mod._get_channel_override = lambda *a, **k: None
    return runner


def test_block_appended():
    """Prompt base + whatsapp → termina con bloque [Contexto: ...] en español."""
    runner = _runner({"display": {"temporal_context_platforms": ["whatsapp", "telegram"]}})
    out = runner._get_system_prompt_for_channel(
        run_mod.Platform.WHATSAPP, "59399@test"
    )
    assert out.startswith("PROMPT_BASE"), out[:50]
    assert "[Contexto:" in out and "hora local (GMT-5)" in out, out
    now = datetime.now(ZoneInfo("America/Guayaquil"))
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    assert dias[now.weekday()] in out
    print("OK bloque presente:", out.split("[Contexto:")[1][:55])


def test_disabled_by_config():
    runner = _runner({"display": {"temporal_context": False}})
    out = runner._get_system_prompt_for_channel(run_mod.Platform.WHATSAPP, "x@test")
    assert out == "PROMPT_BASE", out
    print("OK off → sin bloque")


def test_platform_not_in_list():
    runner = _runner({"display": {"temporal_context_platforms": ["telegram"]}})
    out = runner._get_system_prompt_for_channel(run_mod.Platform.WHATSAPP, "x@test")
    assert out == "PROMPT_BASE", out
    print("OK plataforma fuera de lista → sin bloque")


if __name__ == "__main__":
    test_block_appended()
    test_disabled_by_config()
    test_platform_not_in_list()
    print("T1 TESTS PASS")
