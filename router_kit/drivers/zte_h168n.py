"""ZTE ZXHN H168N V3.5 router driver.

This driver follows the router's own Internet -> WAN flow: it opens the first
PPPoE WAN (for example INTERNET_TR069_R_PTM), fills in the PPPoE username and
password, and submits the form. The router redials that WAN and gets a new
public IP.

The low-level HTTP/session work is in zte_client.py. The existing Huawei
driver is left untouched.
"""
from __future__ import annotations

import os
import time

from router_kit.base import Log, RouterDriver, RouterError, register_driver
from router_kit.drivers.zte_client import Client

BAD_IPS = {"", "0.0.0.0", "::"}


class ZTEH168N(RouterDriver):
    name = "zte_h168n"

    def __init__(self):
        self.client: Client | None = None

    def connect(self) -> None:
        client = Client()
        try:
            client.connect()
        except Exception:
            client.close()
            raise
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def external_ip(self) -> str | None:
        statuses = self._status()
        names = [name.strip().lower()
                 for name in os.environ.get("ROUTER_WANS", "").split(",")
                 if name.strip()]

        for wanted in names:
            for wan in statuses:
                if wanted == str(wan.get("WANCName", "")).lower():
                    ip = str(wan.get("IPAddress", "")).strip()
                    if ip not in BAD_IPS:
                        return ip

        for wan in statuses:
            if str(wan.get("ConnStatus", "")) != "Connected":
                continue
            ip = str(wan.get("IPAddress", "")).strip()
            if ip not in BAD_IPS:
                return ip
        return None

    def wans(self) -> list[dict]:
        return [
            {
                "name": str(wan.get("WANCName") or ""),
                "alias": str(wan.get("WANCName") or ""),
                "status": str(wan.get("ConnStatus") or ""),
                "raw": wan,
            }
            for wan in self._status()
        ]

    def restart_wan(self, name: str, log: Log, timeout: float = 60.0) -> bool:
        if self.client is None:
            raise RouterError("router is not connected")

        wan = self._find(name)
        if wan is None:
            log(f"router: {name} not found, skipped")
            return False

        log(f"router: {name} is {wan['status']}; saving PPPoE credentials")
        self.client.apply(name)

        # The submit drops the link before the router redials it. Waiting for
        # that drop keeps the strategy from mistaking the old connection for
        # a successful reconnect.
        dropped = self._wait(name, lambda st: st != "Connected",
                             min(timeout, 30.0), log)
        if dropped is None:
            log(f"router: warning, {name} did not drop within "
                f"{min(timeout, 30.0):.0f}s")
        return True

    def _status(self) -> list[dict]:
        if self.client is None:
            raise RouterError("router is not connected")
        return self.client.wan_status()

    def _find(self, name: str) -> dict | None:
        wanted = name.strip().lower()
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


register_driver("zte_h168n", ZTEH168N)
