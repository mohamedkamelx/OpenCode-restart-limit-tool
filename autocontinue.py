#!/usr/bin/env python3
"""autocontinue.py - resume an OpenCode session after it stops on a usage limit.

The watcher binds to a session and polls it. When the session goes from
running to stopped AND the last assistant turn failed with a limit/quota
error ("usage limit", "quota exceeded", 429, ...), it posts a prompt back
into that same session and goes back to waiting for the next limit stop.

A stop that is NOT a limit stop is ignored, so normal end-of-turn idling
does not spam "continue".

Usage
    python3 autocontinue.py --status
    python3 autocontinue.py                      # watch the running session
    python3 autocontinue.py --all                # watch every session that runs
    python3 autocontinue.py --session ses_xxx --max 3
    python3 autocontinue.py --dry-run --for 60
    python3 autocontinue.py --detach             # background + log file

Stop with Ctrl-C, SIGTERM, or the --stop-file path.

Retry policy: the first continue after a limit stop is sent immediately. If
that turn also ends on a limit, the next attempt is delayed by --retry-wait
and the delay doubles up to --max-retry-wait. A stop without a limit error
resets the backoff.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from loadenv import load_env

load_env()

DEFAULT_PROMPT = "continue"

# Mirrors the classification OpenCode itself uses for provider errors,
# plus the usual phrasings.
LIMIT_RE = re.compile(
    r"insufficient[-_\s]?quota"
    r"|quota[-_\s]?exceeded"
    r"|budget exceeded"
    r"|usage limit"
    r"|limit (?:has been |was )?reached"
    r"|rate[-_\s]?limit"
    r"|too[_\s]?many[_\s]?requests"
    r"|out of credits|credits? (?:exhausted|exceeded)"
    r"|exceeded your (?:plan |usage )?limit|reached your (?:usage )?limit"
    r"|payment required|\b402\b|\b429\b",
    re.I,
)


# --------------------------------------------------------------------------- #
# server discovery + auth
# --------------------------------------------------------------------------- #
def discover_url() -> str:
    for var in ("OPENCODE_SERVER_URL", "OPENCODE_URL"):
        if os.environ.get(var):
            return os.environ[var].rstrip("/")
    try:
        out = subprocess.run(["opencode", "service", "status"],
                             capture_output=True, text=True, timeout=20)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                return line.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:4096"


def load_password() -> str | None:
    path = Path(os.environ.get("OPENCODE_SERVICE_JSON",
                               "~/.config/opencode/service.json")).expanduser()
    try:
        return json.loads(path.read_text()).get("password")
    except Exception:
        return None


class Client:
    def __init__(self, url: str, password: str | None):
        self.url = url
        self.auth = None
        if password:
            token = base64.b64encode(f"opencode:{password}".encode()).decode()
            self.auth = f"Basic {token}"

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json"}
        if self.auth:
            h["Authorization"] = self.auth
        if extra:
            h.update(extra)
        return h

    def get(self, path: str) -> dict:
        req = urllib.request.Request(self.url + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(),
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}

    def active(self) -> dict:
        return self.get("/api/session/active").get("data", {}) or {}

    def sessions(self) -> list:
        data = self.get("/api/session").get("data", [])
        return data if isinstance(data, list) else []

    def last_assistant(self, session_id: str) -> dict | None:
        try:
            d = self.get(f"/api/session/{session_id}/message"
                         "?limit=5&order=desc&type=assistant")
        except Exception:
            return None
        for m in d.get("data", []) or []:
            if m.get("type") == "assistant":
                return m
        return None

    def prompt(self, session_id: str, text: str, delivery: str | None) -> dict:
        body: dict = {"text": text}
        if delivery:
            body["delivery"] = delivery
        return self.post(f"/api/session/{session_id}/prompt", body)


def resolve_session(c: Client, explicit: str | None) -> str:
    explicit = explicit or os.environ.get("OPENCODE_SESSION_ID")
    if explicit:
        return explicit
    active = c.active()
    if len(active) == 1:
        return next(iter(active))
    if not active:
        raise SystemExit("no running session found; pass --session ses_... or --all")
    updated = {s.get("id"): (s.get("time") or {}).get("updated", 0) for s in c.sessions()}
    return max(active, key=lambda sid: updated.get(sid, 0))


# --------------------------------------------------------------------------- #
# limit detection
# --------------------------------------------------------------------------- #
def limit_reason(msg: dict | None, scan_text: bool = False) -> str | None:
    """Return the matched limit phrase if this turn ended on a usage limit.

    By default only failed turns (error / retry / finish=error) are examined,
    so ordinary assistant prose that merely mentions limits never triggers.
    Pass scan_text=True to also inspect normal turns (riskier).
    """
    if not msg:
        return None
    err = msg.get("error")
    retry = msg.get("retry")
    finish = msg.get("finish")
    failed = bool(err or retry or finish == "error")
    if not failed and not scan_text:
        return None
    blob = json.dumps({
        "error": err,
        "retry": retry,
        "finish": finish,
        "rawFinish": msg.get("rawFinish"),
    }, ensure_ascii=False)
    m = LIMIT_RE.search(blob)
    if m:
        return m.group(0)
    if not failed and not scan_text:
        return None
    # last resort: a short provider message on the failed turn
    text = " ".join(
        str(part.get("text") or "") for part in (msg.get("content") or [])
        if part.get("type") == "text"
    )
    m = LIMIT_RE.search(text[:400])
    return m.group(0) if m else None


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
class Log:
    def __init__(self, path: str | None):
        self.fh = open(path, "a", buffering=1) if path else None

    def __call__(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if self.fh:
            self.fh.write(line + "\n")

    def close(self):
        if self.fh:
            self.fh.close()


# --------------------------------------------------------------------------- #
# watcher
# --------------------------------------------------------------------------- #
class Watcher:
    def __init__(self, args, client: Client, log: Log, targets: set[str]):
        self.args = args
        self.c = client
        self.log = log
        self.targets = set(targets)
        self.running: dict[str, bool] = {}
        self.due: dict[str, float] = {}       # sid -> when to send
        self.backoff: dict[str, float] = {}   # sid -> seconds
        self.sent = 0
        self.last_send: dict[str, float] = {}
        self.stop = False

    # -- helpers ---------------------------------------------------------- #
    def _restart_router(self):
        """Cycle the PPPoE link(s) so the next continue gets a fresh IP."""
        if not self.args.router:
            return
        try:
            from router_restart import restart as restart_router
        except Exception as exc:
            self.log(f"router: module unavailable ({exc})")
            return
        try:
            restart_router(
                self.args.router_wans, self.log,
                settle=self.args.router_settle,
                ip_wait=self.args.router_ip_wait,
                timeout=self.args.router_timeout,
                dry_run=self.args.dry_run,
                driver=self.args.router_driver,
                strategy=self.args.ip_strategy,
            )
        except Exception as exc:
            # never block the continue because of the router
            self.log(f"router: restart failed ({type(exc).__name__}: {exc})")

    def _send(self, sid: str):
        self.due.pop(sid, None)
        if self.args.max and self.sent >= self.args.max:
            self.log(f"reached --max {self.args.max}, stopping")
            self.stop = True
            return
        self._restart_router()
        if self.args.dry_run:
            self.log(f"{sid[-8:]} limit -> would send {self.args.prompt!r} (dry run)")
            self.sent += 1
            self.last_send[sid] = time.time()
            return
        try:
            self.c.prompt(sid, self.args.prompt, self.args.delivery)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            self.log(f"{sid[-8:]} prompt failed: HTTP {e.code} {body}")
            self._schedule(sid, retry=True)
            return
        except Exception as e:
            self.log(f"{sid[-8:]} prompt failed: {type(e).__name__}: {e}")
            self._schedule(sid, retry=True)
            return
        self.sent += 1
        self.last_send[sid] = time.time()
        self.log(f"{sid[-8:]} limit -> sent {self.args.prompt!r} (#{self.sent})")

    def _schedule(self, sid: str, retry: bool):
        now = time.time()
        if now - self.last_send.get(sid, 0) < self.args.min_interval:
            delay = self.args.min_interval
        elif retry:
            delay = self.backoff.get(sid, self.args.retry_wait)
            self.backoff[sid] = min(delay * 2, self.args.max_retry_wait)
        else:
            delay = 0.0
        self.due[sid] = now + delay
        if delay:
            self.log(f"{sid[-8:]} next attempt in {int(delay)}s")

    def _on_stop(self, sid: str):
        msg = self.c.last_assistant(sid)
        reason = limit_reason(msg, self.args.scan_text)
        if not reason:
            self.log(f"{sid[-8:]} stopped (no limit error) - waiting")
            self.backoff[sid] = self.args.retry_wait
            self.due.pop(sid, None)
            return
        self._schedule(sid, retry=bool(self.last_send.get(sid)))
        self.log(f"{sid[-8:]} stopped on limit ({reason!r}) -> continue queued")

    # -- main ------------------------------------------------------------- #
    def run(self) -> int:
        a = self.args
        self.log("watching " + (", ".join(sorted(self.targets)) or "(none)"))
        self.log(f"prompt on limit: {a.prompt!r} | max={a.max or 'unlimited'} "
                 f"| retry={a.retry_wait:.0f}s..{a.max_retry_wait:.0f}s "
                 f"| poll={a.poll}s{' | DRY RUN' if a.dry_run else ''}")
        if a.router:
            self.log("router restart before continue: "
                     f"driver={a.router_driver} strategy={a.ip_strategy} "
                     f"| WANs={', '.join(a.router_wans) or '(none)'} "
                     f"| settle={a.router_settle:.0f}s "
                     f"| ip-wait={a.router_ip_wait:.0f}s")
        else:
            self.log("router restart: off (--router enables it)")

        active = self.c.active()
        for sid in self.targets:
            self.running[sid] = sid in active
            if not self.running[sid]:
                # already stopped before we started watching: fire if it was a limit
                if limit_reason(self.c.last_assistant(sid), a.scan_text):
                    self._on_stop(sid)

        deadline = time.time() + a.for_seconds if a.for_seconds else None

        while not self.stop:
            if deadline and time.time() >= deadline:
                self.log("--for elapsed, exiting")
                break
            if a.stop_file and os.path.exists(a.stop_file):
                self.log(f"stop file found ({a.stop_file}), exiting")
                break

            try:
                active = self.c.active()
            except Exception as e:
                self.log(f"poll failed ({type(e).__name__}); retrying")
                time.sleep(a.poll)
                continue

            if a.all_sessions:
                for sid in active:
                    if sid not in self.targets:
                        self.targets.add(sid)
                        self.running[sid] = True
                        self.log(f"{sid[-8:]} new session detected")

            for sid in list(self.targets):
                running = sid in active
                was = self.running.get(sid)
                if was is None:
                    self.running[sid] = running
                    continue
                if was and not running:
                    self._on_stop(sid)
                self.running[sid] = running

            now = time.time()
            for sid, when in list(self.due.items()):
                if now >= when:
                    self._send(sid)

            if self.stop:
                break
            time.sleep(a.poll)

        self.log(f"stopped ({self.sent} prompt(s) sent)")
        return 0


def install_signals(w: Watcher):
    def handler(signum, frame):
        w.stop = True
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def status(args, client: Client, log: Log) -> int:
    log(f"server : {client.url}")
    try:
        info = client.get("/api/info")
        log(f"info   : opencode {info.get('version')} pid {info.get('pid')}")
    except urllib.error.HTTPError as e:
        log(f"info   : HTTP {e.code} (auth problem?)")
        return 1
    except urllib.error.URLError as e:
        log(f"info   : cannot reach {client.url} ({getattr(e, 'reason', e)})")
        log("         start OpenCode, or set OPENCODE_SERVER_URL")
        return 1

    active = client.active()
    if args.all_sessions:
        sids = sorted(active) or []
    else:
        try:
            sids = [resolve_session(client, args.session)]
        except SystemExit as e:
            log(f"session: {e}")
            return 1
    if not sids:
        log("session: none running (--all)")
        return 0

    for sid in sids:
        msg = client.last_assistant(sid)
        reason = limit_reason(msg, args.scan_text)
        state = "running" if sid in active else "stopped"
        detail = f"last turn failed on limit: {reason!r}" if reason else "last turn: no limit error"
        log(f"session: {sid}  state={state}  {detail}")
    log(f"prompt : {args.prompt!r}  retry={args.retry_wait:.0f}s  poll={args.poll}s")
    return 0


# --------------------------------------------------------------------------- #
# detach
# --------------------------------------------------------------------------- #
def detach(argv) -> int:
    log_path = "/tmp/opencode/autocontinue.log"
    for i, a in enumerate(argv):
        if a == "--log" and i + 1 < len(argv):
            log_path = argv[i + 1]
    parent = os.path.dirname(os.path.abspath(log_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # stdout/stderr go to log_path below, so pass --log is unnecessary and
    # would double-write every line
    clean, i = [], 0
    while i < len(argv):
        if argv[i] == "--log":
            i += 2
            continue
        if argv[i] == "--detach":
            i += 1
            continue
        clean.append(argv[i]); i += 1
    argv = clean
    pid = os.fork()
    if pid > 0:
        print(f"detached pid {pid}, log {log_path}")
        return 0
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    out = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(out, 1)
    os.dup2(out, 2)
    os.execv(sys.executable, [sys.executable] + argv)
    return 0


def main() -> int:
    if sys.version_info < (3, 9):
        sys.exit("autocontinue.py needs Python 3.9+ (found %d.%d)"
                 % sys.version_info[:2])

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session id (default: auto-detect running)")
    ap.add_argument("--all", dest="all_sessions", action="store_true",
                    help="watch every session that becomes active")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT,
                    help=f"text to send (default: {DEFAULT_PROMPT!r})")
    ap.add_argument("--delivery", choices=["queue", "steer"], default="queue")
    ap.add_argument("--max", type=int, default=0, help="stop after N prompts (0 = unlimited)")
    ap.add_argument("--min-interval", type=float, default=5.0,
                    help="never send two prompts closer than this (default 5s)")
    ap.add_argument("--retry-wait", type=float, default=60.0,
                    help="first backoff after a continue also hits the limit (default 60s)")
    ap.add_argument("--max-retry-wait", type=float, default=1800.0,
                    help="cap for the doubling backoff (default 1800s)")
    ap.add_argument("--poll", type=float, default=2.0, help="poll interval (default 2s)")
    ap.add_argument("--for", dest="for_seconds", type=float, default=0,
                    help="exit after N seconds (default: unlimited)")
    ap.add_argument("--dry-run", action="store_true", help="detect and log, send nothing")
    ap.add_argument("--router", action=argparse.BooleanOptionalAction, default=False,
                    help="restart the router WANs before each continue "
                         "(default: off)")
    ap.add_argument("--router-driver",
                    default=os.environ.get("ROUTER_DRIVER", "auto"),
                    help="router driver: auto, huawei_dg8045, zte_h168n, or "
                         "module:Class (default: ROUTER_DRIVER from .env, "
                         "else auto)")
    ap.add_argument("--ip-strategy",
                    default=os.environ.get("IP_CHANGE_STRATEGY", "pppoe_wan"),
                    help="IP-change strategy: pppoe_wan or module:Class "
                         "(default: IP_CHANGE_STRATEGY from .env)")
    ap.add_argument("--router-wans",
                    default=os.environ.get("ROUTER_WANS", ""),
                    help="comma-separated WAN names to restart, in order "
                         "(default: ROUTER_WANS from .env)")
    ap.add_argument("--router-settle", type=float, default=5.0,
                    help="wait after the last restart before checking the IP (default 5s)")
    ap.add_argument("--router-ip-wait", type=float, default=45.0,
                    help="max seconds to wait for a new external IP (default 45)")
    ap.add_argument("--router-timeout", type=float, default=60.0,
                    help="max seconds to wait for a WAN to reconnect (default 60)")
    ap.add_argument("--scan-text", action="store_true",
                    help="also treat a normal turn whose text mentions a limit as a limit stop")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--detach", action="store_true", help="fork into the background")
    ap.add_argument("--log", help="append log lines to this file")
    ap.add_argument("--stop-file", help="exit when this path exists")
    ap.add_argument("--url", help="server base URL")
    ap.add_argument("--no-auth", action="store_true", help="skip basic auth")
    args = ap.parse_args()
    args.router_wans = [w.strip() for w in args.router_wans.split(",") if w.strip()]

    if args.detach and not args.status and not os.environ.get("AUTOCONTINUE_CHILD"):
        os.environ["AUTOCONTINUE_CHILD"] = "1"   # inherited by the re-exec
        return detach([os.path.abspath(__file__)] + sys.argv[1:])

    url = (args.url or discover_url()).rstrip("/")
    client = Client(url, None if args.no_auth else load_password())
    log = Log(args.log)

    try:
        if args.status:
            return status(args, client, log)

        if args.all_sessions:
            targets: set[str] = set(client.active())
            if not targets:
                log("no active sessions; waiting for one to appear")
        else:
            targets = {resolve_session(client, args.session)}

        w = Watcher(args, client, log, targets)
        install_signals(w)
        return w.run()
    except urllib.error.URLError as e:
        log(f"cannot reach OpenCode at {url} ({getattr(e, 'reason', e)})")
        log("start OpenCode, or set OPENCODE_SERVER_URL")
        return 2
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
