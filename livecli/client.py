"""Connection to the LiveCLI Remote Script inside Ableton Live, and config."""
import json
import os
import socket

CONFIG_DIR = os.path.expanduser(os.environ.get("LIVECLI_CONFIG_DIR", "~/.config/live-cli"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


EXIT_CODES = {"error": 1, "usage": 2, "live_not_running": 3, "not_found": 4, "ambiguous": 4,
              "not_supported_by_live_api": 5, "probe": 6, "bad_request": 7}


class LiveError(Exception):
    """An error with a stable code (see EXIT_CODES) and an optional hint for the user or the agent."""

    def __init__(self, message, code=None, hint=None):
        Exception.__init__(self, message)
        self.code = code or classify(message)
        self.hint = hint


def classify(message):
    m = str(message).lower()
    if "not answering" in m or "old claudelive" in m:
        return "live_not_running"
    if "ambiguous" in m or "matches several" in m:
        return "ambiguous"
    if "not found" in m or "no device" in m or "no clip" in m or "bad track reference" in m or "no locator" in m:
        return "not_found"
    if "has no setter" in m or "attributeerror" in m or "not available in live" in m or "unsupported" in m or "not supported" in m:
        return "not_supported_by_live_api"
    if "probe" in m:
        return "probe"
    if m.startswith("usage:"):
        return "usage"
    return "error"


def config():
    try:
        with open(CONFIG_FILE) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise LiveError("bad config file %s: %s" % (CONFIG_FILE, e))


def port():
    return int(os.environ.get("LIVECLI_PORT") or config().get("port") or 9878)


def call(cmd, timeout=10, **kw):
    """Send one command to Live and return its result. Raises LiveError."""
    kw["cmd"] = cmd
    kw.setdefault("timeout", timeout)
    try:
        s = socket.create_connection(("127.0.0.1", port()), timeout=timeout + 5)
    except OSError:
        raise LiveError("Live is not answering on port %d" % port(), code="live_not_running",
                        hint="start Live; choose LiveCLI as Control Surface in Settings > Link, Tempo & MIDI "
                             "('live setup' installs the script, 'live doctor' checks everything)")
    try:
        s.sendall((json.dumps(kw) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            c = s.recv(65536)
            if not c:
                break
            data += c
    finally:
        s.close()
    if not data:
        raise LiveError("empty answer from Live")
    r = json.loads(data.decode())
    if "risultato" in r or "errore" in r:
        raise LiveError("Live is running the old ClaudeLive script", code="live_not_running",
                        hint="in Live choose LiveCLI as Control Surface (Settings > Link, Tempo & MIDI)")
    if not r.get("ok"):
        raise LiveError(r.get("error") or r.get("errore") or "error from Live")
    return r.get("result", r.get("risultato"))
