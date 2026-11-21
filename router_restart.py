"""Restart the DG8045 PPPoE WAN connections before resuming a session.

Self-contained: the gateway client lives in router_client.py, in this folder.

Sequence used by autocontinue:

    VID_20 restart (disconnect + connect, wait until Connected)
    PTM3 trigger
    settle 5s
    wait for the external IP to actually change
    -> then the caller sends "continue"
"""
from __future__ import annotations

import json
import time

from router_client import Session as _RouterSession

DISCONNECT, CONNECT = 1, 2


class RouterError(RuntimeError):
    pass


def _open() -> _RouterSession:
    s = _RouterSession()
    if s.login_result.get("errorCategory") != "ok":
        raise RouterError(f"router login failed: {s.login_result}")
    return s


def _wans(s) -> list:
    st, _, txt = s.api("ntwk/wan")
    data = json.loads(txt)
    return data if isinstance(data, list) else [data]


def _find(s, name: str) -> dict | None:
    wanted = name.lower()
    for w in _wans(s):
        if wanted in (str(w.get("Name", "")).lower(), str(w.get("Alias", "")).lower()):
            return w
    return None


def wan_ip(s) -> str | None:
    st, _, txt = s.api("system/firstupwan")
    return json.loads(txt).get("ExternalIPAddress")


def _trigger(s, wan: dict, code: int) -> None:
    st, res = s.post_api("ntwk/wan", {
        "action": "trigger",
        "data": {"Path": wan["ID"], "Trigger": code},
    })
    if isinstance(res, dict):
        err = res.get("errcode", res.get("errorCategory", "0"))
        if str(err) not in ("0", "ok", "None"):
            raise RouterError(f"trigger {code} rejected: {res}")


def _wait_status(s, name: str, predicate, timeout: float, log) -> str | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        w = _find(s, name)
        if w is None:
            return None
        status = w.get("ConnectionStatus")
        if status != last:
            log(f"router: {name} -> {status}")
            last = status
        if predicate(status):
            return status
        time.sleep(2)
    return None


def bounce(s, name: str, log, timeout: float = 60) -> bool:
    """Disconnect + reconnect one WAN. Inactive WANs get a connect only."""
    w = _find(s, name)
    if w is None:
        log(f"router: {name} not found, skipped")
        return False

    status = w.get("ConnectionStatus")
    log(f"router: {name} is {status}")
    if status == "Connected":
        _trigger(s, w, DISCONNECT)
        _wait_status(s, name, lambda st: st != "Connected", min(timeout, 45), log)
        time.sleep(2)
    _trigger(s, w, CONNECT)
    return True


def restart(names, log, settle: float = 5.0, ip_wait: float = 45.0,
            timeout: float = 60.0, dry_run: bool = False) -> dict:
    """Restart each WAN, then wait for a new external IP.

    Returns {"before": ip, "after": ip, "changed": bool}.
    """
    s = _open()
    before = wan_ip(s)
    log(f"router: current external IP {before}")

    if dry_run:
        for name in names:
            w = _find(s, name)
            state = w.get("ConnectionStatus") if w else "MISSING"
            log(f"router: would restart {name} ({state})")
        log(f"router: would settle {settle:.0f}s then wait up to {ip_wait:.0f}s for an IP change")
        return {"before": before, "after": before, "changed": False}

    for name in names:
        try:
            bounce(s, name, log, timeout)
        except Exception as exc:
            log(f"router: {name} restart failed ({exc})")

        # the live link must come back before moving on
        if name.upper().endswith("VID_20"):
            back = _wait_status(s, name, lambda st: st == "Connected", timeout, log)
            if back is None:
                log(f"router: warning, {name} did not reconnect within {timeout:.0f}s")

    log(f"router: settling {settle:.0f}s")
    time.sleep(settle)

    after = before
    deadline = time.time() + ip_wait
    while time.time() < deadline:
        try:
            after = wan_ip(s)
        except Exception:
            after = None
        if after and after != before:
            break
        time.sleep(3)

    changed = bool(after and before and after != before)
    log(f"router: external IP {before} -> {after}"
        + ("" if changed else "  [UNCHANGED]"))
    return {"before": before, "after": after, "changed": changed}
