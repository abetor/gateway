"""Concrete Telegram implementation of the channel contract.

Credentials come only from environment variables, optionally merged from
``<home>/.env`` by ``core.config.load_env``. Configuration stores variable
names, not values.

The channel must enforce a sender allow-list. Telegram uses the configured chat
ID; without it, startup fails closed. The security model assumes this is a
private operator chat. A group chat ID would allow every member of that group
because there is no per-user filter. See the security section in DESIGN.md.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, List, Optional

import os

from ...core.channel import Channel, Inbound, register

MSG_CAP = 3500          # Telegram allows 4096; reserve margin for transport behavior.
# Truncation must be explicit. The complete text remains in the delivery log.
CUT_NOTE = "\n…(truncated; full text is in <home>/log/delivery.jsonl)"

# Telegram warns that error codes may change. Treat only an explicit allow-list
# of invalid credentials, access, and method failures as permanent. Unknown
# codes remain transient because an extra retry is cheaper than a lost notice.
FATAL_ERROR_CODES = frozenset({401, 403, 404})


def _permanent(resp: dict) -> bool:
    """Return whether a response is permanent under ``FATAL_ERROR_CODES``."""
    code = (resp or {}).get("error_code")
    return isinstance(code, int) and code in FATAL_ERROR_CODES


def _diagnostic(resp: dict, fallback: str) -> str:
    """Return a readable reason, including Telegram's 429 retry hint."""
    reason = str((resp or {}).get("description") or fallback)
    parameters = (resp or {}).get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if retry_after is not None:
        reason += f" (retry_after={retry_after}s)"
    return reason


class TelegramChannel(Channel):
    """Telegram Bot API long-polling channel.

    Credential environment names are configurable and default to
    ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``. The chat ID is also the
    allow-list: foreign chats are ignored without a reply. ``http`` is
    injectable for offline tests, and ``state_path`` persists the update offset
    across restarts. The bot needs a dedicated token because a second
    ``getUpdates`` consumer can steal updates.
    """
    name = "telegram"

    def __init__(self, *, env: Optional[dict] = None, state_path: str = "",
                 http: Optional[Callable] = None, poll_timeout: int = 20,
                 token_env: str = "TELEGRAM_BOT_TOKEN", chat_id_env: str = "TELEGRAM_CHAT_ID"):
        src = os.environ if env is None else env
        self.token = src.get(token_env, "")
        self.chat_id = str(src.get(chat_id_env, "") or "")
        if not self.token or not self.chat_id:
            raise ValueError(f"telegram: env {token_env} and {chat_id_env} are required "
                             "because chat_id is also the sender allow-list")
        self.state_path = state_path
        self.poll_timeout = poll_timeout
        self._http = http or self._urllib_http
        self.offset = self._load_offset()

    # Injectable HTTP transport.
    def _urllib_http(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.poll_timeout + 15) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            # Telegram returns structured JSON for 4xx errors. It is more useful
            # than a generic HTTP message and distinguishes expired credentials
            # from retryable network failures.
            try:
                body = json.loads(e.read().decode() or "{}")
            except (OSError, ValueError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            body["ok"] = False
            body.setdefault("error_code", e.code)
            body.setdefault("description", f"HTTP {e.code}")
            return body
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "description": f"{type(e).__name__}: {e}"}

    # Persisted update offset survives restarts.
    def _load_offset(self) -> int:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return int(json.load(f).get("offset", 0))
        except (OSError, ValueError):
            return 0

    def _save_offset(self) -> None:
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"offset": self.offset}, f)
        except OSError:
            pass                                   # Best effort: loss causes replay, not a crash.

    def poll(self) -> List[Inbound]:
        resp = self._http("getUpdates", {"offset": self.offset + 1, "timeout": self.poll_timeout,
                                         "allowed_updates": ["message"]})
        if not (isinstance(resp, dict) and resp.get("ok")):
            # An expired token or second getUpdates consumer must stay visible;
            # silence would make a permanently dead channel appear healthy.
            self.last_error = str((resp or {}).get("description") or "getUpdates failed")
        out: List[Inbound] = []
        for u in (resp.get("result") or []) if isinstance(resp, dict) else []:
            self.offset = max(self.offset, int(u.get("update_id", 0)))
            msg = u.get("message") or {}
            chat = str(((msg.get("chat") or {}).get("id")) or "")
            text = (msg.get("text") or "").strip()
            if chat != self.chat_id or not text:   # Silently ignore foreign chats and non-text.
                continue
            reply_src = ((msg.get("reply_to_message") or {}).get("text")) or ""
            out.append(Inbound(text=text, reply_to_text=reply_src, sender=chat, raw=u))
        self._save_offset()
        return out

    def _message_result(self, method: str, payload: dict) -> bool:
        resp = self._http(method, payload)
        ok = bool(isinstance(resp, dict) and resp.get("ok"))
        self.last_permanent = False
        self.last_error = ""
        self.last_error_code = None
        if ok:
            result = resp.get("result") if isinstance(resp, dict) else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            self.last_message_id = message_id if isinstance(message_id, int) else None
            return True
        self.last_message_id = None
        self.last_error = _diagnostic(resp if isinstance(resp, dict) else {},
                                      f"{method} failed")
        code = resp.get("error_code") if isinstance(resp, dict) else None
        self.last_error_code = code if isinstance(code, int) else None
        self.last_permanent = _permanent(resp if isinstance(resp, dict) else {})
        return False

    def send(self, text: str, *, parse_mode: str = "") -> bool:
        body = text if len(text) <= MSG_CAP else text[:MSG_CAP - len(CUT_NOTE)] + CUT_NOTE
        payload = {"chat_id": self.chat_id, "text": body}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._message_result("sendMessage", payload)

    def edit(self, message_id: int, text: str, *, parse_mode: str = "") -> bool:
        body = text if len(text) <= MSG_CAP else text[:MSG_CAP - len(CUT_NOTE)] + CUT_NOTE
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": body}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._message_result("editMessageText", payload)

    def check(self) -> str:
        resp = self._http("getMe", {})
        if isinstance(resp, dict) and resp.get("ok"):
            return ""
        return f"telegram getMe: {(resp or {}).get('description') or 'failed'}"


register("telegram", TelegramChannel)
