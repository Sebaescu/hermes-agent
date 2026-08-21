"""Telegram mirror bridge for dashboard-hosted sessions (Odyssey / Desktop).

Odyssey and Hermes-Desktop sessions live in the DASHBOARD process
(``hermes dashboard`` / ``hermes serve`` → ``tui_gateway.server._make_agent``),
NOT in the messaging gateway. The approval mirror already committed for the
messaging gateway (``gateway.approval_mirror_telegram`` in ``gateway/run.py``
TurnRunner) therefore never fires for those sessions — this module closes
that gap from the dashboard side:

* **Send (this module).** When ``gateway.approval_mirror_telegram`` is true,
  approval / clarify / sudo-secret prompts blocking a dashboard session are
  ALSO mirrored to the Telegram home channel as an inline-keyboard card, via
  a direct HTTPS POST to the Bot API. Send-only by design: this process must
  NEVER call ``getUpdates`` — the long poller lives in the gateway process
  and a second consumer would trip Telegram's 409 conflict.
* **Relay (telegram adapter).** Buttons carry a ``dsh:`` callback prefix.
  ``plugins/platforms/telegram/adapter.py`` sees that unknown prefix and
  forwards the callback to the dashboard's ``POST /api/tgbridge/respond``
  (loopback), authenticated with the bridge token this process publishes in
  ``<HERMES_HOME>/runtime/telegram-bridge.json``.
* **Resolve (dashboard).** The relay endpoint resolves approvals via
  ``tools.approval.resolve_gateway_approval`` (module-level state in THIS
  process) and clarifies via the same ``_respond`` path ``clarify.respond``
  uses — so a remote tap is indistinguishable from a TUI answer.

Config resolution mirrors the gateway: the canonical
``gateway.config.load_gateway_config()`` (same precedence — env → config.yaml
→ legacy gateway.json) is the single source of truth for
``approval_mirror_telegram`` and the Telegram home channel.

Security posture:
* sudo/secret prompts are TEXT-ONLY notifications ("open it in Odyssey") —
  credentials are never accepted over Telegram.
* The relay token is a per-boot random secret in a 0600 file under
  ``<HERMES_HOME>/runtime/``; the relay endpoint is loopback-only and
  verifies it with ``hmac.compare_digest``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.tui_gateway.telegram_bridge")

# Relay-endpoint contract shared with plugins/platforms/telegram/adapter.py.
# Keep both sides in sync.
BRIDGE_INFO_FILENAME = "telegram-bridge.json"
RELAY_ENDPOINT = "/api/tgbridge/respond"

_config_lock = threading.Lock()
_config_cache: Optional[dict] = None
_config_cache_at: float = 0.0
_CONFIG_TTL = 30.0

_bridge_token: Optional[str] = None
_bridge_token_lock = threading.Lock()


def _load_mirror_target() -> Optional[dict]:
    """Resolve the Telegram mirror target, or None when mirroring is off.

    Pure decision over the canonical gateway config — mirrors
    ``gateway.run._approval_mirror_target``'s opt-in checks (config flag,
    token, home channel) minus the adapter-presence checks that only make
    sense in the gateway process. Cached briefly so the per-prompt call is
    cheap without pinning a stale config for the process lifetime.
    """
    global _config_cache, _config_cache_at
    now = time.monotonic()
    with _config_lock:
        if _config_cache is not None and (now - _config_cache_at) < _CONFIG_TTL:
            return _config_cache

    target = None
    try:
        from gateway.config import Platform as _Plat, load_gateway_config

        config = load_gateway_config()
        if not getattr(config, "approval_mirror_telegram", False):
            target = None
        else:
            tg_cfg = (getattr(config, "platforms", None) or {}).get(_Plat.TELEGRAM)
            home = (
                getattr(tg_cfg, "home_channel", None) if tg_cfg is not None else None
            )
            chat_id = getattr(home, "chat_id", None) if home is not None else None
            if chat_id:
                thread_id = getattr(home, "thread_id", None) if home is not None else None
                from hermes_cli.config import get_env_value

                token = ""
                if tg_cfg is not None and getattr(tg_cfg, "token", None):
                    token = str(tg_cfg.token)
                if not token:
                    token = get_env_value("TELEGRAM_BOT_TOKEN") or ""
                if token:
                    target = {
                        "token": token,
                        "chat_id": str(chat_id),
                        "thread_id": str(thread_id) if thread_id else None,
                    }
    except Exception:
        logger.debug("telegram bridge: mirror target resolution failed", exc_info=True)
        target = None

    with _config_lock:
        _config_cache = target
        _config_cache_at = now
    return target


def _tg_api_post(token: str, method: str, payload: dict) -> Optional[dict]:
    """POST one JSON call to the Telegram Bot API (send-only helpers only).

    Returns the parsed ``result``/error dict or None on any failure — the
    mirror is best-effort and must never break the prompt path it decorates.
    Uses urllib (stdlib) so the dashboard never pays an aiohttp/httpx import
    on this path; a blocking call is fine because the bridge already runs on
    a background thread (the notify callback fires on the agent thread and
    must not block, so callers wrap this via ``_fire_and_forget``).
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        if not body.get("ok"):
            logger.warning(
                "telegram bridge: %s failed: %s", method, body.get("description")
            )
            return None
        return body
    except Exception as exc:
        logger.debug("telegram bridge: %s error: %s", method, exc)
        return None


