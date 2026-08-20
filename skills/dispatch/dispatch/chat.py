"""Chat transport.

Thin, deliberately: long-poll for updates, send text back. The daemon owns this
socket exclusively -- two consumers polling the same bot get 409s from the API,
which is why setup disables the in-session chat plugin.

A dropped poll is still not an error worth stopping for -- the next tick retries
it -- but it stopped being invisible. Telegram is the only interface after the
cutover, and its total failure looks exactly like silence: the daemon is up, the
queue is healthy, the watchdog is quiet, and the bot answers nothing. So every
failure is counted and its message kept, for the surfaces that are still
reachable when this one is not.

``last_error`` is redacted before it is exposed: it ends up in ``state.json``,
and the bot token is never copied there.

The allowlist is the authentication boundary, and it fails closed: an empty one
admits nobody, and a live transport cannot be built without one at all.
"""
import json
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot%s/%s"


class Chat:
    def __init__(self, token, allowlist=None, opener=None, timeout=30):
        self._token = token
        # A bare string is a list of characters to `for`, and "7" is a chat id
        # somebody holds. One id written without brackets means that one id.
        if isinstance(allowlist, (str, bytes)):
            allowlist = [allowlist]
        self.allowlist = [str(c) for c in (allowlist or []) if str(c)]
        if not self.allowlist:
            # Refusing to exist is the second half of failing closed. `allowed`
            # denies an empty allowlist on its own; this makes a transport that
            # has nobody to admit unconstructable, so a later widening of
            # `allowed` cannot quietly reopen the door.
            raise ValueError(
                "chat_allowlist is empty: a live transport with no allowlist "
                "would admit every chat. Set one with "
                "`dispatch setup --chat <chat-id>`.")
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        # Health, for the surfaces that report on this one.
        self.last_error = None
        self.failures = 0

    def _redact(self, text):
        """The API URL carries the token; an error message may quote it."""
        return text.replace(self._token, "<token>") if self._token else text

    def _failed(self, exc):
        self.failures += 1
        self.last_error = self._redact("%s: %s" % (type(exc).__name__, exc))

    def _worked(self):
        self.failures = 0
        self.last_error = None

    def _call(self, method, params):
        data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(API % (self._token, method), data=data)
        with self._opener(request, timeout=self.timeout + 10) as response:
            return json.loads(response.read().decode())

    def allowed(self, chat_id):
        """Deny by default: an empty allowlist means nobody, not everybody.

        This is the whole authentication boundary of the system. It used to
        read ``not self.allowlist or ...``, which turned a missing or corrupt
        config.json -- whose defaults carry an empty allowlist -- into a bot
        that answered every stranger, with no symptom to notice.
        """
        return bool(self.allowlist) and str(chat_id) in self.allowlist

    def poll(self, offset):
        """Return ``(messages, next_offset)``. Never raises on a bad response."""
        try:
            payload = self._call("getUpdates", {
                "offset": offset, "timeout": self.timeout,
                "allowed_updates": json.dumps(["message"])})
        except Exception as exc:  # noqa: BLE001 - retried next tick, but counted
            self._failed(exc)
            return [], offset
        self._worked()
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
        except Exception as exc:  # noqa: BLE001 - notification loss must not stop work
            self._failed(exc)
            return False
        self._worked()
        return True


class NullChat:
    """Stand-in when no token is configured. Records instead of sending.

    ``reason`` is why there is no real transport. It is a standing condition
    rather than a failure count: nothing the daemon does will fix it, so it is
    reported once and does not tick upwards.
    """

    def __init__(self, reason=None):
        self.sent = []
        self.last_error = reason
        self.failures = 0

    def allowed(self, chat_id):
        """Safe as True: this transport polls nothing, so nothing is admitted
        through it, and `send` only appends to a list."""
        return True

    def poll(self, offset):
        return [], offset

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True
