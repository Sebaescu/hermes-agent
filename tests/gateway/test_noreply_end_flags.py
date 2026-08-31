#!/usr/bin/env python3
"""Test T5/T6: [[NOREPLY]] silencio + [[_END]] cierre conversación en adapter WA.

Corre el adapter.send() contra un fake bridge HTTP (aiohttp test server) y
verifica: NOREPLY no hace POST /send; _END hace POST /conversation/stop.
"""
import asyncio
import json
import sys

sys.path.insert(0, "/home/sebaescu/.hermes/hermes-agent")

from aiohttp import web


async def main():
    calls = {"send": 0, "stop": 0}

    async def send(request):
        calls["send"] += 1
        return web.json_response({"success": True, "messageId": f"m{calls['send']}"})

    async def stop(request):
        calls["stop"] += 1
        return web.json_response({"success": True})

    app = web.Application()
    app.router.add_post("/send", send)
    app.router.add_post("/conversation/stop", stop)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 3987)
    await site.start()

    # adapter con __new__ (sin conectar bridge real)
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    ad = WhatsAppAdapter.__new__(WhatsAppAdapter)
    ad._running = True
    ad._bridge_port = 3987
    ad._bridge_process = None  # no managed bridge en test
    ad._reply_prefix = None
    ad._outgoing_chunk_limit_cache = None

    from aiohttp import ClientSession
    ad._http_session = ClientSession()

    # 1. NOREPLY → cero POST /send
    r1 = await ad.send("593991318555@s.whatsapp.net", "[[NOREPLY]]")
    assert r1.success and r1.message_id is None, r1
    assert calls["send"] == 0, calls
    print("OK [[NOREPLY]] → 0 POST /send")

    # 2. texto normal → 2 POST /send (2 burbujas), 0 stop
    r2 = await ad.send("593991318555@s.whatsapp.net", "hola, como vas con la web?\nlisto te cuento avanzamos bien hoy")
    assert r2.success and calls["send"] == 2, (r2, calls)
    assert calls["stop"] == 0, calls
    print("OK texto normal → 2 send (burbujas), 0 stop")

    # 3. [[_END]] con despedida → send (1 burbuja) + conversation/stop
    r3 = await ad.send("593991318555@s.whatsapp.net", "listo debby, hablamos luego\n[[_END]]")
    assert r3.success, r3
    assert calls["send"] == 3, calls  # acumulativo: 2 del caso anterior + 1
    assert calls["stop"] == 1, calls
    print("OK [[_END]] → send + conversation/stop")

    # 4. sticker-directive-only + NOREPLY edge: no crashea
    r4 = await ad.send("593991318555@s.whatsapp.net", "[[NOREPLY]]")
    assert r4.success
    print("OK NOREPLY repetido estable")

    await ad._http_session.close()
    await runner.cleanup()
    print("T5/T6 TESTS PASS")


asyncio.run(main())
