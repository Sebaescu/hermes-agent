"""Tarjeta TG de approval se marca al resolverse en cualquier canal.

Seb 01/09: aprobaba en Desktop y la tarjeta de Telegram quedaba con
botones vivos (muerta por dentro) — saturaba el chat. El bridge ahora
registra la última tarjeta por sesión y `resolve_gateway_approval` la
edita: botones fuera + "✅ Resuelto".
"""
import hashlib

from tools.approval import resolve_gateway_approval
from tui_gateway import telegram_bridge as tb


def _sk() -> str:
    return "web:cardresolve-test-session"


def _mk_queue(monkeypatch):
    """Queue real del engine con una entrada esperando."""
    from tools import approval as ap
    from tools.approval import _ApprovalEntry

    sk = _sk()
    entry = _ApprovalEntry({
        "request_id": "req_card_1",
        "command": "rm -rf /tmp/x",
        "description": "test card resolve",
        "session_key": sk,
    })
    monkeypatch.setattr(
        ap, "_gateway_queues", {sk: [entry]}, raising=False
    )
    return sk, entry


def test_resolve_edits_live_card(monkeypatch):
    sk, entry = _mk_queue(monkeypatch)
    sk_hash = hashlib.sha256(sk.encode()).hexdigest()[:8]
    edits = []

    tb._LIVE_CARDS[sk_hash] = {
        "chat_id": 8479321670,
        "message_id": 4242,
        "text": "🔐 Approval needed — dashboard session\n<b>test</b>",
    }
    monkeypatch.setattr(tb, "_tg_api_post",
                        lambda token, method, payload: edits.append((method, payload)))
    monkeypatch.setattr(tb, "_load_mirror_target",
                        lambda: {"token": "t", "chat_id": 8479321670})

    n = resolve_gateway_approval(sk, "once")
    assert n == 1
    # el edit corre en daemon thread — esperar a que consuma la lista
    import time
    for _ in range(50):
        if edits:
            break
        time.sleep(0.02)
    assert edits, "editMessageText nunca disparó"
    method, payload = edits[0]
    assert method == "editMessageText"
    assert payload["message_id"] == 4242
    assert "✅ Resuelto" in payload["text"]
    assert sk_hash not in tb._LIVE_CARDS


def test_resolve_sin_card_es_noop(monkeypatch):
    sk, _ = _mk_queue(monkeypatch)
    # sin tarjeta registrada: no debe reventar ni editar nada
    calls = []
    monkeypatch.setattr(tb, "_tg_api_post",
                        lambda t, m, p: calls.append(m))
    n = resolve_gateway_approval(sk, "deny")
    assert n == 1
    import time
    time.sleep(0.1)
    assert calls == []
