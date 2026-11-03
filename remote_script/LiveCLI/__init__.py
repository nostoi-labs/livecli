# LiveCLI Remote Script for Ableton Live: a local JSON API on 127.0.0.1:9878.
#
# One request per connection: the client sends one JSON line {"cmd": ..., ...} and gets one JSON line back:
# {"ok": true, "result": ...} or {"ok": false, "error": "..."}.
# The Live Object Model is touched only from Live's main thread (update_display).
# Commands live in commands.py; the "reload" command re-imports it without restarting Live.
from __future__ import absolute_import, print_function, unicode_literals

import importlib
import json
import os
import queue
import socket
import threading
import traceback

from _Framework.ControlSurface import ControlSurface

from . import commands

HOST = "127.0.0.1"


def _port():
    if os.environ.get("LIVECLI_PORT"):
        return int(os.environ["LIVECLI_PORT"])
    try:
        with open(os.path.expanduser("~/.config/live-cli/config.json")) as fh:
            return int(json.load(fh).get("port", 9878))
    except Exception:
        return 9878


PORT = _port()


def create_instance(c_instance):
    return LiveCLI(c_instance)


class LiveCLI(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._song = self.song()
        self._jobs = queue.Queue()
        self._later = []          # (ticks_left, fn): work scheduled for a later tick
        self._running = True
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind((HOST, PORT))
            self._srv.listen(8)
            self._srv.settimeout(1.0)
            threading.Thread(target=self._accept, daemon=True).start()
            self.log_message("LiveCLI: listening on %s:%d" % (HOST, PORT))
        except Exception as e:
            self.log_message("LiveCLI: cannot start server: %s" % e)

    def disconnect(self):
        self._running = False
        try:
            self._srv.close()
        except Exception:
            pass
        ControlSurface.disconnect(self)

    def _accept(self):
        while self._running:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        try:
            c.settimeout(30)
            data = b""
            while not data.endswith(b"\n"):
                chunk = c.recv(65536)
                if not chunk:
                    break
                data += chunk
            req = json.loads(data.decode("utf-8") or "{}")
            box = queue.Queue()
            self._jobs.put((req, box))
            try:
                res = box.get(timeout=float(req.get("timeout", 10)))
            except queue.Empty:
                res = {"ok": False, "error": "timeout: Live did not answer"}
            c.sendall((json.dumps(res) + "\n").encode("utf-8"))
        except Exception as e:
            try:
                c.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
            except Exception:
                pass
        finally:
            c.close()

    def livecli_after(self, ticks, fn):
        """Run fn() on the main thread after `ticks` display ticks (~100 ms each)."""
        self._later.append([ticks, fn])

    def update_display(self):
        ControlSurface.update_display(self)
        for item in list(self._later):
            item[0] -= 1
            if item[0] <= 0:
                self._later.remove(item)
                try:
                    item[1]()
                except Exception:
                    self.log_message("LiveCLI: " + traceback.format_exc())
        while not self._jobs.empty():
            req, box = self._jobs.get_nowait()
            try:
                if req.get("cmd") == "reload":
                    importlib.reload(commands)
                    box.put({"ok": True, "result": {"reloaded": True, "version": commands.VERSION}})
                else:
                    res = commands.run(self, req, box)
                    if res is not commands.DEFERRED:
                        box.put({"ok": True, "result": res})
            except Exception as e:
                self.log_message("LiveCLI: " + traceback.format_exc())
                box.put({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
