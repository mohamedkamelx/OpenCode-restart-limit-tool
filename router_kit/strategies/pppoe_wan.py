"""PPPoE WAN-bounce IP-change strategy.

Restarts the configured WANs, then polls until the external IP changes. The
built-in drivers that support the fast path drop every link at once and bring
each one back as soon as it reports down, so the whole cycle costs one
reconnect instead of one reconnect per WAN. Drivers without that hook keep the
old sequential restart_wan() behaviour.

The polling interval is deliberately short (a few hundred milliseconds): the
work itself takes seconds, and waiting 2-3 s per poll made the tool look stuck
long after the router was already back.
"""
from __future__ import annotations

import time

from router_kit.base import (IpChangeStrategy, Log, RouterDriver,
                             register_strategy)

POLL = 0.4


class PPPoEWanStrategy(IpChangeStrategy):
    name = "pppoe_wan"

    def change(self, driver: RouterDriver, log: Log, *, wans,
               settle: float = 1.0, ip_wait: float = 30.0,
               timeout: float = 30.0, dry_run: bool = False) -> dict:
        names = [w.strip() for w in (wans or []) if w and w.strip()]
        if not names:
            log("router: no WANs configured "
                "(set ROUTER_WANS in .env or pass --router-wans)")
            return {"before": None, "after": None, "changed": False}

        before = _safe_ip(driver)
        log(f"router: current external IP {before}")

        if dry_run:
            for name in names:
                wan = _find(driver.wans(), name)
                state = wan["status"] if wan else "MISSING"
                log(f"router: would restart {name} ({state})")
            log(f"router: would wait up to {ip_wait:.0f}s for an IP change")
            return {"before": before, "after": before, "changed": False}

        if getattr(driver, "supports_fast_restart", False):
            _restart_together(driver, log, names, timeout)
        else:
            for name in names:
                try:
                    driver.restart_wan(name, log, timeout)
                except Exception as exc:
                    log(f"router: {name} restart failed ({exc})")

        after = _wait_for_new_ip(driver, log, names, before, settle, ip_wait,
                                 timeout)
        changed = bool(after and before and after != before)
        log(f"router: external IP {before} -> {after}"
            + ("" if changed else "  [UNCHANGED]"))
        return {"before": before, "after": after, "changed": changed}


def _restart_together(driver: RouterDriver, log: Log, names: list[str],
                      timeout: float) -> None:
    """Drop every WAN, then reconnect each one as soon as it is down."""
    log("router: restarting " + ", ".join(names) + " together")
    pending: set[str] = set()
    for name in names:
        try:
            if driver.disconnect_wan(name, log):
                pending.add(name)
        except Exception as exc:
            log(f"router: {name} disconnect failed ({exc})")

    # Reconnect each link independently so the faster WAN does not wait for
    # the slower one. PendingDisconnect still blocks a new connect; anything
    # else means the link has let go.
    deadline = time.time() + min(timeout, 25.0)
    while pending and time.time() < deadline:
        states = _states(driver, names)
        for name in list(pending):
            status = states.get(name)
            if status is None or status not in ("Connected", "PendingDisconnect"):
                try:
                    driver.connect_wan(name, log)
                except Exception as exc:
                    log(f"router: {name} connect failed ({exc})")
                pending.discard(name)
        if pending:
            time.sleep(0.3)

    # A link that never reported down must still be brought back up.
    for name in pending:
        try:
            driver.connect_wan(name, log)
        except Exception as exc:
            log(f"router: {name} connect failed ({exc})")


def _wait_for_new_ip(driver: RouterDriver, log: Log, names: list[str],
                     before, settle: float, ip_wait: float,
                     timeout: float):
    after = before
    stable_since: float | None = None
    all_up = False
    deadline = time.time() + max(ip_wait, timeout)
    while time.time() < deadline:
        after = _safe_ip(driver)
        states = _states(driver, names)
        all_up = bool(states) and all(st == "Connected"
                                      for st in states.values())
        changed = bool(after and before and after != before)
        if changed and all_up:
            if stable_since is None:
                stable_since = time.time()
                if settle > 0:
                    log(f"router: new external IP {before} -> {after}, "
                        f"confirming for {settle:.1f}s")
            elif time.time() - stable_since >= settle:
                return after
        else:
            stable_since = None
        time.sleep(POLL)
    if not all_up:
        log("router: warning, not every WAN is Connected yet")
    return after


def _safe_ip(driver: RouterDriver):
    try:
        return driver.external_ip()
    except Exception:
        return None


def _states(driver: RouterDriver, names: list[str]) -> dict[str, str]:
    try:
        wans = driver.wans()
    except Exception:
        return {}
    out = {}
    for name in names:
        wan = _find(wans, name)
        if wan is not None:
            out[name] = wan.get("status", "")
    return out


def _find(wans: list[dict], name: str) -> dict | None:
    wanted = name.lower()
    for wan in wans:
        if wanted in (wan.get("name", "").lower(),
                      wan.get("alias", "").lower()):
            return wan
    return None


register_strategy("pppoe_wan", PPPoEWanStrategy)
