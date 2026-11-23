"""Pluggable router drivers and IP-change strategies.

A driver knows how to talk to one router family. A strategy knows how to
change the external IP using a driver. Both are selected by name, or by a
"module:Class" plugin path, so a different router or a different IP-change
method can be added without touching autocontinue.py.

Built-in registrations happen in router_kit/drivers/ and
router_kit/strategies/.
"""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import Callable

Log = Callable[[str], None]


class RouterError(RuntimeError):
    pass


class RouterDriver(ABC):
    """One router family. Implement these methods to add a new router."""

    name = "router"

    @abstractmethod
    def connect(self) -> None:
        """Open a session and verify that this is the expected router."""

    @abstractmethod
    def close(self) -> None:
        """Release the session. Must be safe to call more than once."""

    @abstractmethod
    def external_ip(self) -> str | None:
        """Return the current WAN IP, or None if it cannot be read."""

    @abstractmethod
    def wans(self) -> list[dict]:
        """Return the WAN list.

        Each item must contain:
            name    - configured WAN name
            alias   - optional alias (may be empty)
            status  - connection status string, e.g. "Connected"
            raw     - the driver's original object
        """

    @abstractmethod
    def restart_wan(self, name: str, log: Log, timeout: float = 60.0) -> bool:
        """Disconnect and reconnect one WAN. Return False if it is missing."""

    def reboot(self, log: Log) -> None:
        """Optional: reboot the router. Strategies may use this."""
        raise RouterError(f"{self.name} does not support reboot")


class IpChangeStrategy(ABC):
    """One way of obtaining a new external IP."""

    name = "strategy"

    @abstractmethod
    def change(self, driver: RouterDriver, log: Log, **options) -> dict:
        """Change the IP. Return {"before": ip, "after": ip, "changed": bool}."""


DRIVERS: dict[str, Callable[[], RouterDriver]] = {}
STRATEGIES: dict[str, Callable[[], IpChangeStrategy]] = {}


def register_driver(name: str, factory: Callable[[], RouterDriver]) -> None:
    DRIVERS[name] = factory


def register_strategy(name: str,
                      factory: Callable[[], IpChangeStrategy]) -> None:
    STRATEGIES[name] = factory


def load_plugins(spec: str | None) -> list[str]:
    """Import comma-separated modules so they can register plugins.

    Use ROUTER_PLUGINS=my_router,my_ip to make external drivers/strategies
    available to ROUTER_DRIVER=auto and IP_CHANGE_STRATEGY names.
    """
    loaded = []
    for name in (spec or "").split(","):
        name = name.strip()
        if not name:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:
            raise RouterError(
                f"cannot import plugin module {name!r}: {exc}") from exc
        loaded.append(name)
    return loaded


def resolve_driver(spec: str | None, log: Log) -> RouterDriver:
    """Return a connected driver.

    spec:
        "auto"                 try every registered driver, in order
        "huawei_dg8045"        a registered driver name
        "module:Class"         import a driver plugin
        "module:create"        import a zero-argument factory
    """
    if not spec or spec == "auto":
        return _detect_driver(log)
    return _create(spec, DRIVERS, "router driver")


def resolve_strategy(spec: str | None) -> IpChangeStrategy:
    """Return an IP-change strategy by registered name or plugin path."""
    return _create(spec or "pppoe_wan", STRATEGIES, "IP-change strategy")


def _detect_driver(log: Log) -> RouterDriver:
    if not DRIVERS:
        raise RouterError("no router drivers registered")

    failures = []
    for name, factory in DRIVERS.items():
        driver = factory()
        try:
            driver.connect()
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            try:
                driver.close()
            except Exception:
                pass
            continue
        log(f"router: auto-detected driver {driver.name}")
        return driver

    raise RouterError("no router driver detected ("
                      + "; ".join(failures) + ")")


def _create(spec: str, registry: dict, kind: str):
    factory = registry.get(spec)
    if factory is not None:
        return factory()

    module_name, sep, attr = spec.partition(":")
    if not sep:
        module_name, _, attr = spec.rpartition(".")
    if not module_name or not attr:
        raise RouterError(
            f"unknown {kind} {spec!r}; use a registered name or module:Class")

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise RouterError(
            f"cannot import {kind} module {module_name!r}: {exc}") from exc

    try:
        obj = getattr(module, attr)
    except AttributeError as exc:
        raise RouterError(
            f"{module_name!r} has no {attr!r} for {kind}") from exc

    try:
        return obj() if callable(obj) else obj
    except Exception as exc:
        raise RouterError(
            f"cannot create {kind} from {spec!r}: {exc}") from exc
