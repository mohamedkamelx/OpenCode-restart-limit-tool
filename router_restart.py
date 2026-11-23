"""Stable entry point for the router/IP-change package.

The implementation lives in router_kit/. autocontinue.py imports restart()
from here, so the package layout can change without touching the watcher.
"""
from router_kit.restart import RouterError, restart

__all__ = ["RouterError", "restart"]
