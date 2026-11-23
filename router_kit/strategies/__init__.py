"""Built-in IP-change strategies.

Add a new strategy file in this folder and import it below so it registers
itself with router_kit.register_strategy().
"""
from router_kit.strategies import pppoe_wan  # noqa: F401

__all__ = ["pppoe_wan"]
