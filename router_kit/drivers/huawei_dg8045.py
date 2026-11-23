"""Huawei DG8045 router driver.

Wraps the low-level client in huawei_client.py. Add another driver in this
folder and import it from router_kit/drivers/__init__.py.
"""
from __future__ import annotations

import json
import time

from router_kit.base import Log, RouterDriver, RouterError, register_driver
from router_kit.drivers.huawei_client import Session

DISCONNECT, CONNECT = 1, 2


class HuaweiDG8045(RouterDriver):
    name = "huawei_dg8045"

    def __init__(self):
        self.session: Session | None = None

    def connect(self) -> None:
        session = Session()
        result = session.login_result
        if result.get("errorCategory") != "ok":
            session.close()
            raise RouterError(f"router login failed: {result}")
        self.session = session

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def external_ip(self) -> str | None:
        return self._json("system/firstupwan").get("ExternalIPAddress")

    def wans(self) -> list[dict]:
        data = self._json("ntwk/wan")
        items = data if isinstance(data, list) else [data]
        return [
            {
                "name": str(item.get("Name") or ""),
                "alias": str(item.get("Alias") or ""),
                "status": str(item.get("ConnectionStatus") or ""),
                "raw": item,
            }
            for item in items
        ]

    def restart_wan(self, name: str, log: Log, timeout: float = 60.0) -> bool:
        wan = self._find(name)
        if wan is None:
            log(f"router: {name} not found, skipped")
            return False

        status = wan["status"]
        log(f"router: {name} is {status}")
        if status == "Connected":
            self._trigger(wan, DISCONNECT)
            self._wait(name, lambda st: st != "Connected",
                       min(timeout, 45.0), log)
            time.sleep(2)
        self._trigger(wan, CONNECT)
        return True

    def _json(self, objname: str):
        if self.session is None:
            raise RouterError("router is not connected")
        _, _, text = self.session.api(objname)
        return json.loads(text)

    def _find(self, name: str) -> dict | None:
        wanted = name.lower()
        for wan in self.wans():
            if wanted in (wan["name"].lower(), wan["alias"].lower()):
                return wan
        return None

    def _wait(self, name: str, predicate, timeout: float, log: Log):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            wan = self._find(name)
            if wan is None:
                return None
            status = wan["status"]
            if status != last:
                log(f"router: {name} -> {status}")
                last = status
            if predicate(status):
                return status
            time.sleep(2)
        return None

    def _trigger(self, wan: dict, code: int) -> None:
        if self.session is None:
            raise RouterError("router is not connected")
        _, result = self.session.post_api("ntwk/wan", {
            "action": "trigger",
            "data": {"Path": wan["raw"]["ID"], "Trigger": code},
        })
        if isinstance(result, dict):
            err = result.get("errcode", result.get("errorCategory", "0"))
            if str(err) not in ("0", "ok", "None"):
                raise RouterError(f"trigger {code} rejected: {result}")


register_driver("huawei_dg8045", HuaweiDG8045)
