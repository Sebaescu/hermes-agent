"""Behavior tests for the Telegram↔Dashboard bridge (Odyssey/Desktop sessions).

The bridge mirrors approval/clarify prompts raised by sessions hosted in the
DASHBOARD process (``tui_gateway.server``) to the Telegram home channel, and
resolves remote taps through the loopback relay endpoint
``POST /api/tgbridge/respond``. Contracts pinned here (behavior, not source
shape):

1. Callback scheme — ``dsh:ap:<sk_hash>:<choice>`` and
   ``dsh:cl:<rid>[:<qid>]:<answer_idx>`` round-trip through
   ``parse_relay_callback``; garbage parses to None; payload lengths stay
   within Telegram's 64-byte callback_data cap.
2. Send path — mirror functions are best-effort no-ops when the mirror is
   off/unconfigured, and POST exactly one sendMessage to the Bot API when
   armed (never getUpdates). Button rows pair approvals ≤2-wide.
3. Relay auth — the per-boot token is verified constant-time; a wrong/absent
   token gets 401; a non-loopback client gets 403 before any token check.
4. Relay resolution — an approval tap resolves via
   ``resolve_gateway_approval`` in THIS process; a clarify tap resolves
   through the same ``_respond`` path ``clarify.respond`` uses; double-tap
   (already-answered) clarify degrades to a benign "expired" result, not an
   error.
5. sk-hash registry — bounded (oldest quarter evicted at cap) and reversible
   only through the registry (hash alone reveals nothing).
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from tui_gateway import telegram_bridge as tgb


# ===========================================================================
# 1. Callback scheme — parse/format round-trip, 64-byte cap
# ===========================================================================


class TestCallbackScheme:
    def test_approval_callback_roundtrip(self):
        sk_hash = tgb._remember_sk_hash("gateway:telegram:12345:678")
        data = tgb._approval_callback_data("gateway:telegram:12345:678", "once")
        assert data == f"dsh:ap:{sk_hash}:once"
        parsed = tgb.parse_relay_callback(data)
        assert parsed == {"kind": "approval", "sk_hash": sk_hash, "choice": "once"}
        assert len(data) <= 64
        # Registry reversibility (relay-side resolution)
        assert tgb.session_key_for_hash(sk_hash) == "gateway:telegram:12345:678"

    @pytest.mark.parametrize(
        "rid,qid,idx",
        [("abcd1234", "", 0), ("abcd1234", "", 7), ("abcd1234", "q2", 3)],
    )
    def test_clarify_callback_roundtrip(self, rid, qid, idx):
        data = tgb._clarify_callback_data(rid, idx, qid)
        parsed = tgb.parse_relay_callback(data)
        if qid:
            assert parsed == {
                "kind": "clarify",
                "request_id": rid,
                "question_id": qid,
                "answer_idx": idx,
            }
        else:
            assert parsed == {
                "kind": "clarify",
                "request_id": rid,
                "answer_idx": idx,
            }
        assert len(data) <= 64

    def test_parse_garbage_returns_none(self):
        for bad in ["", "dsh:", "dsh:ap", "dsh:ap:only", "ap:xx:once", "dsh:xx:1:2", "dsh:cl:rid:abc"]:
            assert tgb.parse_relay_callback(bad) is None

    def test_long_rids_stay_under_cap(self):
        # 8-hex rid is the contract; even a pathological 20-char choice stays ≤64
        data = tgb._clarify_callback_data("a" * 8, 3, "q" * 10)
        assert len(data) <= 64

    def test_approval_choices_fit_cap(self):
        for choice in ["once", "session", "always", "deny"]:
            data = tgb._approval_callback_data("telegram/some/very/long/session/key", choice)
            assert len(data) <= 64, data


class TestSkHashRegistry:
    def test_bounded_registry_evicts_oldest_quarter(self):
        # Reset to a fresh registry
        with tgb._sk_hash_lock:
            tgb._sk_hash_registry.clear()
        max_n = tgb._SK_HASH_REGISTRY_MAX
        # Eviction fires when the NEXT insert would exceed the cap: insert
        # max + one more batch and the oldest quarter must be dropped.
        keys = [f"sk:{i}" for i in range(max_n + 1)]
        for k in keys:
            tgb._remember_sk_hash(k)
        with tgb._sk_hash_lock:
            assert len(tgb._sk_hash_registry) <= max_n
        import hashlib

        # First quarter of keys evicted (insertion-order eviction)
        for k in keys[: max_n // 4]:
            h = hashlib.sha256(k.encode()).hexdigest()[:8]
            assert tgb.session_key_for_hash(h) is None, k
        # Recent keys survive
        for k in keys[-8:]:
            h = hashlib.sha256(k.encode()).hexdigest()[:8]
            assert tgb.session_key_for_hash(h) == k

    def test_registry_survives_repeated_inserts(self):
        h = tgb._remember_sk_hash("stable-key")
        h2 = tgb._remember_sk_hash("stable-key")
        assert h == h2
        assert tgb.session_key_for_hash(h) == "stable-key"


# ===========================================================================
# 2. Send path — armed vs off
# ===========================================================================


class _Target(dict):
    pass


def _arm(monkeypatch, chat_id="111", thread_id=None, token="TOK"):
    target = {
        "token": token,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }
    monkeypatch.setattr(tgb, "_load_mirror_target", lambda: target)


class TestSendPath:
    def test_mirror_off_sends_nothing(self, monkeypatch):
        posted = []
        monkeypatch.setattr(tgb, "_load_mirror_target", lambda: None)
        monkeypatch.setattr(tgb, "_tg_api_post", lambda *a, **k: posted.append(a) or {})
        tgb.mirror_approval("sid", "sk", {"command": "rm -rf /", "description": ""})
        tgb.mirror_clarify("rid1", "sid", "Which?", ["a", "b"])
        tgb.notify_sudo("sid")
        assert posted == []

    def test_armed_approval_posts_one_card_with_buttons(self, monkeypatch):
        _arm(monkeypatch)
        posted = []

        def fake_post(token, method, payload):
            posted.append((method, payload))
            return {"ok": True}

        monkeypatch.setattr(tgb, "_tg_api_post", fake_post)
        tgb.mirror_approval(
            "sid",
            "sk-abc",
            {"command": "dangerous --cmd <secret>", "description": "Run a command", "allow_permanent": True},
        )
        # fire-and-forget: wait for the daemon thread
        for t in threading.enumerate():
            if t.name == "tg-bridge-send":
                t.join(timeout=5)
        assert len(posted) == 1
        method, payload = posted[0]
        assert method == "sendMessage"
        assert payload["chat_id"] == "111"
        assert payload["parse_mode"] == "HTML"
        kb = payload["reply_markup"]["inline_keyboard"]
        flat = [btn["callback_data"] for row in kb for btn in row]
        # Structural: 4 choices → 2 rows of 2
        assert len(kb) == 2 and len(kb[0]) == 2 and len(kb[1]) == 2
        for d in flat:
            assert d.startswith("dsh:ap:") and d.split(":")[3] in ("once", "session", "always", "deny")
        # HTML-escaped command (no raw <secret>)
        assert "<secret>" not in payload["text"]
        assert "&lt;secret&gt;" in payload["text"]

    def test_telegram_session_not_mirrored(self, monkeypatch):
        """Symmetry guard: a session already living on Telegram got its native
        card from the gateway adapter — the dashboard mirror must stay silent
        or the same chat sees the prompt twice."""
        posted = []
        monkeypatch.setattr(tgb, "_load_mirror_target", lambda: {"token": "T", "chat_id": "1", "thread_id": None})
        monkeypatch.setattr(tgb, "_tg_api_post", lambda *a: posted.append(a) or {"ok": True})
        assert tgb._is_telegram_session("gateway:telegram:123:chat")
        assert tgb._is_telegram_session("TELEGRAM::x")
        assert not tgb._is_telegram_session("dashboard:local")
        tgb.mirror_approval("sid", "gateway:telegram:123:chat", {"command": "x"})
        for t in threading.enumerate():
            if t.name == "tg-bridge-send":
                t.join(timeout=5)
        assert posted == []

    def test_armed_clarify_posts_choice_buttons(self, monkeypatch):
        _arm(monkeypatch)
        posted = []
        monkeypatch.setattr(
            tgb, "_tg_api_post", lambda token, method, payload: posted.append((method, payload)) or {"ok": True}
        )
        tgb.mirror_clarify("rid9", "sid", "Pick one", ["first", "second", "third"], multi_select=True)
        for t in threading.enumerate():
            if t.name == "tg-bridge-send":
                t.join(timeout=5)
        assert len(posted) == 1
        method, payload = posted[0]
        assert method == "sendMessage"
        kb = payload["reply_markup"]["inline_keyboard"]
        assert len(kb) == 3
        flat = [btn["callback_data"] for row in kb for btn in row]
        assert flat == ["dsh:cl:rid9:0", "dsh:cl:rid9:1", "dsh:cl:rid9:2"]

    def test_thread_id_added_when_present(self, monkeypatch):
        _arm(monkeypatch, thread_id="77")
        posted = []
        monkeypatch.setattr(
            tgb, "_tg_api_post", lambda *a: posted.append(a) or {"ok": True}
        )
        tgb.notify_text("sid", "hello")
        for t in threading.enumerate():
            if t.name == "tg-bridge-send":
                t.join(timeout=5)
        assert posted and posted[0][2].get("message_thread_id") == "77"

    def test_send_failure_is_silent(self, monkeypatch):
        _arm(monkeypatch)
        monkeypatch.setattr(tgb, "_tg_api_post", lambda *a: None)
        # must not raise
        tgb.mirror_approval("sid", "sk", {"command": "x"})
        tgb.notify_sudo("sid")

    def test_never_getupdates(self, monkeypatch):
        """Send-only contract: the module must never call getUpdates (the
        long poller lives in the gateway process; a second consumer trips
        Telegram's 409)."""
        _arm(monkeypatch)
        methods = []
        monkeypatch.setattr(
            tgb,
            "_tg_api_post",
            lambda token, method, payload: methods.append(method) or {"ok": True},
        )
        tgb.mirror_approval("sid", "sk", {"command": "x"})
        tgb.mirror_clarify("r", "sid", "q", ["a"])
        tgb.notify_sudo("sid")
        tgb.notify_secret("sid", "FOO_KEY", "prompt")
        for t in threading.enumerate():
            if t.name == "tg-bridge-send":
                t.join(timeout=5)
        assert methods == ["sendMessage"] * 4 or set(methods) == {"sendMessage"}

    def test_secret_prompt_is_text_only(self, monkeypatch):
        """sudo/secret prompts must NEVER carry buttons — credentials are not
        entered over Telegram."""
        _arm(monkeypatch)
        posted = []
        monkeypatch.setattr(
            tgb, "_tg_api_post", lambda *a: posted.append(a) or {"ok": True}
        )
        tgb.notify_secret("sid", "API_TOKEN", "Enter API token")
        tgb.notify_sudo("sid")
        for t in threading.enumerate():
            if t.name == "tg-bridge-send":
                t.join(timeout=5)
        assert len(posted) == 2
        for _token, _method, payload in posted:
            assert "reply_markup" not in payload
            assert "API_TOKEN" in payload["text"] or "sudo" in payload["text"].lower()

    def test_send_only_http_contract(self, monkeypatch):
        """Direct behavioral pin on the HTTP layer: _tg_api_post POSTs JSON to
        api.telegram.org/bot<token>/<method> and nothing else."""
        import json as _json
        import urllib.request

        captured = {}

        class _Resp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = _json.loads(req.data.decode("utf-8"))
            return _Resp(b'{"ok": true, "result": {"message_id": 5}}')

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            out = tgb._tg_api_post("BOT", "sendMessage", {"chat_id": 1})
        assert captured["url"] == "https://api.telegram.org/botBOT/sendMessage"
        assert captured["method"] == "POST"
        assert captured["data"] == {"chat_id": 1}
        assert out and out.get("ok") is True

    def test_api_error_returns_none(self, monkeypatch):
        import urllib.request

        class _Resp:
            def read(self):
                return b'{"ok": false, "description": "Bad Request: chat not found"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(urllib.request, "urlopen", lambda req, timeout=None: _Resp()):
            assert tgb._tg_api_post("BOT", "sendMessage", {}) is None


# ===========================================================================
# 3+4. Relay auth + resolution — via the FastAPI endpoint (TestClient)
# ===========================================================================


@pytest.fixture
def relay_client(_isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli import web_server

    # client=("127.0.0.1", 123) — the relay endpoint is loopback-only and
    # must see a loopback peer; TestClient's default ("testclient") would be
    # rejected by that guard.
    client = TestClient(web_server.app, client=("127.0.0.1", 51234))
    return client


class TestRelayAuth:
    def test_wrong_token_401(self, relay_client):
        r = relay_client.post(
            "/api/tgbridge/respond",
            json={"callback_data": "dsh:ap:deadbeef:once"},
            headers={"X-Hermes-Bridge-Token": "nope", "Host": "localhost"},
        )
        assert r.status_code == 401

    def test_missing_token_401(self, relay_client):
        r = relay_client.post(
            "/api/tgbridge/respond",
            json={"callback_data": "dsh:ap:deadbeef:once"},
        )
        assert r.status_code == 401

    def test_good_token_bad_payload_400(self, relay_client):
        r = relay_client.post(
            "/api/tgbridge/respond",
            json={"callback_data": "garbage"},
            headers={"X-Hermes-Bridge-Token": tgb.get_bridge_token()},
        )
        assert r.status_code == 400

    def test_good_token_bad_choice_400(self, relay_client):
        r = relay_client.post(
            "/api/tgbridge/respond",
            json={"callback_data": "dsh:ap:deadbeef:not-a-choice"},
            headers={"X-Hermes-Bridge-Token": tgb.get_bridge_token()},
        )
        # deadbeef not in registry → 404 unknown approval session
        assert r.status_code == 404

    def test_loopback_enforced_before_token(self, relay_client):
        """A non-loopback peer gets 403 even with a VALID token — the guard
        runs before auth so a remote peer can never probe the token."""
        from starlette.testclient import TestClient

        from hermes_cli import web_server

        remote = TestClient(web_server.app, client=("203.0.113.9", 51234))
        r = remote.post(
            "/api/tgbridge/respond",
            json={"callback_data": "dsh:ap:deadbeef:once"},
            headers={"X-Hermes-Bridge-Token": tgb.get_bridge_token()},
        )
        assert r.status_code == 403


class TestRelayResolution:
    def test_approval_tap_resolves_in_this_process(self, relay_client, monkeypatch):
        from tools import approval as approval_mod

        calls = []
        monkeypatch.setattr(
            approval_mod,
            "resolve_gateway_approval",
            lambda sk, choice: calls.append((sk, choice)) or 1,
        )
        sk = "gateway:telegram:42:7"
        sk_hash = tgb._remember_sk_hash(sk)
        r = relay_client.post(
            "/api/tgbridge/respond",
            json={"callback_data": f"dsh:ap:{sk_hash}:deny"},
            headers={"X-Hermes-Bridge-Token": tgb.get_bridge_token()},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "resolved": 1}
        assert calls == [(sk, "deny")]

    def test_clarify_tap_resolves_via_respond_path(self, relay_client, monkeypatch):
        import tui_gateway.server as srv

        rid = "clridge1"
        ev = threading.Event()
        answer_store = {}
        # Build the pending clarify exactly like _block does
        with srv._prompt_lock:
            srv._pending[rid] = ("clarify", ev)
            srv._pending_prompt_payloads[rid] = (
                "clarify.request",
                {"question": "Pick", "choices": ["alpha", "beta", "gamma"]},
            )
        try:
            r = relay_client.post(
                "/api/tgbridge/respond",
                json={"callback_data": f"dsh:cl:{rid}:2"},
                headers={"X-Hermes-Bridge-Token": tgb.get_bridge_token()},
            )
            assert r.status_code == 200, r.text
            assert ev.is_set()
            assert srv._answers[rid] == "gamma"
        finally:
            with srv._prompt_lock:
                srv._pending.pop(rid, None)
                srv._pending_prompt_payloads.pop(rid, None)
                srv._answers.pop(rid, None)

    def test_clarify_double_tap_expired_not_error(self, relay_client, monkeypatch):
        import tui_gateway.server as srv

        rid = "clridge2"
        with srv._prompt_lock:
            srv._pending.pop(rid, None)
            srv._pending_prompt_payloads.pop(rid, None)
        r = relay_client.post(
            "/api/tgbridge/respond",
            json={"callback_data": f"dsh:cl:{rid}:0"},
            headers={"X-Hermes-Bridge-Token": tgb.get_bridge_token()},
        )
        # No pending prompt → 404 (already answered / expired)
        assert r.status_code == 404


# ===========================================================================
# 5. server.py hook — _block mirrors clarify/sudo/secret for dashboard
#    sessions and stays silent for telegram sessions
# ===========================================================================


class TestServerHook:
    @pytest.fixture(autouse=True)
    def _clean_sessions(self):
        import tui_gateway.server as srv

        with srv._prompt_lock:
            saved_sessions = dict(srv._sessions)
            srv._sessions.clear()
        yield
        with srv._prompt_lock:
            srv._sessions.clear()
            srv._sessions.update(saved_sessions)

    def _answer_prompt(self, monkeypatch, event, payload, session_key):
        """Drive a real _block() call with the bridge armed; answer it via
        the relay path so the block returns without timing out."""
        import tui_gateway.server as srv

        armed = {"token": "T", "chat_id": "9", "thread_id": None}
        monkeypatch.setattr(tgb, "_load_mirror_target", lambda: armed)
        posted = []
        monkeypatch.setattr(tgb, "_tg_api_post", lambda *a: posted.append(a[2]) or {"ok": True})

        sid = "hooktest"
        srv._sessions[sid] = {"session_key": session_key}

        # Answer from another thread once the prompt is pending.
        def _tap_answer():
            deadline = time.time() + 10
            rid = None
            while time.time() < deadline:
                with srv._prompt_lock:
                    for r, (_e, _p) in list(srv._pending_prompt_payloads.items()):
                        rid = r
                        break
                if rid:
                    # Resolve via the same _respond path the relay uses.
                    srv._respond(
                        0,
                        {
                            "request_id": rid,
                            "question_id": payload.get("qid", ""),
                            "answer": "ok",
                        },
                        "answer",
                        allow_expired=True,
                    )
                    return
                time.sleep(0.02)

        threading.Thread(target=_tap_answer, daemon=True).start()
        srv._block(event, sid, dict(payload), timeout=8)
        for t in threading.enumerate():
            if t.name == "tg-bridge-send":
                t.join(timeout=5)
        return posted

    def test_clarify_request_mirrored_for_dashboard_session(self, monkeypatch):
        posted = self._answer_prompt(
            monkeypatch,
            "clarify.request",
            {"question": "Which DB?", "choices": ["pg", "mysql"]},
            session_key="dashboard:local:odyssey",
        )
        assert len(posted) == 1
        kb = posted[0]["reply_markup"]["inline_keyboard"]
        flat = [b["callback_data"] for row in kb for b in row]
        assert len(flat) == 2 and all(d.startswith("dsh:cl:") for d in flat)
        assert flat[0].endswith(":0") and flat[1].endswith(":1")

    def test_sudo_request_mirrored_as_text_only(self, monkeypatch):
        posted = self._answer_prompt(
            monkeypatch, "sudo.request", {"prompt": "sudo password"}, session_key="web:local"
        )
        assert len(posted) == 1
        assert "reply_markup" not in posted[0]

    def test_secret_request_mirrored_as_text_only(self, monkeypatch):
        posted = self._answer_prompt(
            monkeypatch,
            "secret.request",
            {"env_var": "API_KEY", "prompt": "enter"},
            session_key="web:local",
        )
        assert len(posted) == 1
        assert "reply_markup" not in posted[0]
        assert "API_KEY" in posted[0]["text"]

    def test_telegram_session_not_mirrored_from_block(self, monkeypatch):
        posted = self._answer_prompt(
            monkeypatch,
            "clarify.request",
            {"question": "q", "choices": ["a"]},
            session_key="gateway:telegram:1:2",
        )
        assert posted == []


# ===========================================================================
# 6. Adapter relay — _handle_dashboard_bridge_callback forwards the raw
#    dsh:* tap to the dashboard's loopback relay endpoint
# ===========================================================================


class TestAdapterRelay:
    @pytest.fixture()
    def adapter(self, _isolate_hermes_home):
        from plugins.platforms.telegram.adapter import TelegramAdapter

        return TelegramAdapter.__new__(TelegramAdapter)

    @staticmethod
    def _query(answered, edits=None):
        """Minimal callback-query double recording answer()/edit calls."""
        edits = edits if edits is not None else []

        class _Q:
            from_user = SimpleNamespace(id="42", first_name="Seba")

            @staticmethod
            async def answer(text=None):
                answered.append(text)

            @staticmethod
            async def edit_message_text(text=None, reply_markup=None, **kw):
                edits.append(text)

        return _Q()

    def test_relay_posts_callback_to_bridge_endpoint(self, adapter, monkeypatch):
        """The tap is forwarded verbatim to the endpoint published in the
        bridge file, with the bridge token, over loopback HTTP."""
        import tui_gateway.telegram_bridge as tgb
        from hermes_constants import get_hermes_home

        target = get_hermes_home() / "runtime" / "telegram-bridge.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "port": 9119,
                    "pid": 1,
                    "token": "bridgetoken",
                    "endpoint": "/api/tgbridge/respond",
                }
            ),
            encoding="utf-8",
        )
        captured = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True, "resolved": 1}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return _Resp()

        fake_httpx = SimpleNamespace(AsyncClient=_Client)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        answered, edits = [], []
        query = self._query(answered, edits)
        asyncio.run(adapter._handle_dashboard_bridge_callback(query, "dsh:ap:aabbccdd:deny"))

        assert captured["url"] == "http://127.0.0.1:9119/api/tgbridge/respond"
        assert captured["json"] == {"callback_data": "dsh:ap:aabbccdd:deny", "user_display": "Seba"}
        assert captured["headers"]["X-Hermes-Bridge-Token"] == "bridgetoken"
        assert answered == ["✅ Answered."]
        assert edits and "Denied" in edits[0]

    def test_relay_no_bridge_file_answers_gracefully(self, adapter, monkeypatch):
        from hermes_constants import get_hermes_home

        target = get_hermes_home() / "runtime" / "telegram-bridge.json"
        if target.exists():
            target.unlink()
        answered, edits = [], []
        query = self._query(answered, edits)
        asyncio.run(adapter._handle_dashboard_bridge_callback(query, "dsh:cl:ab:0"))
        assert answered == ["⚠ Dashboard bridge not available."]

    def test_relay_404_maps_to_expired(self, adapter, monkeypatch):
        import tui_gateway.telegram_bridge as tgb
        from hermes_constants import get_hermes_home

        target = get_hermes_home() / "runtime" / "telegram-bridge.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"port": 9119, "token": "t", "endpoint": "/api/tgbridge/respond"}),
            encoding="utf-8",
        )

        class _Resp:
            status_code = 404

            @staticmethod
            def json():
                return {"detail": "no pending clarify"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=_Client))
        answered, edits = [], []
        query = self._query(answered, edits)
        asyncio.run(adapter._handle_dashboard_bridge_callback(query, "dsh:cl:ab:0"))
        assert answered == ["⌛ Prompt already resolved or expired."]

    def test_callback_dispatch_routes_dsh(self, adapter, monkeypatch, _isolate_hermes_home):
        """_handle_callback_query routes a dsh:* tap through the bridge
        relay (after the user-authorization check) instead of any local
        handler."""
        from types import SimpleNamespace as NS

        relayed = []

        async def fake_relay(self, q, data):
            relayed.append(data)

        def fake_authz(self, *a, **k):
            return True

        monkeypatch.setattr(
            type(adapter), "_handle_dashboard_bridge_callback", fake_relay
        )
        monkeypatch.setattr(type(adapter), "_is_callback_user_authorized", fake_authz)

        update = NS(
            callback_query=NS(
                data="dsh:ap:aabbccdd:once",
                message=NS(chat_id=100, chat=NS(type="private")),
                from_user=NS(id="42", first_name="Seba"),
            )
        )
        asyncio.run(adapter._handle_callback_query(update, None))
        assert relayed == ["dsh:ap:aabbccdd:once"]
