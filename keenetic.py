import time
from hashlib import md5, sha256
from json import JSONDecodeError, loads
from typing import Any, Dict, List, Optional, Tuple

from requests import Session

from app.configuration import Config


class ConnectedDevice:
    def __init__(self, dictionary):
        self.__dict__ = {
            key.replace("-", "_"): value for key, value in dictionary.items()
        }

    def __getattr__(self, attr):
        return self.__dict__.get(attr)

    def __str__(self):
        return f"ConnectedDevice({self.__dict__})"


class ApiRouter:
    """Keenetic RCI client with a persistent HTTP session.

    Auth cookie is reused across polls. Re-login happens only on 401/403
    (single retry), not on ordinary API errors or empty optional probes.
    """

    def __init__(self, username="admin", password="", host="192.168.1.1", port=80):
        self.__session = Session()
        self.host = str(host or "").strip()
        self.port = int(port or 80)
        self.__endpoint = f"http://{self.host}:{self.port}"
        if self.port == 443:
            self.__endpoint = f"https://{self.host}:{self.port}"
        self.__username = username or ""
        self.__password = password or ""
        self.isAuth = False
        # Optional RCI features absent on this firmware (do not re-probe each poll)
        self._missing_show: set = set()
        self._missing_paths: set = set()
        self._vpn_probed_show: set = set()
        self.auth()

    @property
    def credentials_key(self) -> Tuple[str, int, str, str]:
        return (self.host, self.port, self.__username, self.__password)

    def update_credentials(self, username: str, password: str, host: str = None, port: int = None) -> bool:
        """Update credentials/endpoint in-place. Returns True if session was reset."""
        new_host = str(host if host is not None else self.host).strip()
        new_port = int(port if port is not None else self.port or 80)
        new_user = username or ""
        new_pass = password or ""
        changed = (
            new_host != self.host
            or new_port != self.port
            or new_user != self.__username
            or new_pass != self.__password
        )
        if not changed:
            return False
        self.host = new_host
        self.port = new_port
        self.__username = new_user
        self.__password = new_pass
        self.__endpoint = f"http://{self.host}:{self.port}"
        if self.port == 443:
            self.__endpoint = f"https://{self.host}:{self.port}"
        self.__session = Session()
        self.isAuth = False
        self._missing_show.clear()
        self._missing_paths.clear()
        self._vpn_probed_show.clear()
        self.auth()
        return True

    def close(self):
        """Drop auth and close the persistent HTTP session."""
        self.isAuth = False
        self._missing_show.clear()
        self._missing_paths.clear()
        self._vpn_probed_show.clear()
        try:
            self.__session.close()
        except Exception:
            pass
        self.__session = Session()

    def auth(self) -> bool:
        try:
            response = self.__session.get(
                self.__endpoint + "/auth",
                timeout=Config.HTTP_REQUEST_TIMEOUT,
            )
            if response.status_code == 401:
                realm = response.headers["X-NDM-Realm"]
                password = f"{self.__username}:{realm}:{self.__password}"
                password = md5(password.encode("utf-8"))
                challenge = response.headers["X-NDM-Challenge"]
                password = challenge + password.hexdigest()
                password = sha256(password.encode("utf-8")).hexdigest()
                response = self.__session.post(
                    self.__endpoint + "/auth",
                    json={"login": self.__username, "password": password},
                    timeout=Config.HTTP_REQUEST_TIMEOUT,
                )
                self.isAuth = response.status_code == 200
            elif response.status_code == 200:
                # Existing session cookie still valid
                self.isAuth = True
            else:
                self.isAuth = False
        except Exception:
            self.isAuth = False
        return self.isAuth

    def get(self, address, params=None):
        return self.__session.get(
            self.__endpoint + address,
            params=params or {},
            timeout=Config.HTTP_REQUEST_TIMEOUT,
        )

    def post(self, address, data):
        return self.__session.post(
            self.__endpoint + address,
            json=data,
            timeout=Config.HTTP_REQUEST_TIMEOUT,
        )

    def _ensure_auth(self) -> bool:
        if self.isAuth:
            return True
        return self.auth()

    def _request(self, method: str, path: str, data: Any = None, params=None, *, retry_auth: bool = True):
        """HTTP request on the shared session; re-auth once on 401/403."""
        if not self._ensure_auth():
            return None
        try:
            if method == "GET":
                response = self.get(path, params=params)
            else:
                response = self.post(path, data)
        except Exception:
            return None

        if response.status_code in (401, 403):
            self.isAuth = False
            if not retry_auth or not self.auth():
                return None
            try:
                if method == "GET":
                    response = self.get(path, params=params)
                else:
                    response = self.post(path, data)
            except Exception:
                return None
            if response.status_code in (401, 403):
                self.isAuth = False
                return None

        return response

    def _json_get(self, path: str) -> Optional[Any]:
        response = self._request("GET", path)
        if response is None or not response.ok:
            return None
        try:
            return loads(response.text) if response.text else {}
        except (JSONDecodeError, ValueError, TypeError):
            return None

    def _json_post(self, path: str, data: Any) -> Optional[Any]:
        response = self._request("POST", path, data=data)
        if response is None or not response.ok:
            return None
        try:
            if not response.text:
                return {}
            return loads(response.text)
        except (JSONDecodeError, ValueError, TypeError):
            return None

    @staticmethod
    def _usable_show_payload(data: Any) -> bool:
        """True if RCI show payload looks like real data (not pure error).

        Empty list ``[]`` is usable (e.g. no VPN tunnels). Empty dict ``{}`` is
        not — callers that need “key present but idle” must check key presence.
        """
        if data is None:
            return False
        if isinstance(data, list):
            return True
        if not isinstance(data, dict) or not data:
            return False
        if set(data.keys()) <= {"status", "message", "code"}:
            status = data.get("status")
            if isinstance(status, list):
                return False
            if status in ("error", "warning"):
                return False
        return True

    @staticmethod
    def _looks_like_not_found(payload: Any) -> bool:
        """True when RCI says the feature/path does not exist on this firmware."""
        if not isinstance(payload, dict):
            return False

        def _msg_not_found(msg: Any) -> bool:
            text = str(msg or "").lower()
            return "not found" in text or "unknown command" in text

        status = payload.get("status")
        if status == "error" and _msg_not_found(payload.get("message")):
            return True
        if isinstance(status, list):
            for item in status:
                if not isinstance(item, dict):
                    continue
                if item.get("status") == "error" and _msg_not_found(item.get("message")):
                    return True
        return False

    @staticmethod
    def _root_error_mentions(payload: Any, name: str) -> bool:
        """True when root RCI status reports a not-found error for this show name."""
        if not isinstance(payload, dict) or not name:
            return False
        needle = str(name)
        status = payload.get("status")
        if isinstance(status, list):
            for item in status:
                if not isinstance(item, dict) or item.get("status") != "error":
                    continue
                msg = str(item.get("message") or "")
                if needle in msg and ApiRouter._looks_like_not_found(item):
                    return True
        elif isinstance(status, dict) and status.get("status") == "error":
            msg = str(status.get("message") or "")
            if needle in msg and ApiRouter._looks_like_not_found(status):
                return True
        elif payload.get("status") == "error":
            msg = str(payload.get("message") or "")
            if needle in msg and ApiRouter._looks_like_not_found(payload):
                return True
        return False

    def _remember_missing_show(self, name: str):
        key = str(name or "").strip()
        if key:
            self._missing_show.add(key)

    def _ingest_show_feature(self, name: str, value: Any, *, requested: bool = False):
        """Update missing-cache from one show.* result (present or not-found)."""
        if self._looks_like_not_found(value):
            self._remember_missing_show(name)
            return
        if value is None:
            # Absent key in a batched show is not proof the feature is missing
            return
        # Any non-error payload (including {} / []) means the feature exists.
        # Idle PPTP often returns {} when no tunnels — must not blacklist.
        self._missing_show.discard(name)

    def _json_get_cached(self, path: str) -> Optional[Any]:
        if path in self._missing_paths:
            return None
        data = self._json_get(path)
        if self._looks_like_not_found(data):
            self._missing_paths.add(path)
            return None
        if data is None:
            # Transient failure — do not permanently blacklist
            return None
        # Empty list is valid (no tunnels). Empty error-shell dict → missing.
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and (
            not data or set(data.keys()) <= {"status", "message", "code", "ident"}
        ):
            if not data:
                # Idle endpoint may return {}; keep path usable
                return data
            if set(data.keys()) <= {"status", "message", "code", "ident"}:
                self._missing_paths.add(path)
                return None
        if not self._usable_show_payload(data):
            self._missing_paths.add(path)
            return None
        return data

    def _probe_show_feature(self, name: str, status_paths: Optional[List[str]] = None) -> Optional[Any]:
        """Probe one show feature at most once per session; cache absences."""
        if name in self._missing_show:
            return None
        if name in self._vpn_probed_show:
            return None
        self._vpn_probed_show.add(name)
        result = self._json_post("/rci/", {"show": {name: {}}})
        value = None
        if isinstance(result, dict):
            show = result.get("show") if isinstance(result.get("show"), dict) else result
            if isinstance(show, dict) and name in show:
                value = show.get(name)
            elif self._usable_show_payload(result) and "show" not in result:
                value = result
        self._ingest_show_feature(name, value if value is not None else result)
        if name in self._missing_show:
            return None
        if self._usable_show_payload(value):
            return value
        for path in status_paths or []:
            data = self._json_get_cached(path)
            if data is not None:
                self._missing_show.discard(name)
                return data
        self._remember_missing_show(name)
        return None

    @staticmethod
    def parse_system_resources(system_data: Any) -> Dict[str, Any]:
        system_data = system_data if isinstance(system_data, dict) else {}
        cpu_value = system_data.get("cpuload")
        if isinstance(cpu_value, str):
            try:
                cpu_value = int(float(cpu_value))
            except ValueError:
                cpu_value = None
        elif isinstance(cpu_value, (int, float)):
            cpu_value = int(cpu_value)
        else:
            cpu_value = None

        ram_value = None
        memory_value = system_data.get("memory")
        if isinstance(memory_value, str) and "/" in memory_value:
            used_value, total_value = memory_value.split("/", 1)
            try:
                used_value = int(used_value)
                total_value = int(total_value)
                if total_value > 0:
                    ram_value = int((used_value * 100) / total_value)
            except ValueError:
                ram_value = None
        elif isinstance(memory_value, (int, float)):
            ram_value = int(memory_value)

        uptime_value = system_data.get("uptime")
        if isinstance(uptime_value, str):
            try:
                uptime_value = int(float(uptime_value))
            except ValueError:
                uptime_value = None
        return {
            "cpu": cpu_value,
            "ram": ram_value,
            "uptime": uptime_value,
        }

    @property
    def devices(self):
        data = self._json_get("/rci/show/ip/hotspot")
        return self.hosts_from_hotspot(data)

    @staticmethod
    def hosts_from_hotspot(hotspot: Any) -> List:
        """Parse ConnectedDevice list from show ip hotspot payload."""
        if not isinstance(hotspot, dict):
            return []
        hosts = hotspot.get("host") or []
        if not isinstance(hosts, list):
            return []
        return list(map(ConnectedDevice, hosts))

    @staticmethod
    def hosts_from_info(info: Any) -> Optional[List]:
        """Hosts from batched ``info`` show (None = hotspot missing, use fallback GET)."""
        if not isinstance(info, dict):
            return None
        show = info.get("show") if isinstance(info.get("show"), dict) else {}
        ip_block = show.get("ip") if isinstance(show.get("ip"), dict) else {}
        if "hotspot" not in ip_block:
            return None
        return ApiRouter.hosts_from_hotspot(ip_block.get("hotspot"))

    @property
    def connected_devices(self):
        return list(filter(lambda device: device.active, self.devices))

    def info(self, include_vpn: bool = True):
        show_block: Dict[str, Any] = {
            "system": {},
            "version": {},
            "ip": {"hotspot": {}},
            "internet": {"status": {}},
            "interface": {},
        }
        # Only request VPN-server shows when explicitly wanted and not
        # known-missing / known-silenced on this session.
        if include_vpn:
            for name in ("vpn-server", "sstp-server", "l2tp-server", "wireguard-server"):
                if name not in self._missing_show:
                    show_block[name] = {}
        result = self._json_post("/rci/", {"show": show_block})
        if isinstance(result, dict):
            show = result.get("show") if isinstance(result.get("show"), dict) else {}
            if not isinstance(show, dict):
                show = {}
            if include_vpn:
                for name in ("vpn-server", "sstp-server", "l2tp-server", "wireguard-server"):
                    if name in show_block:
                        self._ingest_show_feature(name, show.get(name), requested=True)
                # A name we asked for may be absent from `show` while the root
                # `status` list reports "not found" for that section. Treat that
                # as missing so it is not re-requested every poll.
                for name in ("vpn-server", "sstp-server", "l2tp-server", "wireguard-server"):
                    if (
                        name in show_block
                        and name not in show
                        and name not in self._missing_show
                        and self._root_error_mentions(result, name)
                    ):
                        self._remember_missing_show(name)
        return result

    def interface_stat(self, interface_name):
        data = self._json_post(
            "/rci/",
            [{"show": {"interface": {"stat": [{"name": interface_name}]}}}],
        )
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return {}
        stat = data.get("show", {}).get("interface", {}).get("stat")
        if isinstance(stat, list) and len(stat) > 0 and isinstance(stat[0], dict):
            return stat[0]
        if isinstance(stat, dict):
            return stat
        return {}

    def system_resources(self, system_data: Any = None) -> Dict[str, Any]:
        if system_data is None:
            data = self._json_post("/rci/", {"show": {"system": {}}})
            if isinstance(data, list):
                data = data[0] if data else {}
            if isinstance(data, dict):
                system_data = (data.get("show") or {}).get("system")
        return self.parse_system_resources(system_data)

    def show_version(self) -> Dict[str, Any]:
        data = self._json_get("/rci/show/version")
        return data if isinstance(data, dict) else {}

    def show_system_update(self) -> Dict[str, Any]:
        data = self._json_get("/rci/show/system/update")
        return data if isinstance(data, dict) else {}

    def show_interfaces(self) -> Dict[str, Any]:
        data = self._json_get("/rci/show/interface")
        return data if isinstance(data, dict) else {}

    def show_vpn_server(self) -> Dict[str, Any]:
        data = self._json_get("/rci/show/vpn-server")
        return data if isinstance(data, dict) and self._usable_show_payload(data) else {}

    def show_vpn_server_clients(self) -> Any:
        return self._json_get("/rci/show/vpn-server/tunnel")

    def _first_json_get(self, paths: List[str]) -> Any:
        for path in paths:
            data = self._json_get_cached(path)
            if data is not None:
                return data
        return None

    def _show_from_dict(self, show: Optional[dict], *keys: str) -> Any:
        cur: Any = show or {}
        for key in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur if self._usable_show_payload(cur) else None

    def show_vpn_servers(self, show: Optional[dict] = None) -> List[Dict[str, Any]]:
        """Discover built-in VPN servers (PPTP/SSTP/L2TP/WireGuard).

        Reuses ``show`` from the main info poll when possible. Missing optional
        features are remembered per HTTP session and are not re-probed every poll
        (avoids flooding router syslog with RCI \"not found\" errors).
        """
        specs = [
            {
                "key": "pptp-server",
                "vpn_type": "PPTP",
                "title": "PPTP VPN Server",
                "show_name": "vpn-server",
                "status_paths": ["/rci/show/vpn-server"],
                "client_paths": [
                    "/rci/show/vpn-server/tunnel",
                    "/rci/show/vpn-server/clients",
                    "/rci/show/vpn-server/session",
                    "/rci/show/vpn-server/sessions",
                ],
                "client_keys": ("tunnel", "clients", "session", "sessions", "client"),
            },
            {
                "key": "sstp-server",
                "vpn_type": "SSTP",
                "title": "SSTP VPN Server",
                "show_name": "sstp-server",
                "status_paths": ["/rci/show/sstp-server"],
                "client_paths": [
                    "/rci/show/sstp-server/tunnel",
                    "/rci/show/sstp-server/session",
                    "/rci/show/sstp-server/sessions",
                    "/rci/show/sstp-server/clients",
                    "/rci/show/sstp-server/client",
                ],
                "client_keys": ("tunnel", "session", "sessions", "clients", "client"),
            },
            {
                "key": "l2tp-server",
                "vpn_type": "L2TP",
                "title": "L2TP/IPsec VPN Server",
                "show_name": "l2tp-server",
                "status_paths": [
                    "/rci/show/l2tp-server",
                    "/rci/show/ipsec/l2tp",
                ],
                "client_paths": [
                    "/rci/show/l2tp-server/session",
                    "/rci/show/l2tp-server/sessions",
                    "/rci/show/l2tp-server/clients",
                ],
                "client_keys": ("tunnel", "session", "sessions", "clients"),
            },
            {
                "key": "wireguard-server",
                "vpn_type": "WireGuard",
                "title": "WireGuard VPN Server",
                "show_name": "wireguard-server",
                "status_paths": [
                    "/rci/show/wireguard-server",
                    "/rci/show/interface/WireguardServer0",
                ],
                "client_paths": [
                    "/rci/show/wireguard-server/peer",
                    "/rci/show/wireguard-server/peers",
                ],
                "client_keys": ("tunnel", "peer", "peers", "session"),
            },
        ]

        show_data: Dict[str, Any] = {}
        if isinstance(show, dict):
            show_data.update(show)
            for name in ("vpn-server", "sstp-server", "l2tp-server", "wireguard-server"):
                if name in show_data:
                    self._ingest_show_feature(name, show_data.get(name), requested=False)

        rc = show_data.get("rc") if isinstance(show_data.get("rc"), dict) else {}
        result: List[Dict[str, Any]] = []
        for spec in specs:
            name = spec["show_name"]
            if name in self._missing_show:
                continue

            status = None
            if name in show_data:
                raw = show_data.get(name)
                # Key present in batched show — keep even if {} (idle PPTP/SSTP)
                if self._looks_like_not_found(raw):
                    self._remember_missing_show(name)
                    continue
                status = raw if isinstance(raw, dict) else {}
            if status is None:
                status = self._show_from_dict(rc, name)
            if status is None:
                # One probe per session for features not present in the info payload
                status = self._probe_show_feature(name, spec.get("status_paths") or [])
            if name in self._missing_show:
                continue
            if status is None:
                continue
            if not isinstance(status, dict):
                status = {}

            clients = None
            if isinstance(status, dict):
                for ck in spec["client_keys"]:
                    if ck not in status:
                        continue
                    nested = status.get(ck)
                    # Explicit tunnel/session key (even []) wins over probing
                    if nested is None:
                        clients = []
                        break
                    if isinstance(nested, list) or self._usable_show_payload(nested):
                        clients = nested
                        break
            if clients is None:
                for ck in spec["client_keys"]:
                    nested = self._show_from_dict(show_data, name, ck)
                    if nested is not None:
                        clients = nested
                        break
            if clients is None:
                # Already in the main info batch — do not re-GET /tunnel every poll
                if name in show_data:
                    clients = []
                else:
                    clients = self._first_json_get(spec["client_paths"])
                    if clients is None and any(
                        p in self._missing_paths for p in (spec.get("client_paths") or [])
                    ):
                        clients = []

            vpn_type = spec["vpn_type"]
            title = spec["title"]
            if isinstance(status, dict):
                raw_type = str(status.get("type") or "").strip()
                if raw_type:
                    vpn_type = raw_type.upper() if len(raw_type) <= 8 else raw_type
                    if vpn_type.upper() == "PPTP":
                        title = "PPTP VPN Server"
                    elif vpn_type.upper() == "SSTP":
                        title = "SSTP VPN Server"
                    elif "L2TP" in vpn_type.upper():
                        title = "L2TP/IPsec VPN Server"

            result.append(
                {
                    "key": spec["key"],
                    "vpn_type": vpn_type,
                    "title": title,
                    "status": status if isinstance(status, dict) else {},
                    "clients": clients,
                }
            )
        return result

    def reboot(self, interval: Optional[int] = None) -> bool:
        body: Dict[str, Any] = {}
        if interval is not None:
            body["interval"] = int(interval)
        return self._json_post("/rci/system/reboot", body) is not None

    def save_config(self) -> bool:
        result = self._json_post("/rci/", {"configuration": {"save": {}}})
        return result is not None

    def apply_update(self, channel: Optional[str] = None) -> bool:
        """Start firmware install.

        Modern KeeneticOS (draft/preview/stable via components) needs
        ``components list`` then ``components commit``. ``system update`` is
        a legacy fallback only when ``components commit`` is missing.

        On draft/preview, ``POST /rci/system/update`` often returns ``{}``
        without starting an install — do not treat that as success when commit
        is available.
        """
        ch = str(channel or "").strip().lower() or None
        # Refresh candidate list so commit has a prepared target
        self.components_list(channel=ch)
        result = self._json_post("/rci/components/commit", {})
        if result is not None and not self._rci_has_error(result):
            return True
        # Legacy path only if commit is absent / unreachable
        if result is None or self._looks_like_not_found(result):
            legacy = self._json_post("/rci/system/update", {})
            return legacy is not None and not self._rci_has_error(legacy)
        return False

    def wake_host(self, mac: str) -> bool:
        mac_value = str(mac or "").strip()
        if not mac_value:
            return False
        result = self._json_post("/rci/", {"ip": {"hotspot": {"wake": {"mac": mac_value}}}})
        return result is not None

    def set_host_access(self, mac: str, access: str) -> bool:
        mac_value = str(mac or "").strip().lower()
        access_value = str(access or "").strip().lower()
        if not mac_value or access_value not in ("permit", "deny"):
            return False
        host = {"mac": mac_value, access_value: True}
        result = self._json_post("/rci/", {"ip": {"hotspot": {"host": host}}})
        return result is not None

    def set_host_name(self, mac: str, name: str) -> bool:
        mac_value = str(mac or "").strip().lower()
        name_value = str(name or "").strip()
        if not mac_value or not name_value:
            return False
        result = self._json_post(
            "/rci/",
            {"ip": {"hotspot": {"host": {"mac": mac_value, "name": name_value}}}},
        )
        return result is not None

    def set_host_policy(self, mac: str, policy: str) -> bool:
        mac_value = str(mac or "").strip().lower()
        if not mac_value:
            return False
        host: Dict[str, Any] = {"mac": mac_value, "policy": str(policy or "")}
        result = self._json_post("/rci/", {"ip": {"hotspot": {"host": host}}})
        return result is not None

    def components_list(self, timeout: float = 60.0, channel: Optional[str] = None) -> Dict[str, Any]:
        """Run Keenetic ``components list`` (may be async via ``continued``).

        This is the reliable way to learn the candidate firmware on draft/preview
        channels: response has ``firmware`` (remote) and ``local`` (installed).

        Optional ``channel`` maps to CLI ``components list <channel>``
        (stable / preview / draft / release).
        """
        body: Dict[str, Any] = {}
        ch = str(channel or "").strip().lower()
        if ch in ("stable", "preview", "draft", "release"):
            body[ch] = True
        data = self._json_post("/rci/components/list", body)
        if not isinstance(data, dict):
            data = {}
        deadline = time.time() + max(1.0, float(timeout or 60.0))
        while isinstance(data, dict) and data.get("continued") and time.time() < deadline:
            time.sleep(1.0)
            nxt = self._json_get("/rci/components/list")
            if isinstance(nxt, dict):
                data = nxt
            else:
                break
        return data if isinstance(data, dict) and not data.get("continued") else (
            data if isinstance(data, dict) else {}
        )

    def check_firmware(self) -> Dict[str, Any]:
        """Detect firmware update via components list (+ version/update fallbacks).

        On Alpha/draft builds ``show/system/update`` is often empty and
        ``show/version`` has no ``fw-available``. The web UI uses
        ``POST /rci/components/list``: compare ``firmware.version`` to ``local.version``.
        """
        components = self.components_list()
        if not isinstance(components, dict):
            components = {}

        remote = components.get("firmware") if isinstance(components.get("firmware"), dict) else {}
        local = components.get("local") if isinstance(components.get("local"), dict) else {}
        current = str(local.get("version") or local.get("release") or "").strip()
        update_release = str(remote.get("version") or remote.get("release") or "").strip()
        update_title = str(remote.get("title") or "").strip()
        channel = str(components.get("sandbox") or "").strip()
        model = None
        firmware_title = str(local.get("title") or "").strip()

        # Only hit show/version when components list did not yield versions
        version: Dict[str, Any] = {}
        if not current or not update_release:
            version = self.show_version()
            if not isinstance(version, dict):
                version = {}
            if not current:
                current = str(version.get("release") or version.get("title") or "").strip()
            if not firmware_title:
                firmware_title = str(version.get("title") or current).strip()
            if not channel:
                channel = str(
                    version.get("fw-update-sandbox")
                    or version.get("fw_update_sandbox")
                    or version.get("sandbox")
                    or ""
                ).strip()
            if not update_release:
                update_release = str(
                    version.get("fw-available")
                    or version.get("release-available")
                    or version.get("fw_available")
                    or version.get("release_available")
                    or ""
                ).strip()
            model = version.get("model") or version.get("device")

        if not update_release:
            update = self.show_system_update()
            if isinstance(update, dict) and update:
                update_release = str(
                    update.get("release")
                    or update.get("version")
                    or update.get("title")
                    or update.get("fw-available")
                    or ""
                ).strip()
                channel = str(
                    update.get("channel") or update.get("component") or channel or ""
                ).strip()
                if update.get("available") in (True, 1, "1", "true", "yes") and not update_release:
                    update_release = str(update.get("new") or update.get("target") or "").strip()

        if not model and version:
            model = version.get("model") or version.get("device")

        available = bool(update_release) and (
            (bool(current) and update_release != current) or (not current)
        )
        return {
            "firmware_version": current,
            "firmware_title": firmware_title or update_title or current,
            "model": model,
            "update_available": available,
            "update_version": update_release if available else "",
            "update_channel": channel,
        }

    @staticmethod
    def vpn_service_name(key: str) -> Optional[str]:
        """Map plugin VPN key to Keenetic RCI service name for built-in servers."""
        mapping = {
            "pptp-server": "vpn-server",
            "vpn-server": "vpn-server",
            "sstp-server": "sstp-server",
            "l2tp-server": "l2tp-server",
            "wireguard-server": "wireguard-server",
        }
        return mapping.get(str(key or "").strip())

    @staticmethod
    def _rci_has_error(payload: Any) -> bool:
        if payload is None:
            return True
        if isinstance(payload, list):
            return any(ApiRouter._rci_has_error(item) for item in payload)
        if not isinstance(payload, dict):
            return False
        status = payload.get("status")
        if isinstance(status, list):
            for item in status:
                if isinstance(item, dict) and item.get("status") == "error":
                    return True
        return any(ApiRouter._rci_has_error(v) for v in payload.values() if isinstance(v, (dict, list)))

    def set_vpn_server_enabled(self, key: str, enabled: bool) -> bool:
        service = self.vpn_service_name(key)
        if not service:
            return False
        result = self._json_post("/rci/", {service: {"enable": bool(enabled)}})
        if result is None or self._rci_has_error(result):
            return False
        return True

    def set_interface_state(self, interface: str, up: bool = True) -> bool:
        name = str(interface or "").strip()
        if not name:
            return False
        action = {"up": True} if up else {"down": True}
        result = self._json_post("/rci/", {"interface": {name: action}})
        if result is None or self._rci_has_error(result):
            return False
        return True

    def vpn_logout_session(self, key: str, session_id) -> bool:
        """Disconnect one VPN server client session (Keenetic session-logout)."""
        service = self.vpn_service_name(key)
        if not service:
            return False
        sid = str(session_id).strip()
        if not sid:
            return False
        # Prefer int when numeric — Keenetic accepts both, UI uses string
        session_value: Any = int(sid) if sid.isdigit() else sid
        result = self._json_post(
            "/rci/",
            {service: {"session-logout": {"session": session_value}}},
        )
        if result is None:
            return False
        # "session is not found" counts as failure
        if self._rci_has_error(result):
            return False
        return True

    def get_log(self, limit: int = 100) -> Optional[Any]:
        """Snapshot of system journal (show log). Prefer batch POST, fallback GET."""
        lim = max(1, min(int(limit or 100), 500))
        result = self._json_post("/rci/", {"show": {"log": {"limit": lim}}})
        if result is not None:
            show = result.get("show") if isinstance(result, dict) else None
            if isinstance(show, dict) and "log" in show:
                return show.get("log")
            if isinstance(result, dict) and ("log" in result or self._usable_show_payload(result)):
                return result.get("log", result)
        return self._json_get("/rci/show/log")

    def vpn_connect(self, interface: str) -> bool:
        return self.set_interface_state(interface, up=True)

    def vpn_disconnect(self, interface: str) -> bool:
        return self.set_interface_state(interface, up=False)
