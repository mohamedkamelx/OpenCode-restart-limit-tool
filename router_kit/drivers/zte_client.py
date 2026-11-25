"""Self-contained client for a ZTE ZXHN H168N V3.5 gateway.

The router changes its public IP when the first PPPoE WAN is saved again with
its username and password. This client follows the router's own browser flow:

1. log in with the administrator credentials (SHA256 of password + login token)
2. open an authenticated page to activate the session and read its token
3. read the Internet -> WAN configuration XML
4. POST that WAN back with the PPPoE credentials filled in

Connection details come from the environment (loaded from .env by loadenv.py):

    ROUTER_HOST, ROUTER_PORT        gateway address (port 80 for this router)
    ROUTER_USER, ROUTER_PASS        administrator login
    ROUTER_PPP_USER, ROUTER_PPP_PASS  PPPoE credentials for the selected WAN

No third-party modules are used.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

from loadenv import load_env
from router_kit.base import RouterError

load_env()

CFG_API = "/common_page/Internet_Internet_lua.lua?TypeUplink=1&pageType=0"
STA_API = "/common_page/Internet_Internet_lua.lua?TypeUplink=1&pageType=1"
ACTIVATE = "/getpage.lua?pid=123&nextpage=Internet_AdminInternetStatus_DSL_t.lp&Menu3Location=0"
LOGIN_TOKEN = "/function_module/login_module/login_page/logintoken_lua.lua"

# Field order from the router's own Apply POST. Keeping it avoids surprises
# with firmware that is picky about form layout.
KEYS = [
    "_InstID", "InstHasGot", "ControlType", "WANCName", "xdslMode",
    "sub_DestAddress0", "sub_DestAddress1", "ATMEncapsulation", "ATMQoS",
    "ATMPeakCellRate", "ATMSCR", "ATMMaxBurstSize", "mode", "StrServList",
    "Servlist_INTERNET", "Servlist_TR069", "Servlist_IPTV", "Servlist_MGT",
    "MTU", "linkMode", "TransType", "PrefixUserName", "UserName", "Password",
    "AuthType", "ConnTrigger", "IdleTime0", "IdleTime1", "IpMode",
    "Addressingtype", "IPAddress0", "IPAddress1", "IPAddress2", "IPAddress3",
    "SubnetMask0", "SubnetMask1", "SubnetMask2", "SubnetMask3",
    "GateWay0", "GateWay1", "GateWay2", "GateWay3",
    "DNS10", "DNS11", "DNS12", "DNS13", "DNS20", "DNS21", "DNS22", "DNS23",
    "DNS30", "DNS31", "DNS32", "DNS33", "IsNAT", "IPv6AcquireMode", "Gua1",
    "Gua1PrefixLen", "Gateway6", "Pd", "PdLen", "Dns1v6", "Dns2v6", "Dns3v6",
    "IsPD", "Unnumbered", "IsSLAAC", "IsGUA", "IsPdAddr", "VlanEnable",
    "VLANID", "Priority", "Btn_cancel_internet", "Btn_apply_internet",
]

# Values supplied by the hidden template on the WAN page rather than by XML.
DEFAULTS = {
    "InstHasGot": "1", "ControlType": "0", "sub_DestAddress0": "",
    "sub_DestAddress1": "", "ATMEncapsulation": "LLC", "ATMQoS": "UBR",
    "ATMPeakCellRate": "", "ATMSCR": "", "ATMMaxBurstSize": "",
    "Servlist_INTERNET": "1", "Servlist_TR069": "1", "Servlist_IPTV": "0",
    "Servlist_MGT": "0", "PrefixUserName": "ETIS_", "IdleTime0": "20",
    "IdleTime1": "0", "Addressingtype": "DHCP", "IPAddress0": "",
    "IPAddress1": "", "IPAddress2": "", "IPAddress3": "", "SubnetMask0": "",
    "SubnetMask1": "", "SubnetMask2": "", "SubnetMask3": "", "GateWay0": "",
    "GateWay1": "", "GateWay2": "", "GateWay3": "",
    "DNS10": "0", "DNS11": "0", "DNS12": "0", "DNS13": "0",
    "DNS20": "0", "DNS21": "0", "DNS22": "0", "DNS23": "0",
    "DNS30": "0", "DNS31": "0", "DNS32": "0", "DNS33": "0",
    "Gua1": "", "Gua1PrefixLen": "128", "Gateway6": "", "Dns1v6": "",
    "Dns2v6": "", "Dns3v6": "", "Unnumbered": "0", "VLANID": "",
    "Priority": "0", "Btn_cancel_internet": "", "Btn_apply_internet": "",
}


def decode_js_string(raw: str) -> str:
    """Decode the router's quoted \\xNN session token."""
    return re.sub(r"\\x([0-9a-fA-F]{2})",
                  lambda m: chr(int(m.group(1), 16)), raw or "")


