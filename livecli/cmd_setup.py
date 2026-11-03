"""Installation and reliability: setup, doctor, wait, config, completion, skill."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import cli as C
from . import probes as P
from .client import CONFIG_DIR, CONFIG_FILE, LiveError, call, config, port

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT_SRC = os.path.join(REPO, "remote_script", "LiveCLI")


def user_library():
    """Live's User Library: from Library.cfg of the newest Live preferences if set, else the default."""
    default = os.path.expanduser("~/Music/Ableton/User Library")
    prefs = sorted(glob.glob(os.path.expanduser("~/Library/Preferences/Ableton/Live */Library.cfg")))
    for p in reversed(prefs):
        try:
            txt = open(p, "rb").read().decode("utf-8", "ignore")
        except OSError:
            continue
        m = re.search(r'<UserLibrary>.*?<Data\s+Value="([^"]+)"', txt, re.S) or re.search(r'UserLibrary[^>]*Path[^>]*Value="([^"]+)"', txt)
        if m and os.path.isdir(m.group(1)):
            return m.group(1)
    return default


def script_version(path):
    try:
        m = re.search(r"^VERSION\s*=\s*(\d+)", open(os.path.join(path, "commands.py")).read(), re.M)
        return int(m.group(1)) if m else None
    except OSError:
        return None


@C.command("setup", "setup [--user-library PATH] [--dry-run]", "install (or update) the Remote Script into Live's User "
           "Library and tell what to choose in Live's settings", "other")
