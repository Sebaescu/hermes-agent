#!/usr/bin/env python3
"""Test T4: sweep anti-leak — emisores de status interno vs chats de cliente."""
import sys

sys.path.insert(0, "/home/sebaescu/.hermes/hermes-agent")

import gateway.run as run_mod
from agent.background_review import summarize_background_review_actions


def test_bg_review_off_suppresses_actions():
    """memory_notifications=off → actions=[] → callback de bg-review nunca dispara."""
    acts = summarize_background_review_actions([], [], notification_mode="off")
    assert acts == [], acts
    print("OK bg-review off → actions vacías (callback no se invoca)")


def test_prepare_status_no_raw_for_whatsapp():
    """_prepare_gateway_status_message: WhatsApp NUNCA recibe raw diagnostics."""
    raw = "compression summary failed; fallback context marker set"
    out = run_mod._prepare_gateway_status_message(run_mod.Platform.WHATSAPP, "status", raw)
    # raw text platforms: local/api_server/webhook — whatsapp NO es raw platform,
    # y noisy-status regex filtra compression chatter
    assert out is None or "fallback context marker" not in out, out
    print("OK whatsapp filtra compression chatter →", repr(out))


def test_notice_gate_source():
    """El gate client_facing_platforms está presente en el notice callback."""
    src = open("/home/sebaescu/.hermes/hermes-agent/gateway/run.py").read()
    assert "client_facing_platforms" in src, "falta gate"
    assert 'logger.debug("notice suppressed for client-facing platform")' in src
    print("OK notice gate presente en _notice_callback_sync")


if __name__ == "__main__":
    test_bg_review_off_suppresses_actions()
    test_prepare_status_no_raw_for_whatsapp()
    test_notice_gate_source()
    print("T4 TESTS PASS")
