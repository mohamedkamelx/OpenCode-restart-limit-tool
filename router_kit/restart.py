"""Configured router/IP-change entry point.

autocontinue.py calls restart(); the router driver and IP-change strategy are
selected by ROUTER_DRIVER and IP_CHANGE_STRATEGY (or the matching CLI flags):

    ROUTER_DRIVER=auto                  try every registered driver
    ROUTER_DRIVER=huawei_dg8045         built-in driver
    ROUTER_DRIVER=my_router:MyDriver    external driver plugin

    IP_CHANGE_STRATEGY=pppoe_wan        built-in strategy
    IP_CHANGE_STRATEGY=my_ip:MyChange   external strategy plugin

Drivers and strategies register themselves with router_kit.register_driver()
and router_kit.register_strategy().
"""
from __future__ import annotations

import os

import router_kit.drivers     # noqa: F401  (registers built-in drivers)
import router_kit.strategies  # noqa: F401  (registers built-in strategies)
from router_kit.base import (RouterError, load_plugins, resolve_driver,
                             resolve_strategy)

__all__ = ["RouterError", "restart"]


def restart(names, log, settle: float = 5.0, ip_wait: float = 45.0,
            timeout: float = 60.0, dry_run: bool = False,
            driver: str | None = None,
            strategy: str | None = None) -> dict:
    """Restart the configured WANs and wait for a new external IP.

    Returns {"before": ip, "after": ip, "changed": bool}.
    """
    load_plugins(os.environ.get("ROUTER_PLUGINS"))
    driver_spec = driver or os.environ.get("ROUTER_DRIVER", "auto")
    strategy_spec = strategy or os.environ.get("IP_CHANGE_STRATEGY", "pppoe_wan")

    router = resolve_driver(driver_spec, log)
    try:
        changer = resolve_strategy(strategy_spec)
        log(f"router: driver={router.name} strategy={changer.name}")
        return changer.change(
            router, log,
            wans=names,
            settle=settle,
            ip_wait=ip_wait,
            timeout=timeout,
            dry_run=dry_run,
        )
    finally:
        try:
            router.close()
        except Exception:
            pass