def c_setup(a):
    lib = a[a.index("--user-library") + 1] if "--user-library" in a else user_library()
    dest = os.path.join(lib, "Remote Scripts", "LiveCLI")
    if not os.path.isdir(SCRIPT_SRC):
        raise LiveError("Remote Script source not found in %s" % SCRIPT_SRC)
    before = script_version(dest)
    plan = {"from": SCRIPT_SRC, "to": dest, "installed_version": before, "new_version": script_version(SCRIPT_SRC)}
    if "--dry-run" in a:
        return C.out(plan, "would copy %s -> %s (installed v%s, new v%s)" % (SCRIPT_SRC, dest, before, plan["new_version"]))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(SCRIPT_SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    running = False
    try:
        r = call("ping", timeout=2)
        running = True
        if r.get("version") != plan["new_version"]:
            call("reload")       # commands.py is reloadable; __init__.py changes need a Live restart
    except LiveError:
        pass
    plan["live_running"] = running
    C.out(plan, "Remote Script v%s installed in %s\n"
          "Next, in Live: Settings > Link, Tempo & MIDI > a free Control Surface slot: LiveCLI (Input and Output: None).\n"
          "Live loads Remote Scripts at startup: restart Live if LiveCLI is not in the list. Then: live doctor"
          % (plan["new_version"], dest))


def _check(rows, ok, what, detail="", hint=""):
    rows.append({"ok": bool(ok), "check": what, "detail": detail, "hint": "" if ok else hint})
    return ok


@C.command("doctor", "doctor", "check everything: Python, Remote Script, Live, port, version, config, probes, ffmpeg", "other")
def c_doctor(a):
    rows = []
    _check(rows, sys.version_info >= (3, 9), "Python", sys.version.split()[0], "Python 3.9 or newer is needed")
    dest = os.path.join(user_library(), "Remote Scripts", "LiveCLI")
    inst, src = script_version(dest), script_version(SCRIPT_SRC)
    _check(rows, inst is not None, "Remote Script installed", dest if inst else "missing", "run 'live setup'")
    if inst is not None:
        _check(rows, inst == src, "Remote Script up to date", "installed v%s, CLI v%s" % (inst, src), "run 'live setup'")
    live_proc = subprocess.run(["pgrep", "-x", "Live"], capture_output=True).returncode == 0
    _check(rows, live_proc, "Live running", "", "start Ableton Live")
    answering = None
    try:
        answering = call("ping", timeout=2)
    except LiveError as e:
        _check(rows, False, "LiveCLI answering on port %d" % port(), str(e),
               "choose LiveCLI as Control Surface in Live's settings (restart Live after 'live setup')")
    if answering:
        _check(rows, True, "LiveCLI answering on port %d" % port(), "script v%s" % answering.get("version"))
        _check(rows, answering.get("version") == src, "running script matches the CLI",
               "running v%s, CLI v%s" % (answering.get("version"), src), "run 'live setup' (or 'live reload')")
        try:
            s_ = call("song")
            _check(rows, True, "Live version", s_.get("live", "?"))
        except LiveError:
            pass
    try:
        config()
        _check(rows, True, "config", CONFIG_FILE if os.path.exists(CONFIG_FILE) else "none (defaults)")
    except LiveError as e:
        _check(rows, False, "config", str(e), "fix the JSON in %s" % CONFIG_FILE)
    srvs = P.servers()
    _check(rows, True, "probe servers", ", ".join("%s %s" % (s.info.get("product"), s.info.get("version")) for s in srvs) or
           "none (load a probe plugin to use measurements)")
    ff = shutil.which("ffmpeg") or (os.path.exists("/opt/homebrew/bin/ffmpeg") and "/opt/homebrew/bin/ffmpeg")
    _check(rows, True, "ffmpeg (for grid, capture)", ff or "not found: 'brew install ffmpeg' to use grid and capture")
    if C.JSON:
        return C.out({"ok": all(r["ok"] for r in rows), "checks": rows})
    for r in rows:
        print("  %s %-34s %s" % ("✓" if r["ok"] else "✗", r["check"], r["detail"]))
        if r["hint"]:
            print("      → %s" % r["hint"])


@C.command("wait", "wait [seconds]", "wait until Live answers (default 60 s); exit code 3 if it doesn't", "other")
def c_wait(a):
    sec = float(a[0]) if a else 60.0
    t_end = time.time() + sec
    while True:
        try:
            r = call("ping", timeout=2)
            return C.out(r, "Live is ready (script v%s)" % r.get("version"))
        except LiveError:
            if time.time() >= t_end:
                raise LiveError("Live did not answer within %g s" % sec, code="live_not_running")
            time.sleep(0.5)


@C.command("config", "config [show|set <key> <json value>|path]", "show or change ~/.config/live-cli/config.json "
           "(keys with dots: capture.enabled)", "other")
def c_config(a):
    op = a[0] if a else "show"
    if op == "path":
        return print(CONFIG_FILE)
    cfg = config()
    if op == "show":
        return print(json.dumps(cfg, indent=1, ensure_ascii=False))
    if op == "set":
        C.need(a, 3, "config set <key> <json value>")
        try:
            val = json.loads(" ".join(a[2:]))
        except ValueError:
            val = " ".join(a[2:])
        node = cfg
        keys = a[1].split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = val
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as fh:
            json.dump(cfg, fh, indent=1, ensure_ascii=False)
        return C.out(cfg, "%s = %s" % (a[1], json.dumps(val)))
    raise LiveError("usage: live config show|set <key> <value>|path")


@C.command("completion", "completion zsh|bash", "print a shell completion script (eval it in your shell rc)", "other")
def c_completion(a):
    shell = a[0] if a else "zsh"
    names = " ".join(sorted(set(C.COMMANDS) | set(C.ALIASES)))
    if shell == "bash":
        print('_live() { COMPREPLY=($(compgen -W "%s" -- "${COMP_WORDS[COMP_CWORD]}")); }\ncomplete -F _live live' % names)
    elif shell == "zsh":
        print("#compdef live\n_live() { local -a cmds; cmds=(%s); _arguments '1:command:(${cmds})' '*::arg:_files' }\n"
              "compdef _live live" % names)
    else:
        raise LiveError("completion: zsh or bash")


def skill_text():
    groups = []
    for g, title in C.GROUPS:
        items = [(n, c) for n, c in C.COMMANDS.items() if c[3] == g]
        if items:
            groups.append("### %s\n" % title + "\n".join("- `live %s`: %s" % (c[1], c[2]) for n, c in items))
    gaps = os.path.join(REPO, "docs", "API-GAPS.md")
    gaps_note = "See docs/API-GAPS.md in the LiveCLI repo for what Live's API cannot do." if os.path.exists(gaps) else ""
    return """---
name: livecli
description: Control and measure Ableton Live from the command line with the `live` command (LiveCLI). Use it whenever the user asks to inspect or change their Live set (tracks, devices, plugin parameters, mixer, routing, clips, notes, automation, scenes, locators), to measure audio (levels, LUFS, frequency bands, phase, latency, timing against the grid) with probes, to find which of their installed plugins fit a job, or to bounce audio inside Live.
---

# LiveCLI

`live` talks to Ableton Live through the LiveCLI Remote Script (local, no MCP). Every command accepts `--json`.
Start with `live doctor` if anything fails; `live wait` waits for Live.

## Rules
- Reading is free. Ask the user before changing their set. `live snapshot` before a series of changes, `live diff` and `live restore` after: a safety net more precise than undo.
- References: track `t3`, `r0`, `m` (master), a return letter or part of a name; device: index, rack path `2.0.1` or part of the name; probe: `#<id>`, label or track name; clip: slot number or `@N` (arrangement).
- `live meters` is Live's 0-1 meter scale, not dBFS: for real levels use probes (`probe-add`, `measure`).
- Plugins expose to Live only the parameters they publish; `set` on AU/VST works by display value with read-back.
- Errors: JSON `{"ok":false,"error","code","hint"}`; exit codes 3 Live not running, 4 not found/ambiguous, 5 not supported by Live's API, 6 probe.
%s

## Commands
%s
""" % (gaps_note, "\n\n".join(groups))


@C.command("skill", "skill print|install [--dir DIR]", "agent skill (SKILL.md) generated from these commands; install "
           "writes it to ~/.claude/skills/livecli (Claude Code) or --dir", "other")
def c_skill(a):
    op = a[0] if a else "print"
    text = skill_text()
    if op == "print":
        return print(text)
    if op == "install":
        d = a[a.index("--dir") + 1] if "--dir" in a else os.path.expanduser("~/.claude/skills/livecli")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w") as fh:
            fh.write(text)
        return C.out({"installed": os.path.join(d, "SKILL.md")}, "skill written to %s" % os.path.join(d, "SKILL.md"))
    raise LiveError("usage: live skill print|install [--dir DIR]")
