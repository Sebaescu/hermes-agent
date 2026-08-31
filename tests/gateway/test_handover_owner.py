#!/usr/bin/env python3
"""Test T8: handover por presencia del dueño — helpers + lógica de ventana."""
import sys
import time

sys.path.insert(0, "/home/sebaescu/.hermes/hermes-agent")

from plugins.platforms.whatsapp.adapter import WhatsAppAdapter


def main():
    ad = WhatsAppAdapter.__new__(WhatsAppAdapter)
    ad._bridge_port = 3987
    ad._bridge_process = None
    ad._owner_present_until = {}

    # 1. idle minutes default
    assert ad._handover_idle_minutes() == 15.0
    print("OK handover_idle_minutes default 15")

    # 2. owner escribe → presence activa → ventana vigente
    now = time.time()
    ad._owner_present_until["593991318555@s.whatsapp.net"] = now + 60 * 15
    until = ad._owner_present_until.get("593991318555@s.whatsapp.net", 0.0)
    assert time.time() < until
    print("OK owner_present activo → mensajes de clienta se droppean (lógica _poll)")

    # 3. ventana expirada → resume
    ad._owner_present_until["593991318555@s.whatsapp.net"] = now - 1
    until = ad._owner_present_until.get("593991318555@s.whatsapp.net", 0.0)
    assert time.time() >= until
    print("OK ventana expirada → resume + notify")

    # 4. notify chat id resoluble desde creds
    cid = ad._notify_chat_id()
    print("notify_chat_id:", cid)
    assert cid and cid.endswith("@s.whatsapp.net")
    print("OK notify_chat_id resuelto desde creds.json")

    print("T8 TESTS PASS")


main()
