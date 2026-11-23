"""Pluggable router drivers and IP-change strategies.

Public imports for plugins:

    from router_kit import RouterDriver, IpChangeStrategy
    from router_kit import register_driver, register_strategy

Built-in drivers live in router_kit/drivers/ and built-in strategies in
router_kit/strategies/.
"""
from router_kit.base import (
    IpChangeStrategy,
    Log,
    RouterDriver,
    RouterError,
    load_plugins,
    register_driver,
    register_strategy,
    resolve_driver,
    resolve_strategy,
)
from router_kit import drivers as _drivers  # noqa: F401  (built-in drivers)
from router_kit import strategies as _strategies  # noqa: F401  (built-in strategies)

__all__ = [
    "IpChangeStrategy",
    "Log",
    "RouterDriver",
    "RouterError",
    "load_plugins",
    "register_driver",
    "register_strategy",
    "resolve_driver",
    "resolve_strategy",
]
