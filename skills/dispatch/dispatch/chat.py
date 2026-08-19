"""Chat transport.

Thin, deliberately: long-poll for updates, send text back. The daemon owns this
socket exclusively -- two consumers polling the same bot get 409s from the API,
which is why setup refuses to run while the in-session chat plugin is enabled.
"""
import json
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot%s/%s"


class Chat:
    def __init__(self, token, allowlist=None, opener=None, timeout=30):
        self._token = token
        self.allowlist = [str(c) for c in (allowlist or [])]
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _call(self, method, params):
        data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(API % (self._token, method), data=data)
        with self._opener(request, timeout=self.timeout + 10) as response:
            return json.loads(response.read().decode())

    def allowed(self, chat_id):
        return not self.allowlist or str(chat_id) in self.allowlist

    def poll(self, offset):
        """Return ``(messages, next_offset)``. Never raises on a bad response."""
        try:
            payload = self._call("getUpdates", {
                "offset": offset, "timeout": self.timeout,
                "allowed_updates": json.dumps(["message"])})
        except Exception:  # noqa: BLE001 - a dropped poll is retried next tick
            return [], offset
        messages = []
        next_offset = offset
        for update in payload.get("result") or []:
            next_offset = max(next_offset, update.get("update_id", 0) + 1)
            message = update.get("message") or {}
            text = message.get("text")
            chat_id = (message.get("chat") or {}).get("id")
            if text is None or chat_id is None:
                continue
            if not self.allowed(chat_id):
                continue
            messages.append({"chat_id": str(chat_id), "text": text,
                             "message_id": message.get("message_id")})
        return messages, next_offset

    def send(self, chat_id, text):
        try:
            self._call("sendMessage", {"chat_id": chat_id, "text": text})
            return True
        except Exception:  # noqa: BLE001 - notification loss must not stop work
            return False


class NullChat:
    """Stand-in when no token is configured. Records instead of sending."""

    def __init__(self):
        self.sent = []

    def allowed(self, chat_id):
        return True

    def poll(self, offset):
        return [], offset

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True
