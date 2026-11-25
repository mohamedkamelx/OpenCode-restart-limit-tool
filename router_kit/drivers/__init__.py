"""Built-in router drivers.

Add a new driver file in this folder and import it below so it registers
itself with router_kit.register_driver().
"""
from router_kit.drivers import huawei_dg8045  # noqa: F401
from router_kit.drivers import zte_h168n  # noqa: F401

__all__ = ["huawei_dg8045", "zte_h168n"]
