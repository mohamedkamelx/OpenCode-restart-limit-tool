"""PPPoE WAN-bounce IP-change strategy.

Restarts the configured WANs in order, waits for the first link to come back,
then polls until the external IP changes. This is the built-in strategy used
when IP_CHANGE_STRATEGY is unset. Add another strategy in this folder and
import it from router_kit/strategies/__init__.py.
"""
from __future__ import annotations

import time

from router_kit.base import (IpChangeStrategy, Log, RouterDriver,
                             register_strategy)


class PPPoEWanStrategy(IpChangeStrategy):
    name = "pppoe_wan"

    def change(self, driver: RouterDriver, log: Log, *, wans,
               settle: float = 5.0, ip_wait: float = 45.0,
               timeout: float = 60.0, dry_run: bool = False) -> dict:
        if not wans:
            log("router: no WANs configured "
                "(set ROUTER_WANS in .env or pass --router-wans)")
            return {"before": None, "after": None, "changed": False}

        before = driver.external_ip()
        log(f"router: current external IP {before}")

        if dry_run:
            for name in wans:
                wan = _find(driver.wans(), name)
                state = wan["status"] if wan else "MISSING"
                log(f"router: would restart {name} ({state})")
            log(f"router: would settle {settle:.0f}s then wait up to "
                f"{ip_wait:.0f}s for an IP change")
            return {"before": before, "after": before, "changed": False}

        for name in wans:
            try:
                driver.restart_wan(name, log, timeout)
            except Exception as exc:
                log(f"router: {name} restart failed ({exc})")

            # the first (live) link must come back before moving on
            if name == wans[0]:
                back = _wait_status(driver, name,
                                    lambda status: status == "Connected",
                                    timeout, log)
                if back is None:
                    log(f"router: warning, {name} did not reconnect "
                        f"within {timeout:.0f}s")

        log(f"router: settling {settle:.0f}s")
        time.sleep(settle)

        after = before
        deadline = time.time() + ip_wait
        while time.time() < deadline:
            try:
                after = driver.external_ip()
            except Exception:
                after = None
            if after and after != before:
                break
            time.sleep(3)

        changed = bool(after and before and after != before)
        log(f"router: external IP {before} -> {after}"
            + ("" if changed else "  [UNCHANGED]"))
        return {"before": before, "after": after, "changed": changed}


def _find(wans: list[dict], name: str) -> dict | None:
    wanted = name.lower()
    for wan in wans:
        if wanted in (wan.get("name", "").lower(),
                      wan.get("alias", "").lower()):
            return wan
    return None


def _wait_status(driver: RouterDriver, name: str, predicate,
                 timeout: float, log: Log):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        wan = _find(driver.wans(), name)
        if wan is None:
            return None
        status = wan.get("status", "")
        if status != last:
            log(f"router: {name} -> {status}")
            last = status
        if predicate(status):
            return status
        time.sleep(2)
    return None


register_strategy("pppoe_wan", PPPoEWanStrategy)