class Client:
    """One HTTP session with the ZTE gateway."""

    def __init__(self, host=None, port=None, user=None, password=None,
                 ppp_user=None, ppp_pass=None, timeout: float = 15.0):
        self.host = str(host or os.environ.get("ROUTER_HOST", "192.168.1.1"))
        self.port = int(port or os.environ.get("ROUTER_PORT", "80"))
        self.user = str(user or os.environ.get("ROUTER_USER", ""))
        self.password = str(password or os.environ.get("ROUTER_PASS", ""))
        self.ppp_user = str(ppp_user or os.environ.get("ROUTER_PPP_USER", ""))
        self.ppp_pass = str(ppp_pass or os.environ.get("ROUTER_PPP_PASS", ""))
        self.timeout = float(os.environ.get("ROUTER_CONNECT_TIMEOUT", timeout))
        self.conn = None
        self.cookies = {}
        self.session_token = ""

    def connect(self) -> None:
        if not self.user or not self.password:
            raise RouterError(
                "router credentials missing: set ROUTER_USER / ROUTER_PASS in .env")
        self.conn = http.client.HTTPConnection(self.host, self.port,
                                               timeout=self.timeout)
        self.login()
        self.activate()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None

    def _request(self, method: str, path: str, body: str | None = None,
                 headers: dict | None = None) -> tuple[int, str]:
        payload = body.encode("utf-8") if body is not None else None
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "Chrome/158 Safari/537.36",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        if self.cookies:
            hdrs["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.cookies.items())
        if payload is not None:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            hdrs.update(headers)

        for attempt in range(2):
            if self.conn is None:
                self.conn = http.client.HTTPConnection(
                    self.host, self.port, timeout=self.timeout)
            try:
                self.conn.request(method, path, body=payload, headers=hdrs)
                response = self.conn.getresponse()
                text = response.read().decode("utf-8", "replace")
                self._store_cookies(response)
                return response.status, text
            except (OSError, http.client.HTTPException, EOFError) as exc:
                self.close()
                if attempt:
                    raise RouterError(
                        f"router request {method} {path} failed: {exc}") from exc

        raise RouterError("router request failed")

    def _store_cookies(self, response) -> None:
        for name, value in response.getheaders():
            if name.lower() != "set-cookie":
                continue
            pair = value.split(";", 1)[0]
            if "=" in pair:
                key, val = pair.split("=", 1)
                self.cookies[key.strip()] = val.strip()

    def login(self) -> None:
        page = self._request("GET", "/")[1]
        token_xml = self._request("GET", LOGIN_TOKEN)[1]
        challenge = re.sub(r"<[^>]+>", "", token_xml).strip()
        match = re.search(
            r'addParameter\("_sessionTOKEN",\s*"([^"]+)"', page)
        if not challenge or not match:
            raise RouterError("cannot read ZTE login tokens")

        password_hash = hashlib.sha256(
            (self.password + challenge).encode()).hexdigest()
        body = urllib.parse.urlencode({
            "Username": self.user,
            "Password": password_hash,
            "action": "login",
            "_sessionTOKEN": match.group(1),
        })
        status, _ = self._request("POST", "/", body, {
            "Origin": f"http://{self.host}",
            "Referer": f"http://{self.host}/",
            "Upgrade-Insecure-Requests": "1",
        })
        if status not in (200, 302) or "SID" not in self.cookies:
            raise RouterError(f"ZTE login failed (HTTP {status})")

    def activate(self) -> None:
        """Open a real page: the API returns 404 until the session is used."""
        status, page = self._request("GET", ACTIVATE)
        if status != 200:
            raise RouterError(f"cannot activate ZTE session (HTTP {status})")
        match = re.search(r'_sessionTmpToken\s*=\s*"([^"]+)"', page)
        if not match:
            raise RouterError("cannot read ZTE session token")
        self.session_token = decode_js_string(match.group(1))

    def instances(self, path: str) -> list[dict]:
        status, text = self._request("GET", path, None, {
            "Accept": "application/xml, text/xml, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"http://{self.host}/",
        })
        if status != 200:
            raise RouterError(f"ZTE API {path} returned HTTP {status}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise RouterError(
                f"ZTE API {path} returned non-XML: {text[:80]!r}") from exc
        error = root.findtext("IF_ERRORSTR")
        if error and error != "SUCC":
            raise RouterError(f"ZTE API {path} error: {error}")
        out = []
        for inst in root.findall("./ID_WAN_COMFIG/Instance"):
            kids = list(inst)
            data = {}
            for i in range(0, len(kids) - 1, 2):
                name = (kids[i].text or "").strip()
                if name:
                    data[name] = kids[i + 1].text or ""
            out.append(data)
        return out

    def wan_config(self) -> list[dict]:
        return self.instances(CFG_API)

    def wan_status(self) -> list[dict]:
        return self.instances(STA_API)

    def _find_config(self, name: str) -> dict:
        wanted = name.strip().lower()
        for wan in self.wan_config():
            if wanted == str(wan.get("WANCName", "")).lower():
                return wan
        raise RouterError(f"WAN {name!r} not found on the ZTE router")

    def _apply_once(self, name: str) -> str:
        cfg = self._find_config(name)
        values = dict(DEFAULTS)
        values.update(cfg)

        user = self.ppp_user
        if user:
            prefix = str(values.get("PrefixUserName", ""))
            values["UserName"] = (user if user.startswith(prefix)
                                  else prefix + user)
        if self.ppp_pass:
            values["Password"] = self.ppp_pass

        body = urllib.parse.urlencode(
            [("IF_ACTION", "Apply")]
            + [(key, str(values.get(key, ""))) for key in KEYS]
            + [("_sessionTOKEN", self.session_token)]
        )
        status, text = self._request("POST", CFG_API, body, {
            "Origin": f"http://{self.host}",
            "Referer": f"http://{self.host}/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/xml, text/xml, */*; q=0.01",
        })
        if status != 200:
            raise RouterError(f"ZTE Apply returned HTTP {status}")
        if "expired" in text.lower():
            raise RouterError("ZTE session expired")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise RouterError(
                f"ZTE Apply returned non-XML: {text[:80]!r}") from exc
        error = root.findtext("IF_ERRORSTR") or ""
        if error and error != "SUCC":
            raise RouterError(f"ZTE Apply failed: {error}")
        return text

    def apply(self, name: str) -> str:
        """Save the WAN with PPPoE credentials; the router redials it."""
        self.activate()
        try:
            return self._apply_once(name)
        except RouterError as exc:
            if "expired" not in str(exc).lower():
                raise
            self.activate()
            return self._apply_once(name)
