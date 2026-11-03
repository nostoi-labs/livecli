"""Connection to the LiveCLI Remote Script inside Ableton Live, and config."""
import json
import os
import socket

CONFIG_DIR = os.path.expanduser(os.environ.get("LIVECLI_CONFIG_DIR", "~/.config/live-cli"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


class LiveError(Exception):
    pass


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
        raise LiveError("Live is not answering on port %d: is Live running with the LiveCLI control surface "
                        "enabled (Settings > Link, Tempo & MIDI > Control Surface)?" % port())
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
        raise LiveError("Live is running the old ClaudeLive script: in Live choose LiveCLI as Control Surface "
                        "(Settings > Link, Tempo & MIDI)")
    if not r.get("ok"):
        raise LiveError(r.get("error") or r.get("errore") or "error from Live")
    return r.get("result", r.get("risultato"))