def _fire_and_forget(fn, *args) -> None:
    """Run fn(*args) on a daemon thread — prompt paths must never block on us."""
    t = threading.Thread(target=fn, args=args, daemon=True, name="tg-bridge-send")
    t.start()


def _is_telegram_session(session_key: str) -> bool:
    """Guard for symmetry with the gateway mirror: a session that already
    lives on Telegram got its native prompt card from the gateway adapter —
    mirroring it again here would duplicate the card in the same chat."""
    return "telegram" in str(session_key or "").lower()


def _html_escape(text: str) -> str:
    """Minimal HTML-escape for parse_mode=HTML cards."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _approval_callback_data(session_key: str, choice: str) -> str:
    """``dsh:ap:<sk_hash>:<choice>`` — compact, ≤64 bytes, no raw ids leak.

    The relay endpoint resolves the approval by session via the dashboard's
    module-level approval state; the hash scopes the button to the session
    whose prompt created it (a stale card from another session can't resolve
    this one). resolve_gateway_approval operates FIFO per session_key, so the
    choice token is the only per-tap state we need.
    """
    sk_hash = _remember_sk_hash(session_key)
    return f"dsh:ap:{sk_hash}:{choice}"


def _clarify_callback_data(rid: str, answer_idx: int, qid: str = "") -> str:
    """``dsh:cl:<rid>[:<qid>]:<answer_idx>`` — rid is the short (8-hex) id.

    Batch clarifies append the per-question ``qid`` so the relay can lock the
    individual answer via ``question_id`` (the same field
    ``clarify.respond`` uses). Single-question cards omit it.
    """
    if qid:
        return f"dsh:cl:{rid}:{qid}:{answer_idx}"
    return f"dsh:cl:{rid}:{answer_idx}"


def _send_card(text: str, rows) -> None:
    """Best-effort inline-keyboard card to the home channel (no throw).

    ``rows`` may be a zero-arg callable producing the button rows so callers
    that check the target first don't double-resolve it — the callable only
    runs when the mirror is actually armed.
    """
    target = _load_mirror_target()
    if not target:
        return
    if callable(rows):
        rows = rows()
    payload: dict[str, Any] = {
        "chat_id": target["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": rows},
    }
    if target.get("thread_id"):
        payload["message_thread_id"] = target["thread_id"]
    _fire_and_forget(_tg_api_post, target["token"], "sendMessage", payload)


def _send_text(text: str) -> None:
    target = _load_mirror_target()
    if not target:
        return
    payload: dict[str, Any] = {"chat_id": target["chat_id"], "text": text}
    if target.get("thread_id"):
        payload["message_thread_id"] = target["thread_id"]
    _fire_and_forget(_tg_api_post, target["token"], "sendMessage", payload)


def _approval_button_rows(session_key: str, choices: list[str]) -> list[list[dict]]:
    """Pair choices into ≤2-wide rows, mirroring send_exec_approval's layout."""
    labels = {
        "once": "✅ Allow Once",
        "session": "✅ Session",
        "always": "✅ Always",
        "deny": "❌ Deny",
    }
    buttons = [
        {"text": labels.get(c, c), "callback_data": _approval_callback_data(session_key, c)}
        for c in choices
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def mirror_approval(sid: str, session_key: str, data: dict | None) -> None:
    """Mirror one approval prompt as a Telegram inline card (best-effort).

    ``data`` is the raw approval payload (``command`` / ``description`` /
    ``allow_permanent`` / ``allow_session`` / ``smart_denied``) — the same
    dict the TUI ``approval.request`` payload is built from, minus the
    client-safe rewrite (we redact the command here before it leaves the
    process, reusing the gateway's redaction seam).
    """
    try:
        if not data:
            return
        if _is_telegram_session(session_key):
            return
        from gateway.run import _redact_approval_command

        command = _redact_approval_command(data.get("command", ""))
        description = data.get("description", "")
        if data.get("smart_denied"):
            choices = ["once", "deny"]
        elif data.get("allow_permanent") is False:
            choices = ["once", "session", "deny"]
        else:
            choices = ["once", "session", "always", "deny"]
        header = "🔐 Approval needed — dashboard session"
        body = (
            f"{header}\n<b>Command:</b> <code>{_html_escape(command)}</code>"
            if not description
            else f"{header}\n<b>{_html_escape(str(description))}</b>\n<code>{_html_escape(command)}</code>"
        )
        # Telegram caps messages at 4096 chars; trim the command, keep header.
        if len(body) > 3800:
            body = body[:3797] + "…"
        _send_card(
            body, lambda: _approval_button_rows(session_key, choices)
        )
    except Exception:
        logger.debug("telegram bridge: approval mirror failed", exc_info=True)


def mirror_clarify(
    rid: str,
    sid: str,
    question: str,
    choices: list,
    *,
    multi_select: bool = False,
) -> None:
    """Mirror one clarify prompt as a numbered-choice Telegram card.

    ``rid`` may carry a batch suffix (``"<rid>:<qid>"``) — parsed apart here
    so the callback locks the individual answer via ``question_id``. Answers
    come back as ``dsh:cl:...`` and resolve through the same ``_respond``
    path the TUI's ``clarify.respond`` uses. Free-text answers must still be
    typed in the TUI/Odyssey — only the listed choices are remotely
    clickable.
    """
    try:
        if not choices:
            return
        qid = ""
        if ":" in rid:
            rid, qid = rid.split(":", 1)
        q = str(question)[:3500]
        lines = ["❓ Clarify — dashboard session", "", q, ""]
        rows: list[list[dict]] = []
        for idx, choice in enumerate(choices):
            label = str(choice)[:56]
            lines.append(f"{idx + 1}. {label}")
            rows.append(
                [
                    {
                        "text": label,
                        "callback_data": _clarify_callback_data(rid, idx, qid),
                    }
                ]
            )
        if multi_select:
            lines.append("")
            lines.append("(multi-select: tap one at a time, last one confirms)")
        _send_card("\n".join(lines), rows)
    except Exception:
        logger.debug("telegram bridge: clarify mirror failed", exc_info=True)


def notify_text(sid: str, text: str) -> None:
    """Plain-text notification (sudo/secret prompts — never input)."""
    try:
        _send_text(text)
    except Exception:
        logger.debug("telegram bridge: text notify failed", exc_info=True)


def notify_sudo(sid: str) -> None:
    notify_text(
        sid,
        "🔒 A dashboard session is waiting for your sudo password — "
        "open it in Odyssey/Desktop to answer.",
    )


def notify_secret(sid: str, env_var: str, prompt: str) -> None:
    notify_text(
        sid,
        f"🔑 A dashboard session is waiting for a secret ({env_var or 'unnamed'}) — "
        "open it in Odyssey/Desktop to answer. Secrets are never entered via Telegram.",
    )


# ── Relay auth: per-boot token published for the gateway's telegram adapter ──

# sk_hash → session_key registry, populated on every mirrored approval card.
# Bounded: prompts are rare, but a long-lived dashboard shouldn't grow it
# forever. The relay endpoint resolves ``dsh:ap:<hash>:<choice>`` callbacks
# through here without reaching into the server's session table.
_sk_hash_registry: dict[str, str] = {}
_SK_HASH_REGISTRY_MAX = 256
_sk_hash_lock = threading.Lock()


def _remember_sk_hash(session_key: str) -> str:
    sk_hash = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:8]
    with _sk_hash_lock:
        if len(_sk_hash_registry) >= _SK_HASH_REGISTRY_MAX:
            # Drop the oldest quarter (dict preserves insertion order).
            for k in list(_sk_hash_registry)[: _SK_HASH_REGISTRY_MAX // 4]:
                _sk_hash_registry.pop(k, None)
        _sk_hash_registry[sk_hash] = session_key
    return sk_hash


def session_key_for_hash(sk_hash: str) -> Optional[str]:
    with _sk_hash_lock:
        return _sk_hash_registry.get(sk_hash)


def parse_relay_callback(data: str) -> Optional[dict]:
    """Parse a ``dsh:*`` callback payload into a relay instruction.

    Returns ``{"kind": "approval", "session_key": …, "choice": …}`` or
    ``{"kind": "clarify", "request_id": …, "answer_idx": …}``, or None when
    the payload doesn't parse. Shared contract with the telegram adapter —
    the adapter forwards raw callback_data and the dashboard's relay
    endpoint parses it here, so both sides evolve together.
    """
    if not isinstance(data, str) or not data.startswith("dsh:"):
        return None
    parts = data.split(":")
    try:
        if len(parts) == 4 and parts[1] == "ap":
            return {
                "kind": "approval",
                "sk_hash": parts[2],
                "choice": parts[3],
            }
        if len(parts) == 4 and parts[1] == "cl":
            return {
                "kind": "clarify",
                "request_id": parts[2],
                "answer_idx": int(parts[3]),
            }
        if len(parts) == 5 and parts[1] == "cl":
            return {
                "kind": "clarify",
                "request_id": parts[2],
                "question_id": parts[3],
                "answer_idx": int(parts[4]),
            }
    except (ValueError, IndexError):
        return None
    return None



def get_bridge_token() -> str:
    """Per-boot random token the telegram adapter presents on the relay route.

    Generated lazily, held in memory only — the relay endpoint verifies with
    ``hmac.compare_digest`` and the value never enters a log line.
    """
    global _bridge_token
    with _bridge_token_lock:
        if _bridge_token is None:
            _bridge_token = secrets.token_urlsafe(32)
        return _bridge_token


def publish_bridge_info(port: int) -> None:
    """Write ``<HERMES_HOME>/runtime/telegram-bridge.json`` (port + token).

    Called when the dashboard's HTTP server boots — same lifecycle as the
    desktop-backend port file. Atomic replace, 0600, profile-aware home.
    """
    from hermes_constants import get_hermes_home

    target = get_hermes_home() / "runtime" / BRIDGE_INFO_FILENAME
    payload = {
        "port": int(port),
        "pid": os.getpid(),
        "token": get_bridge_token(),
        "endpoint": RELAY_ENDPOINT,
    }
    try:
        import tempfile

        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f"{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(json.dumps(payload, separators=(",", ":")))
        os.chmod(fh.name, 0o600)
        tmp = fh.name
        os.replace(tmp, target)
    except Exception:
        logger.debug("telegram bridge: failed to publish bridge info", exc_info=True)


def read_bridge_info() -> Optional[dict]:
    """Read the bridge endpoint info (used by tests and the relay-side helper)."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "runtime" / BRIDGE_INFO_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("port") and raw.get("token"):
            return raw
    except Exception:
        pass
    return None


def verify_relay_token(presented: Optional[str]) -> bool:
    """Constant-time check of the token the gateway relay presented."""
    if not presented:
        return False
    expected = get_bridge_token()
    import hmac as _hmac

    return _hmac.compare_digest(presented.encode(), expected.encode())
