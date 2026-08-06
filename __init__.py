"""Keenetic plugin: polling, cache, VPN, firmware, control."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import redirect, render_template, request
from sqlalchemy import delete, or_

from app.core.lib.common import CategoryNotify, addNotify
from app.core.lib.object import callMethodThread, updatePropertyThread
from app.core.main.BasePlugin import BasePlugin
from app.database import get_now_to_utc, row2dict, session_scope
from app.logging_config import getLogger
from plugins.Keenetic.forms.DeviceForm import DeviceForm
from plugins.Keenetic.forms.LogRuleForm import LogRuleForm
from plugins.Keenetic.forms.RouterForm import RouterForm
from plugins.Keenetic.forms.SettingForms import SettingsForm
from plugins.Keenetic.forms.VpnForm import VpnForm
from plugins.Keenetic.helpers import (
    DEVICE_SYNC_DEFAULT,
    INTERNET_SYNC_DEFAULT,
    DEFAULT_JOURNAL_BUFFER_LIMIT,
    LOG_POLL_LIMIT,
    ROUTER_SYNC_DEFAULT,
    VPN_CLIENT_SYNC_DEFAULT,
    VPN_SERVER_SYNC_DEFAULT,
    compile_log_rule_pattern,
    default_device_icon,
    default_router_icon,
    default_vpn_icon,
    live_device_fields,
    map_keenetic_log_level,
    match_log_rule,
    normalize_log_entries,
    normalize_mac,
    parse_vpn_interfaces,
    parse_vpn_server,
    resolve_sync_live,
    sessions_by_identity,
    stable_device_fields,
)
from plugins.Keenetic.keenetic import ApiRouter
from plugins.Keenetic.models.Device import KeeneticDevice
from plugins.Keenetic.models.LogRule import KeeneticLogRule
from plugins.Keenetic.models.Router import Router
from plugins.Keenetic.models.Vpn import KeeneticVpn


class Keenetic(BasePlugin):

    def __init__(self, app):
        self._cache_lock = threading.RLock()
        self.router_runtime: Dict[int, dict] = {}
        super().__init__(app, __name__)
        self.title = "Keenetic"
        self.description = """Keenetic: get devices info"""
        self.system = True
        self.actions = ["cycle", "search", "widget"]
        self.category = "Devices"
        self.version = "0.14"
        self.routers = {}
        self._processing_routers = set()
        self._processing_lock = threading.Lock()
        self.routers_cache: Dict[int, dict] = {}
        self.devices_cache: Dict[int, dict] = {}
        self.devices_by_router_mac: Dict[tuple, int] = {}
        self.devices_by_router: Dict[int, set] = {}
        self.vpns_cache: Dict[int, dict] = {}
        self.vpns_by_router_key: Dict[tuple, int] = {}
        self.device_runtime: Dict[int, dict] = {}
        self.vpn_runtime: Dict[int, dict] = {}
        self.log_rules_cache: Dict[int, dict] = {}
        self.log_rules_by_router: Dict[int, set] = {}
        self._last_firmware_check = 0.0
        self._journal_loggers: Dict[int, Any] = {}

    def initialization(self):
        self._load_entity_cache()

    def loadConfig(self):
        super().loadConfig()
        self._trim_all_journal_buffers()

    def _get_journal_logger(self, router_id: int):
        """Per-router rotating file logger: logs/KeeneticJournal_<id>.log."""
        rid = int(router_id)
        logger = self._journal_loggers.get(rid)
        if logger is None:
            logger = getLogger(f"KeeneticJournal_{rid}")
            self._journal_loggers[rid] = logger
        return logger

    def _journal_buffer_limit(self) -> int:
        try:
            value = int(self.config.get("journal_buffer_limit", DEFAULT_JOURNAL_BUFFER_LIMIT))
        except (TypeError, ValueError):
            value = DEFAULT_JOURNAL_BUFFER_LIMIT
        return max(10, min(value, 5000))

    def _trim_all_journal_buffers(self):
        limit = self._journal_buffer_limit()
        with self._cache_lock:
            for rt in self.router_runtime.values():
                buffer = list(rt.get("log_entries") or [])
                if len(buffer) > limit:
                    rt["log_entries"] = buffer[-limit:]
                seen = rt.get("log_seen")
                if isinstance(seen, set) and len(seen) > limit * 3:
                    keep = {str(e.get("id")) for e in rt.get("log_entries") or [] if e.get("id")}
                    rt["log_seen"] = keep

    @staticmethod
    def _journal_entry_mcp_id(router_id: int, log_id: str) -> str:
        return f"r{int(router_id)}#{log_id}"

    @staticmethod
    def _parse_journal_mcp_id(entity_id) -> tuple:
        text = str(entity_id or "")
        if "#" not in text or not text.startswith("r"):
            raise ValueError(f"Invalid journal entity id: {entity_id}")
        router_part, log_id = text.split("#", 1)
        try:
            router_id = int(router_part[1:])
        except (TypeError, ValueError) as ex:
            raise ValueError(f"Invalid journal entity id: {entity_id}") from ex
        if not log_id:
            raise ValueError(f"Invalid journal entity id: {entity_id}")
        return router_id, log_id

    def _journal_entry_to_mcp(self, router_id: int, entry: dict) -> dict:
        log_id = str(entry.get("id") or "")
        return {
            "id": self._journal_entry_mcp_id(router_id, log_id),
            "router_id": int(router_id),
            "log_id": log_id,
            "time": entry.get("time") or "",
            "level": entry.get("level") or "",
            "label": entry.get("label") or "",
            "facility": entry.get("facility") or "",
            "message": entry.get("message") or "",
            "repeated": entry.get("repeated") or 0,
        }

    def list_journal_entries(
        self,
        router_id: Optional[int] = None,
        query: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        limit = max(1, min(int(limit or 100), 5000))
        q = (query or "").strip().lower()
        result: List[dict] = []
        with self._cache_lock:
            if router_id not in (None, ""):
                router_ids = [int(router_id)]
            else:
                router_ids = sorted(int(rid) for rid in self.router_runtime.keys())
            for rid in router_ids:
                rt = self.router_runtime.get(int(rid), {})
                buffer = list(rt.get("log_entries") or [])
                for entry in reversed(buffer):
                    item = self._journal_entry_to_mcp(int(rid), entry)
                    if q:
                        hay = " ".join(
                            str(v or "")
                            for k, v in item.items()
                            if k not in ("id", "router_id", "log_id")
                        ).lower()
                        if q not in hay:
                            continue
                    result.append(item)
                    if len(result) >= limit:
                        return result
        return result

    def get_journal_entry(self, entity_id) -> dict:
        router_id, log_id = self._parse_journal_mcp_id(entity_id)
        with self._cache_lock:
            rt = self.router_runtime.get(int(router_id), {})
            for entry in rt.get("log_entries") or []:
                if str(entry.get("id") or "") == log_id:
                    return self._journal_entry_to_mcp(int(router_id), entry)
        raise ValueError(f"Journal entry not found: {entity_id}")

    @staticmethod
    def _journal_log_level(level: Any, label: Any = "") -> int:
        mapped = map_keenetic_log_level(level, label)
        if mapped == "error":
            return logging.ERROR
        if mapped == "warning":
            return logging.WARNING
        if mapped == "debug":
            return logging.DEBUG
        # notice/info → INFO (Python has no NOTICE)
        return logging.INFO

    def _write_journal_file(self, router: dict, entry: dict):
        if not router.get("log_to_file"):
            return
        router_id = router.get("id")
        if not router_id:
            return
        title = router.get("title") or router.get("ip") or router_id or "?"
        time_v = entry.get("time") or ""
        level = entry.get("level") or ""
        label = entry.get("label") or ""
        facility = entry.get("facility") or ""
        message = entry.get("message") or ""
        repeated = entry.get("repeated")
        parts = [f"[{title}]"]
        if time_v:
            parts.append(str(time_v))
        if level:
            parts.append(str(level))
        if facility:
            parts.append(str(facility))
        if repeated not in (None, "", 0, "0"):
            parts.append(f"(x{repeated})")
        parts.append(str(message))
        self._get_journal_logger(int(router_id)).log(
            self._journal_log_level(level, label),
            " ".join(parts),
        )

    # --- cache ---

    def _row_to_cache(self, row) -> dict:
        data = row2dict(row)
        # Volatile live fields live only in router_runtime / device_runtime / vpn_runtime
        for key in ("online", "updated"):
            data.pop(key, None)
        for key, value in list(data.items()):
            if isinstance(value, datetime):
                data[key] = value.isoformat(sep=" ", timespec="seconds")
        return data

    def _load_entity_cache(self):
        with session_scope() as session:
            routers = session.query(Router).all()
            devices = session.query(KeeneticDevice).all()
            vpns = session.query(KeeneticVpn).all()
            log_rules = session.query(KeeneticLogRule).all()
            routers_data = [self._row_to_cache(row) for row in routers]
            devices_data = [self._row_to_cache(row) for row in devices]
            vpns_data = [self._row_to_cache(row) for row in vpns]
            log_rules_data = [self._row_to_cache(row) for row in log_rules]
        with self._cache_lock:
            self.routers_cache = {int(row["id"]): row for row in routers_data}
            self.devices_cache = {int(row["id"]): row for row in devices_data}
            self.vpns_cache = {int(row["id"]): row for row in vpns_data}
            self.log_rules_cache = {int(row["id"]): row for row in log_rules_data}
            self.devices_by_router_mac = {}
            self.devices_by_router = {}
            for device_id, row in self.devices_cache.items():
                router_id = int(row.get("router_id") or 0)
                mac = normalize_mac(row.get("mac"))
                self.devices_by_router.setdefault(router_id, set()).add(device_id)
                if mac:
                    self.devices_by_router_mac[(router_id, mac)] = device_id
            self.vpns_by_router_key = {}
            for vpn_id, row in self.vpns_cache.items():
                router_id = int(row.get("router_id") or 0)
                key = str(row.get("key") or "").strip()
                if key:
                    self.vpns_by_router_key[(router_id, key)] = vpn_id
            self.log_rules_by_router = {}
            for rule_id, row in self.log_rules_cache.items():
                router_id = int(row.get("router_id") or 0)
                self.log_rules_by_router.setdefault(router_id, set()).add(rule_id)

    def _cache_upsert_log_rule(self, data: dict):
        rule_id = int(data["id"])
        router_id = int(data.get("router_id") or 0)
        with self._cache_lock:
            old = self.log_rules_cache.get(rule_id)
            if old:
                old_router = int(old.get("router_id") or 0)
                self.log_rules_by_router.get(old_router, set()).discard(rule_id)
            self.log_rules_cache[rule_id] = dict(data)
            self.log_rules_by_router.setdefault(router_id, set()).add(rule_id)

    def _cache_delete_log_rule_unlocked(self, rule_id: int):
        old = self.log_rules_cache.pop(rule_id, None)
        if not old:
            return
        router_id = int(old.get("router_id") or 0)
        self.log_rules_by_router.get(router_id, set()).discard(rule_id)

    def _cache_delete_log_rule(self, rule_id: int):
        with self._cache_lock:
            self._cache_delete_log_rule_unlocked(int(rule_id))

    def list_log_rules(self, router_id: Optional[int] = None) -> List[dict]:
        with self._cache_lock:
            if router_id is None:
                ids = list(self.log_rules_cache.keys())
            else:
                ids = list(self.log_rules_by_router.get(int(router_id), set()))
            return [dict(self.log_rules_cache[i]) for i in ids if i in self.log_rules_cache]

    def get_log_rule(self, rule_id: int) -> Optional[dict]:
        with self._cache_lock:
            row = self.log_rules_cache.get(int(rule_id))
            return dict(row) if row else None

    def _active_log_rules(self, router_id: int) -> List[dict]:
        rules = self.list_log_rules(int(router_id))
        return [r for r in rules if r.get("active")]

    def _apply_log_rules(self, router: dict, entry: dict):
        """Apply file-write / method rules for one new journal entry."""
        router_id = int(router.get("id") or 0)
        active = self._active_log_rules(router_id)
        log_to_file = bool(router.get("log_to_file"))

        if not active:
            # Legacy: write all + call router linked_method
            self._write_journal_file(router, entry)
            self._notify_log_entry(router, entry)
            return

        matched: List[dict] = []
        for rule in active:
            pattern = str(rule.get("pattern") or "")
            compiled, err = compile_log_rule_pattern(pattern)
            if compiled is None:
                if err:
                    self.logger.warning(
                        "Keenetic log rule %s invalid pattern %r: %s",
                        rule.get("id"),
                        pattern,
                        err,
                    )
                continue
            if match_log_rule(pattern, entry):
                matched.append(rule)

        if log_to_file and any(r.get("write_to_file") for r in matched):
            self._write_journal_file(router, entry)

        called_methods: set = set()
        for rule in matched:
            method = str(rule.get("linked_method") or "").strip()
            linked = str(rule.get("linked_object") or "").strip()
            if not linked or not method:
                continue
            key = (linked, method)
            if key in called_methods:
                continue
            called_methods.add(key)
            message = str(entry.get("message") or "")
            params = {
                "EVENT": "log",
                "VALUE": message,
                "NEW_VALUE": message,
                "MESSAGE": message,
                "LEVEL": str(entry.get("level") or ""),
                "TIME": str(entry.get("time") or ""),
                "FACILITY": str(entry.get("facility") or ""),
                "LABEL": str(entry.get("label") or ""),
                "LOG_ID": str(entry.get("id") or ""),
                "REPEATED": entry.get("repeated") or 0,
                "ROUTER_ID": router.get("id"),
                "ROUTER_TITLE": router.get("title") or router.get("ip") or "",
                "RULE_ID": rule.get("id"),
                "RULE_TITLE": rule.get("title") or "",
                "PATTERN": str(rule.get("pattern") or ""),
                "SOURCE": self.name,
            }
            callMethodThread(f"{linked}.{method}", params, self.name)

    def _cache_upsert_router(self, data: dict):
        router_id = int(data["id"])
        with self._cache_lock:
            self.routers_cache[router_id] = dict(data)

    def _cache_delete_router(self, router_id: int):
        router_id = int(router_id)
        with self._cache_lock:
            old = self.routers_cache.get(router_id)
            self.routers_cache.pop(router_id, None)
            device_ids = list(self.devices_by_router.get(router_id, set()))
            for device_id in device_ids:
                self._cache_delete_device_unlocked(device_id)
            vpn_ids = [
                vpn_id
                for (rid, _key), vpn_id in list(self.vpns_by_router_key.items())
                if rid == router_id
            ]
            for vpn_id in vpn_ids:
                self._cache_delete_vpn_unlocked(vpn_id)
            rule_ids = list(self.log_rules_by_router.get(router_id, set()))
            for rule_id in rule_ids:
                self._cache_delete_log_rule_unlocked(rule_id)
            self.router_runtime.pop(router_id, None)
        # Close cached RCI session outside lock
        api = self.routers.pop(router_id, None)
        if api is None and old:
            ip = str(old.get("ip") or "")
            port = int(old.get("port") or 80)
            api = self.routers.pop(ip, None) or self.routers.pop(f"{ip}:{port}", None)
        if api is not None:
            try:
                api.close()
            except Exception:
                pass

    def _cache_upsert_device(self, data: dict):
        device_id = int(data["id"])
        router_id = int(data.get("router_id") or 0)
        mac = normalize_mac(data.get("mac"))
        with self._cache_lock:
            old = self.devices_cache.get(device_id)
            if old:
                old_router = int(old.get("router_id") or 0)
                old_mac = normalize_mac(old.get("mac"))
                if old_mac:
                    self.devices_by_router_mac.pop((old_router, old_mac), None)
                self.devices_by_router.get(old_router, set()).discard(device_id)
            self.devices_cache[device_id] = dict(data)
            self.devices_by_router.setdefault(router_id, set()).add(device_id)
            if mac:
                self.devices_by_router_mac[(router_id, mac)] = device_id

    def _cache_delete_device_unlocked(self, device_id: int):
        old = self.devices_cache.pop(device_id, None)
        self.device_runtime.pop(device_id, None)
        if not old:
            return
        router_id = int(old.get("router_id") or 0)
        mac = normalize_mac(old.get("mac"))
        self.devices_by_router.get(router_id, set()).discard(device_id)
        if mac:
            self.devices_by_router_mac.pop((router_id, mac), None)

    def _cache_delete_device(self, device_id: int):
        with self._cache_lock:
            self._cache_delete_device_unlocked(int(device_id))

    def _cache_upsert_vpn(self, data: dict):
        vpn_id = int(data["id"])
        router_id = int(data.get("router_id") or 0)
        key = str(data.get("key") or "").strip()
        with self._cache_lock:
            old = self.vpns_cache.get(vpn_id)
            if old:
                old_key = str(old.get("key") or "").strip()
                old_router = int(old.get("router_id") or 0)
                if old_key:
                    self.vpns_by_router_key.pop((old_router, old_key), None)
            self.vpns_cache[vpn_id] = dict(data)
            if key:
                self.vpns_by_router_key[(router_id, key)] = vpn_id

    def _cache_delete_vpn_unlocked(self, vpn_id: int):
        old = self.vpns_cache.pop(vpn_id, None)
        self.vpn_runtime.pop(vpn_id, None)
        if not old:
            return
        router_id = int(old.get("router_id") or 0)
        key = str(old.get("key") or "").strip()
        if key:
            self.vpns_by_router_key.pop((router_id, key), None)

    def _cache_delete_vpn(self, vpn_id: int):
        with self._cache_lock:
            self._cache_delete_vpn_unlocked(int(vpn_id))

    def _get_api(self, router: dict) -> ApiRouter:
        router_id = int(router.get("id") or 0)
        ip = str(router.get("ip") or "").strip()
        port = int(router.get("port") or 80)
        login = router.get("login") or ""
        password = router.get("password") or ""
        if not router_id or not ip:
            # Ephemeral client (should not happen for persisted routers)
            return ApiRouter(login, password, ip, port)

        api = self.routers.get(router_id)
        if api is None:
            # Drop legacy ip / ip:port keys if any leftovers remain
            for legacy_key in (ip, f"{ip}:{port}"):
                self.routers.pop(legacy_key, None)
            api = ApiRouter(login, password, ip, port)
            self.routers[router_id] = api
        else:
            api.update_credentials(login, password, host=ip, port=port)
        if not api.isAuth:
            api.auth()
        return api

    # --- merge / sync ---

    def merge_router(self, router_id: int) -> Optional[dict]:
        with self._cache_lock:
            base = self.routers_cache.get(int(router_id))
            if not base:
                return None
            data = deepcopy(base)
            data.update(self.router_runtime.get(int(router_id), {}))
        # Hide runtime journal state from API/MCP (log_seen is a set)
        log_seen = data.pop("log_seen", None)
        if log_seen is not None:
            data["log_seen_count"] = len(log_seen) if hasattr(log_seen, "__len__") else None
        data.pop("log_entries", None)
        # Hide stale "update available" once installed version matches candidate
        current = str(data.get("firmware_version") or "").strip()
        pending = str(data.get("update_version") or "").strip()
        if data.get("update_available") and current and pending and current == pending:
            data["update_available"] = 0
            data["update_version"] = ""
        elif data.get("update_available") and not pending:
            data["update_available"] = 0
        return data

    def merge_device(self, device_id: int) -> Optional[dict]:
        with self._cache_lock:
            base = self.devices_cache.get(int(device_id))
            if not base:
                return None
            data = deepcopy(base)
            data.update(self.device_runtime.get(int(device_id), {}))
            return data

    def merge_vpn(self, vpn_id: int) -> Optional[dict]:
        with self._cache_lock:
            base = self.vpns_cache.get(int(vpn_id))
            if not base:
                return None
            data = deepcopy(base)
            data.update(self.vpn_runtime.get(int(vpn_id), {}))
            return data

    def list_merged_routers(self) -> List[dict]:
        with self._cache_lock:
            ids = list(self.routers_cache.keys())
        result = []
        for router_id in ids:
            item = self.merge_router(router_id)
            if item:
                result.append(item)
        return sorted(result, key=lambda row: (str(row.get("title") or ""), int(row.get("id") or 0)))

    def list_merged_devices(self, router_id: Optional[int] = None) -> List[dict]:
        with self._cache_lock:
            if router_id is None:
                ids = list(self.devices_cache.keys())
            else:
                ids = list(self.devices_by_router.get(int(router_id), set()))
        result = []
        for device_id in ids:
            item = self.merge_device(device_id)
            if item:
                result.append(item)
        return sorted(result, key=lambda row: (str(row.get("title") or ""), int(row.get("id") or 0)))

    def list_merged_vpns(self, router_id: Optional[int] = None) -> List[dict]:
        with self._cache_lock:
            if router_id is None:
                ids = list(self.vpns_cache.keys())
            else:
                ids = [
                    vpn_id
                    for (rid, _key), vpn_id in self.vpns_by_router_key.items()
                    if rid == int(router_id)
                ]
        result = []
        for vpn_id in ids:
            item = self.merge_vpn(vpn_id)
            if item:
                result.append(item)
        return sorted(result, key=lambda row: (str(row.get("title") or ""), int(row.get("id") or 0)))

    def _push_live(self, linked_object: str, props: Dict[str, Any], runtime: dict, sync_fields: set):
        linked = str(linked_object or "").strip()
        if not linked or not sync_fields:
            return
        last_pushed = runtime.setdefault("last_pushed", {})
        for name in sync_fields:
            if name not in props:
                continue
            value = props[name]
            if last_pushed.get(name) == value:
                continue
            updatePropertyThread(f"{linked}.{name}", value, self.name)
            last_pushed[name] = value

    @staticmethod
    def _format_bytes(value: Any) -> str:
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            return "0 B"
        if n < 1024:
            return f"{n} B"
        units = ["KB", "MB", "GB", "TB"]
        size = float(n)
        for unit in units:
            size /= 1024.0
            if size < 1024 or unit == units[-1]:
                if size >= 10 or unit == "KB":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
        return f"{n} B"

    def _notify_vpn_session(
        self,
        vpn: dict,
        runtime: dict,
        event: str,
        session: dict,
        sync_fields: set,
    ):
        linked = str(vpn.get("linked_object") or "").strip()
        method = str(vpn.get("linked_method") or "").strip()
        user = str(session.get("name") or "").strip()
        ip = str(session.get("address") or "").strip()
        rx = session.get("rxbytes") or 0
        tx = session.get("txbytes") or 0
        uptime = session.get("uptime")
        vpn_title = vpn.get("title") or vpn.get("key") or "VPN"
        vpn_type = vpn.get("vpn_type") or ""
        if event == "connect":
            value = f"{vpn_title}: {user or 'client'} connected"
            if ip:
                value += f" ({ip})"
        else:
            value = f"{vpn_title}: {user or 'client'} disconnected"
            if ip:
                value += f" ({ip})"
            value += f", ↓{self._format_bytes(rx)} ↑{self._format_bytes(tx)}"

        props = {
            "last_event": event,
            "last_user": user,
            "last_ip": ip,
            "last_rxbytes": rx if event == "disconnect" else 0,
            "last_txbytes": tx if event == "disconnect" else 0,
            "last_uptime": uptime if event == "disconnect" else 0,
        }
        if linked:
            self._push_live(linked, props, runtime, sync_fields)

        if linked and method:
            params = {
                "EVENT": event,
                "VALUE": value,
                "NEW_VALUE": value,
                "USER": user,
                "IP": ip,
                "REMOTE": str(session.get("remote") or "").strip(),
                "UPTIME": uptime,
                "RXBYTES": rx if event == "disconnect" else 0,
                "TXBYTES": tx if event == "disconnect" else 0,
                "VPN_KEY": vpn.get("key"),
                "VPN_TYPE": vpn_type,
                "VPN_TITLE": vpn_title,
                "SESSION_ID": session.get("session_id"),
                "SOURCE": self.name,
            }
            callMethodThread(f"{linked}.{method}", params, self.name)

    def _process_vpn_session_events(self, vpn: dict, runtime: dict, sessions: List[dict], sync_fields: set):
        # Only connected sessions participate in connect/disconnect tracking
        active = [s for s in (sessions or []) if isinstance(s, dict) and s.get("connected")]
        current = sessions_by_identity(active)
        previous = runtime.get("sessions_by_id")
        runtime["sessions_by_id"] = current
        # First observation after start/restart — remember baseline, don't flood connects
        if previous is None:
            return
        if not isinstance(previous, dict):
            previous = {}
        for key, session in current.items():
            if key not in previous:
                self._notify_vpn_session(vpn, runtime, "connect", session, sync_fields)
        for key, session in previous.items():
            if key not in current:
                self._notify_vpn_session(vpn, runtime, "disconnect", session, sync_fields)

    def _ws_notify(self, operation: str, entity_id: int, runtime: dict, live: dict, fields: tuple):
        """Push changed live fields to admin UI via wsServer (event name = plugin name)."""
        if not entity_id:
            return
        last_ws = runtime.setdefault("last_ws", {})
        payload: Dict[str, Any] = {"id": int(entity_id)}
        changed = False
        for name in fields:
            if name not in live:
                continue
            value = live[name]
            if last_ws.get(name) == value:
                continue
            payload[name] = value
            last_ws[name] = value
            changed = True
        if changed:
            self.sendDataToWebsocket(operation, payload)

    @staticmethod
    def _extract_interface_traffic(interface_data):
        if not isinstance(interface_data, dict):
            return 0, 0
        rx_candidates = ("rxbytes", "rx_bytes", "received", "received_bytes")
        tx_candidates = ("txbytes", "tx_bytes", "sent", "sent_bytes")

        def pick_value(candidates):
            for key in candidates:
                value = interface_data.get(key)
                if value is None:
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
            return 0

        return pick_value(rx_candidates), pick_value(tx_candidates)

    def _upsert_device_stable(self, router_id: int, mac: str, stable: dict) -> Optional[int]:
        mac_n = normalize_mac(mac)
        with self._cache_lock:
            device_id = self.devices_by_router_mac.get((router_id, mac_n))
            cached = self.devices_cache.get(device_id) if device_id else None

        need_insert = device_id is None
        need_update = False
        if cached:
            for field, value in stable.items():
                if field == "icon":
                    continue
                if field == "mac":
                    continue
                if (cached.get(field) or None) != (value or None):
                    need_update = True
                    break
            if not cached.get("icon") and stable.get("icon"):
                need_update = True
        if not need_insert and not need_update:
            return device_id

        with session_scope() as session:
            if need_insert:
                rec = (
                    session.query(KeeneticDevice)
                    .filter(
                        KeeneticDevice.router_id == router_id,
                        KeeneticDevice.mac == mac,
                    )
                    .one_or_none()
                )
                if not rec and stable.get("title"):
                    rec = (
                        session.query(KeeneticDevice)
                        .filter(
                            KeeneticDevice.router_id == router_id,
                            KeeneticDevice.title == stable.get("title"),
                        )
                        .first()
                    )
                    if rec:
                        rec.mac = mac
                if not rec:
                    rec = KeeneticDevice(router_id=router_id, mac=mac)
                    session.add(rec)
            else:
                rec = session.get(KeeneticDevice, device_id)
                if not rec:
                    return None
            for field, value in stable.items():
                if field == "icon":
                    continue
                setattr(rec, field, value)
            if not rec.icon:
                rec.icon = stable.get("icon") or default_device_icon(
                    stable.get("title") or "",
                    mac,
                    stable.get("device_hint") or "",
                    stable.get("ssid") or "",
                    stable.get("ap") or "",
                )
            session.commit()
            data = self._row_to_cache(rec)
        self._cache_upsert_device(data)
        return int(data["id"])

    def _upsert_vpn_stable(self, router_id: int, key: str, stable: dict) -> Optional[int]:
        key = str(key or "").strip()
        with self._cache_lock:
            vpn_id = self.vpns_by_router_key.get((router_id, key))
            cached = self.vpns_cache.get(vpn_id) if vpn_id else None

        need_insert = vpn_id is None
        need_update = False
        if cached:
            for field in ("title", "vpn_type", "role"):
                if (cached.get(field) or None) != (stable.get(field) or None):
                    need_update = True
                    break
            if not cached.get("icon"):
                need_update = True
        if not need_insert and not need_update:
            return vpn_id

        with session_scope() as session:
            if need_insert:
                rec = (
                    session.query(KeeneticVpn)
                    .filter(KeeneticVpn.router_id == router_id, KeeneticVpn.key == key)
                    .one_or_none()
                )
                if not rec:
                    rec = KeeneticVpn(router_id=router_id, key=key)
                    session.add(rec)
            else:
                rec = session.get(KeeneticVpn, vpn_id)
                if not rec:
                    return None
            rec.role = stable.get("role")
            rec.vpn_type = stable.get("vpn_type")
            rec.title = stable.get("title")
            if not rec.icon:
                rec.icon = stable.get("icon") or default_vpn_icon(stable.get("role") or "client")
            session.commit()
            data = self._row_to_cache(rec)
        self._cache_upsert_vpn(data)
        return int(data["id"])

    def _update_router_stable(self, router_id: int, fields: dict):
        with self._cache_lock:
            cached = self.routers_cache.get(router_id)
        if not cached:
            return
        changed = {}
        for key, value in fields.items():
            if key == "icon" and cached.get("icon"):
                continue
            if (cached.get(key) or None) != (value or None):
                changed[key] = value
        if not changed:
            return
        with session_scope() as session:
            rec = session.get(Router, router_id)
            if not rec:
                return
            for key, value in changed.items():
                setattr(rec, key, value)
            if not rec.icon:
                rec.icon = default_router_icon()
            session.commit()
            data = self._row_to_cache(rec)
        self._cache_upsert_router(data)

    # --- poll ---

    def _process_router(self, router_id: int):
        with self._processing_lock:
            if router_id in self._processing_routers:
                return
            self._processing_routers.add(router_id)
        try:
            with self._cache_lock:
                router = deepcopy(self.routers_cache.get(router_id))
            if not router:
                return
            api = self._get_api(router)
            now = get_now_to_utc()
            now_s = now.isoformat(sep=" ", timespec="seconds") if hasattr(now, "isoformat") else str(now)

            info = api.info
            resources = {"cpu": None, "ram": None, "uptime": None}
            if info:
                version = (info.get("show") or {}).get("version") or {}
                model = version.get("model") or version.get("device")
                firmware_version = version.get("release") or version.get("title")
                stable_router = {"model": model, "firmware_version": firmware_version}
                if not router.get("icon"):
                    stable_router["icon"] = default_router_icon()
                self._update_router_stable(router_id, stable_router)

                runtime = {
                    "online": 1,
                    "updated": now_s,
                    "firmware_title": version.get("title") or firmware_version,
                }
                resources = api.system_resources(((info.get("show") or {}).get("system")))
                runtime.update({k: v for k, v in resources.items() if v is not None})
                with self._cache_lock:
                    prev = self.router_runtime.get(router_id, {})
                    runtime["last_pushed"] = prev.get("last_pushed", {})
                    runtime["last_ws"] = prev.get("last_ws", {})
                    runtime["firmware_notified_version"] = prev.get("firmware_notified_version", "")
                    # Clear stale update badge after OS install (version now matches candidate)
                    pending = str(prev.get("update_version") or "").strip()
                    cur = str(firmware_version or "").strip()
                    cleared_update = False
                    if prev.get("update_available") and pending and cur and pending == cur:
                        runtime["update_available"] = 0
                        runtime["update_version"] = ""
                        runtime["firmware_notified_version"] = ""
                        cleared_update = True
                    self.router_runtime[router_id] = {**prev, **runtime}
                    rt = self.router_runtime[router_id]

                sync_fields = resolve_sync_live(
                    router.get("sync_live"),
                    router.get("linked_object"),
                    ROUTER_SYNC_DEFAULT,
                )
                push_props = {
                    "online": 1,
                    "cpu": resources.get("cpu"),
                    "ram": resources.get("ram"),
                    "uptime": resources.get("uptime"),
                    "firmware_version": firmware_version,
                }
                ws_props = {
                    "online": 1,
                    "cpu": resources.get("cpu"),
                    "ram": resources.get("ram"),
                    "uptime": resources.get("uptime"),
                    "firmware_version": firmware_version,
                    "updated": now_s,
                }
                ws_keys = ["online", "cpu", "ram", "uptime", "firmware_version", "updated"]
                if cleared_update:
                    push_props["update_available"] = 0
                    push_props["update_version"] = ""
                    ws_props["update_available"] = 0
                    ws_props["update_version"] = ""
                    ws_keys.extend(["update_available", "update_version"])
                self._push_live(router.get("linked_object"), push_props, rt, sync_fields)
                self._ws_notify(
                    "updateRouter",
                    router_id,
                    rt,
                    ws_props,
                    tuple(ws_keys),
                )

                self._poll_internet(router_id, api, info, now_s)
                self._poll_devices(router_id, api, info, now_s)
                self._poll_vpn(router_id, api, info, now_s, router)
                self._poll_log(router_id, api, router)
            else:
                with self._cache_lock:
                    prev = self.router_runtime.get(router_id, {})
                    self.router_runtime[router_id] = {
                        **prev,
                        "online": 0,
                        "updated": now_s,
                        "last_pushed": prev.get("last_pushed", {}),
                        "last_ws": prev.get("last_ws", {}),
                    }
                    rt = self.router_runtime[router_id]
                sync_fields = resolve_sync_live(
                    router.get("sync_live"),
                    router.get("linked_object"),
                    ROUTER_SYNC_DEFAULT,
                )
                self._push_live(router.get("linked_object"), {"online": 0}, rt, sync_fields)
                self._ws_notify(
                    "updateRouter",
                    router_id,
                    rt,
                    {"online": 0, "updated": now_s},
                    ("online", "updated"),
                )
        finally:
            with self._processing_lock:
                self._processing_routers.discard(router_id)

    def _poll_internet(self, router_id: int, api: ApiRouter, info: dict, now_s: str):
        try:
            status = ((info.get("show") or {}).get("internet") or {}).get("status") or {}
            online = 1 if status.get("internet") else 0
            inet_ip = ""
            inet_rx = 0
            inet_tx = 0
            if online:
                gateway = status.get("gateway") or {}
                interface = gateway.get("interface")
                interface_info = ((info.get("show") or {}).get("interface") or {}).get(interface, {}) if interface else {}
                if not isinstance(interface_info, dict):
                    interface_info = {}
                inet_ip = interface_info.get("address", "") or ""
                if interface:
                    # Prefer counters from batched show.interface; avoid extra interface_stat
                    counter_keys = (
                        "rxbytes",
                        "rx_bytes",
                        "received",
                        "received_bytes",
                        "txbytes",
                        "tx_bytes",
                        "sent",
                        "sent_bytes",
                    )
                    inet_rx, inet_tx = self._extract_interface_traffic(interface_info)
                    if not any(k in interface_info for k in counter_keys):
                        inet_rx, inet_tx = self._extract_interface_traffic(
                            api.interface_stat(interface)
                        )
            stable = {
                "title": "Internet",
                "mac": "0.0.0.0.0.0",
                "hostname": None,
                "interface": None,
                "ssid": None,
                "ap": None,
                "registered": 1,
                "access": None,
                "device_hint": "internet",
                "icon": "fas fa-globe",
            }
            device_id = self._upsert_device_stable(router_id, "0.0.0.0.0.0", stable)
            if not device_id:
                return
            live = {
                "ip": inet_ip,
                "online": online,
                "rxbytes": inet_rx,
                "txbytes": inet_tx,
                "updated": now_s,
            }
            with self._cache_lock:
                prev = self.device_runtime.get(device_id, {})
                live["last_pushed"] = prev.get("last_pushed", {})
                live["last_ws"] = prev.get("last_ws", {})
                self.device_runtime[device_id] = {**prev, **live}
                device = self.devices_cache.get(device_id, {})
                rt = self.device_runtime[device_id]
            sync_fields = resolve_sync_live(
                device.get("sync_live"),
                device.get("linked_object"),
                INTERNET_SYNC_DEFAULT,
            )
            self._push_live(
                device.get("linked_object"),
                {"ip": inet_ip, "online": online, "rxbytes": inet_rx, "txbytes": inet_tx},
                rt,
                sync_fields,
            )
            self._ws_notify(
                "updateDevice",
                device_id,
                rt,
                live,
                ("online", "ip", "rssi", "rxbytes", "txbytes", "uptime", "updated"),
            )
        except Exception:
            self.logger.exception("Error get status internet")

    def _poll_devices(self, router_id: int, api: ApiRouter, info: dict, now_s: str):
        if not api.isAuth:
            return
        seen = set()
        # Reuse hotspot from batched info — skip duplicate GET /rci/show/ip/hotspot
        devices = ApiRouter.hosts_from_info(info)
        if devices is None:
            devices = api.devices
        for dev in devices:
            mac = str(getattr(dev, "mac", None) or "").strip()
            if not mac:
                continue
            title = str(getattr(dev, "name", None) or mac).strip()
            stable = stable_device_fields(dev, title, mac)
            if not stable.get("icon"):
                stable["icon"] = default_device_icon(
                    title,
                    mac,
                    stable.get("device_hint") or "",
                    stable.get("ssid") or "",
                    stable.get("ap") or "",
                )
            device_id = self._upsert_device_stable(router_id, mac, stable)
            if not device_id:
                continue
            seen.add(device_id)
            live = live_device_fields(dev)
            live["updated"] = now_s
            with self._cache_lock:
                prev = self.device_runtime.get(device_id, {})
                live["last_pushed"] = prev.get("last_pushed", {})
                live["last_ws"] = prev.get("last_ws", {})
                self.device_runtime[device_id] = {**prev, **live}
                device = self.devices_cache.get(device_id, {})
                rt = self.device_runtime[device_id]
            live["access"] = device.get("access")
            sync_fields = resolve_sync_live(
                device.get("sync_live"),
                device.get("linked_object"),
                DEVICE_SYNC_DEFAULT,
            )
            push_props = {
                "ip": live.get("ip"),
                "online": live.get("online"),
                "signal_strength": live.get("rssi"),
                "rxbytes": live.get("rxbytes"),
                "txbytes": live.get("txbytes"),
                "uptime": live.get("uptime"),
            }
            self._push_live(device.get("linked_object"), push_props, rt, sync_fields)
            self._ws_notify(
                "updateDevice",
                device_id,
                rt,
                live,
                ("online", "ip", "rssi", "rxbytes", "txbytes", "uptime", "updated", "access"),
            )

        with self._cache_lock:
            device_ids = list(self.devices_by_router.get(router_id, set()))
        for device_id in device_ids:
            with self._cache_lock:
                device = self.devices_cache.get(device_id, {})
            if normalize_mac(device.get("mac")) == "0.0.0.0.0.0":
                continue
            if device_id in seen:
                continue
            with self._cache_lock:
                prev = self.device_runtime.get(device_id, {})
                self.device_runtime[device_id] = {
                    **prev,
                    "online": 0,
                    "updated": now_s,
                    "last_pushed": prev.get("last_pushed", {}),
                    "last_ws": prev.get("last_ws", {}),
                }
                rt = self.device_runtime[device_id]
            sync_fields = resolve_sync_live(
                device.get("sync_live"),
                device.get("linked_object"),
                DEVICE_SYNC_DEFAULT,
            )
            self._push_live(device.get("linked_object"), {"online": 0}, rt, sync_fields)
            self._ws_notify(
                "updateDevice",
                device_id,
                rt,
                {"online": 0, "updated": now_s},
                ("online", "updated"),
            )

    def _poll_vpn(self, router_id: int, api: ApiRouter, info: dict, now_s: str, router: dict = None):
        if router is not None and not router.get("poll_vpn"):
            return
        seen_server_ids: set = set()
        try:
            show = (info.get("show") or {}) if isinstance(info, dict) else {}
            if isinstance(show, dict) and "interface" in show and isinstance(show.get("interface"), dict):
                interfaces = show.get("interface") or {}
            else:
                interfaces = api.show_interfaces()
            tunnels = parse_vpn_interfaces(interfaces)
            for tunnel in tunnels:
                role = tunnel.get("role") or "client"
                vpn_id = self._upsert_vpn_stable(
                    router_id,
                    tunnel["key"],
                    {
                        "role": role,
                        "vpn_type": tunnel["vpn_type"],
                        "title": tunnel["title"],
                        "icon": default_vpn_icon(role),
                    },
                )
                if not vpn_id:
                    continue
                live = {
                    "online": tunnel.get("online", 0),
                    "enabled": tunnel.get("enabled", 0),
                    "address": tunnel.get("address") or "",
                    "ip": tunnel.get("address") or "",
                    "updated": now_s,
                }
                if role == "server":
                    live["clients_online"] = tunnel.get("clients_online", 0)
                with self._cache_lock:
                    prev = self.vpn_runtime.get(vpn_id, {})
                    live["last_pushed"] = prev.get("last_pushed", {})
                    live["last_ws"] = prev.get("last_ws", {})
                    self.vpn_runtime[vpn_id] = {**prev, **live}
                    vpn = self.vpns_cache.get(vpn_id, {})
                    rt = self.vpn_runtime[vpn_id]
                default_sync = VPN_SERVER_SYNC_DEFAULT if role == "server" else VPN_CLIENT_SYNC_DEFAULT
                sync_fields = resolve_sync_live(
                    vpn.get("sync_live"),
                    vpn.get("linked_object"),
                    default_sync,
                )
                push_props = {"online": live["online"], "ip": live["ip"]}
                if role == "server":
                    push_props["clients_online"] = live.get("clients_online", 0)
                self._push_live(vpn.get("linked_object"), push_props, rt, sync_fields)
                self._ws_notify(
                    "updateVpn",
                    vpn_id,
                    rt,
                    live,
                    ("online", "enabled", "ip", "address", "clients_online", "updated", "sessions"),
                )

            for server_raw in api.show_vpn_servers(show):
                server = parse_vpn_server(
                    server_raw.get("status"),
                    server_raw.get("clients"),
                    key=server_raw.get("key") or "vpn-server",
                    vpn_type=server_raw.get("vpn_type") or "vpn-server",
                    title=server_raw.get("title") or "VPN Server",
                )
                if not server:
                    continue
                vpn_id = self._upsert_vpn_stable(
                    router_id,
                    server["key"],
                    {
                        "role": "server",
                        "vpn_type": server["vpn_type"],
                        "title": server["title"],
                        "icon": default_vpn_icon("server"),
                    },
                )
                if not vpn_id:
                    continue
                seen_server_ids.add(int(vpn_id))
                live = {
                    "online": server.get("online", 0),
                    "enabled": server.get("enabled", 0),
                    "address": server.get("address") or "",
                    "ip": server.get("address") or "",
                    "clients_online": server.get("clients_online", 0),
                    "sessions": server.get("sessions") or [],
                    "updated": now_s,
                }
                with self._cache_lock:
                    prev = self.vpn_runtime.get(vpn_id, {})
                    live["last_pushed"] = prev.get("last_pushed", {})
                    live["last_ws"] = prev.get("last_ws", {})
                    live["sessions_by_id"] = prev.get("sessions_by_id")
                    self.vpn_runtime[vpn_id] = {**prev, **live}
                    vpn = self.vpns_cache.get(vpn_id, {})
                    rt = self.vpn_runtime[vpn_id]
                sync_fields = resolve_sync_live(
                    vpn.get("sync_live"),
                    vpn.get("linked_object"),
                    VPN_SERVER_SYNC_DEFAULT,
                )
                self._push_live(
                    vpn.get("linked_object"),
                    {"online": live["online"], "clients_online": live["clients_online"]},
                    rt,
                    sync_fields,
                )
                self._process_vpn_session_events(
                    {**vpn, "title": vpn.get("title") or server.get("title"), "vpn_type": vpn.get("vpn_type") or server.get("vpn_type"), "key": vpn.get("key") or server.get("key")},
                    rt,
                    live.get("sessions") or [],
                    sync_fields,
                )
                self._ws_notify(
                    "updateVpn",
                    vpn_id,
                    rt,
                    live,
                    ("online", "enabled", "ip", "address", "clients_online", "updated", "sessions"),
                )

            # Servers that vanished from this poll still need disconnect for leftover sessions
            with self._cache_lock:
                orphan_ids = [
                    vid
                    for (rid, _key), vid in list(self.vpns_by_router_key.items())
                    if rid == router_id and vid not in seen_server_ids
                ]
            for vpn_id in orphan_ids:
                with self._cache_lock:
                    vpn = deepcopy(self.vpns_cache.get(vpn_id) or {})
                    rt = self.vpn_runtime.setdefault(vpn_id, {})
                if str(vpn.get("role") or "") != "server":
                    continue
                if not rt.get("sessions_by_id"):
                    continue
                sync_fields = resolve_sync_live(
                    vpn.get("sync_live"),
                    vpn.get("linked_object"),
                    VPN_SERVER_SYNC_DEFAULT,
                )
                self._process_vpn_session_events(vpn, rt, [], sync_fields)
                with self._cache_lock:
                    rt["sessions"] = []
                    rt["clients_online"] = 0
                self._ws_notify(
                    "updateVpn",
                    vpn_id,
                    rt,
                    {"sessions": [], "clients_online": 0, "updated": now_s},
                    ("sessions", "clients_online", "updated"),
                )

            # Drop legacy key from early VPN discovery (vpn-server → pptp-server)
            with self._cache_lock:
                legacy_id = self.vpns_by_router_key.get((router_id, "vpn-server"))
                has_pptp = self.vpns_by_router_key.get((router_id, "pptp-server"))
            if legacy_id and has_pptp:
                with session_scope() as session:
                    session.execute(delete(KeeneticVpn).where(KeeneticVpn.id == legacy_id))
                    session.commit()
                self._cache_delete_vpn(legacy_id)
        except Exception as ex:
            self.logger.exception("Keenetic VPN poll failed for router %s: %s", router_id, ex)
        finally:
            with self._cache_lock:
                rt = self.router_runtime.setdefault(router_id, {})
                rt["vpn_checked_at"] = now_s

    def _notify_log_entry(self, router: dict, entry: dict):
        linked = str(router.get("linked_object") or "").strip()
        method = str(router.get("linked_method") or "").strip()
        if not linked or not method:
            return
        message = str(entry.get("message") or "")
        params = {
            "EVENT": "log",
            "VALUE": message,
            "NEW_VALUE": message,
            "MESSAGE": message,
            "LEVEL": str(entry.get("level") or ""),
            "TIME": str(entry.get("time") or ""),
            "FACILITY": str(entry.get("facility") or ""),
            "LABEL": str(entry.get("label") or ""),
            "LOG_ID": str(entry.get("id") or ""),
            "REPEATED": entry.get("repeated") or 0,
            "ROUTER_ID": router.get("id"),
            "ROUTER_TITLE": router.get("title") or router.get("ip") or "",
            "SOURCE": self.name,
        }
        callMethodThread(f"{linked}.{method}", params, self.name)

    def _poll_log(self, router_id: int, api: ApiRouter, router: dict):
        if not router.get("poll_log"):
            return
        try:
            raw = api.get_log(LOG_POLL_LIMIT)
            if raw is None:
                return
            entries = normalize_log_entries(raw)
            if not entries:
                return

            with self._cache_lock:
                rt = self.router_runtime.setdefault(router_id, {})
                seen = rt.get("log_seen")
                if not isinstance(seen, set):
                    seen = set(seen or [])
                buffer = list(rt.get("log_entries") or [])
                baseline_done = bool(rt.get("log_baseline_done"))

            new_entries: List[dict] = []
            for entry in entries:
                eid = str(entry.get("id") or "")
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                new_entries.append(entry)

            if not baseline_done:
                # First snapshot: fill buffer newest-first, no method floods
                ordered = list(reversed(entries)) if entries else []
                buf_limit = self._journal_buffer_limit()
                buffer = ordered[-buf_limit:]
                seen = {str(e.get("id")) for e in entries if e.get("id")}
                with self._cache_lock:
                    rt = self.router_runtime.setdefault(router_id, {})
                    rt["log_entries"] = buffer
                    rt["log_seen"] = seen
                    rt["log_baseline_done"] = True
                if buffer:
                    self.sendDataToWebsocket(
                        "appendLog",
                        {"id": int(router_id), "entries": buffer, "replace": True},
                    )
                return

            if not new_entries:
                return

            # Keenetic snapshot order is typically oldest→newest; notify in that order
            for entry in new_entries:
                buffer.append(entry)
                self._apply_log_rules(router, entry)
            buf_limit = self._journal_buffer_limit()
            if len(buffer) > buf_limit:
                buffer = buffer[-buf_limit:]

            with self._cache_lock:
                rt = self.router_runtime.setdefault(router_id, {})
                rt["log_entries"] = buffer
                rt["log_seen"] = seen
                # Cap seen set so it does not grow forever
                if len(seen) > buf_limit * 3:
                    keep = {str(e.get("id")) for e in buffer if e.get("id")}
                    rt["log_seen"] = keep

            self.sendDataToWebsocket(
                "appendLog",
                {"id": int(router_id), "entries": new_entries, "replace": False},
            )
        except Exception as ex:
            self.logger.exception("Keenetic log poll failed for router %s: %s", router_id, ex)

    def _notify_firmware_update(self, router: dict, fw: dict, runtime: dict):
        """addNotify + linked_method once per new update_version."""
        if not fw.get("update_available"):
            runtime["firmware_notified_version"] = ""
            return
        version = str(fw.get("update_version") or "").strip()
        if not version:
            return
        if runtime.get("firmware_notified_version") == version:
            return
        linked = str(router.get("linked_object") or "").strip()
        method = str(router.get("linked_method") or "").strip()
        current = str(fw.get("firmware_version") or router.get("firmware_version") or "").strip()
        title = router.get("title") or router.get("ip") or router.get("id") or "Keenetic"
        value = f"{title}: firmware update available"
        if current:
            value += f" ({current} → {version})"
        else:
            value += f" ({version})"
        notified = False
        try:
            addNotify(
                "Keenetic firmware update",
                value,
                CategoryNotify.Warning,
                self.name,
                params={
                    "router_id": router.get("id"),
                    "title": title,
                    "firmware_version": current,
                    "update_version": version,
                    "update_channel": str(fw.get("update_channel") or ""),
                    "event": "firmware_update",
                    "url": f"/admin/Keenetic?router={router.get('id')}" if router.get("id") else "/admin/Keenetic",
                },
            )
            notified = True
        except Exception:
            self.logger.exception("addNotify failed for firmware update")
        if linked and method:
            params = {
                "EVENT": "firmware_update",
                "VALUE": value,
                "NEW_VALUE": value,
                "FIRMWARE_VERSION": current,
                "UPDATE_VERSION": version,
                "UPDATE_CHANNEL": str(fw.get("update_channel") or ""),
                "UPDATE_AVAILABLE": 1,
                "MODEL": fw.get("model") or router.get("model") or "",
                "ROUTER_ID": router.get("id"),
                "ROUTER_TITLE": title,
                "SOURCE": self.name,
            }
            try:
                callMethodThread(f"{linked}.{method}", params, self.name)
                notified = True
            except Exception:
                self.logger.exception(
                    "linked_method failed for firmware update %s.%s", linked, method
                )
        # Mark only after a successful notify path so a failed addNotify can retry
        if notified:
            runtime["firmware_notified_version"] = version
        else:
            self.logger.warning(
                "Firmware update available for router %s (%s → %s) but notify failed",
                router.get("id"),
                current,
                version,
            )

    def _apply_firmware_result(self, router_id: int, router: dict, fw: dict):
        """Persist firmware check result, sync live/WS, notify on new update."""
        self._update_router_stable(
            router_id,
            {
                "firmware_version": fw.get("firmware_version"),
                "model": fw.get("model") or router.get("model"),
            },
        )
        with self._cache_lock:
            prev = self.router_runtime.get(router_id, {})
            notified = prev.get("firmware_notified_version", "")
            self.router_runtime[router_id] = {
                **prev,
                "firmware_title": fw.get("firmware_title"),
                "update_available": 1 if fw.get("update_available") else 0,
                "update_version": fw.get("update_version") or "",
                "update_channel": fw.get("update_channel") or "",
                "firmware_checked_at": get_now_to_utc().isoformat(sep=" ", timespec="seconds"),
                "firmware_notified_version": notified,
                "last_pushed": prev.get("last_pushed", {}),
                "last_ws": prev.get("last_ws", {}),
            }
            rt = self.router_runtime[router_id]
            router = self.routers_cache.get(router_id, router)
        sync_fields = resolve_sync_live(
            router.get("sync_live"),
            router.get("linked_object"),
            ROUTER_SYNC_DEFAULT,
        )
        self._push_live(
            router.get("linked_object"),
            {
                "firmware_version": fw.get("firmware_version"),
                "update_available": 1 if fw.get("update_available") else 0,
                "update_version": fw.get("update_version") or "",
            },
            rt,
            sync_fields,
        )
        self._ws_notify(
            "updateRouter",
            router_id,
            rt,
            {
                "firmware_version": fw.get("firmware_version"),
                "update_available": 1 if fw.get("update_available") else 0,
                "update_version": fw.get("update_version") or "",
            },
            ("firmware_version", "update_available", "update_version"),
        )
        self._notify_firmware_update(router, fw, rt)

    def _poll_firmware(self):
        now_ts = time.time()
        interval = float(self.config.get("firmware_check_interval", 3600) or 3600)
        if self._last_firmware_check and (now_ts - self._last_firmware_check) < interval:
            return
        self._last_firmware_check = now_ts
        with self._cache_lock:
            router_ids = list(self.routers_cache.keys())
        for router_id in router_ids:
            with self._cache_lock:
                router = deepcopy(self.routers_cache.get(router_id))
            if not router:
                continue
            try:
                api = self._get_api(router)
                if not api.isAuth:
                    continue
                fw = api.check_firmware()
                self._apply_firmware_result(router_id, router, fw)
            except Exception:
                self.logger.exception("Firmware check failed for router %s", router_id)

    def _poll_routers(self):
        if not self.routers_cache:
            self._load_entity_cache()
        with self._cache_lock:
            routers = list(self.routers_cache.keys())
        if not routers:
            return
        with ThreadPoolExecutor(max_workers=min(len(routers), 10)) as executor:
            futures = {executor.submit(self._process_router, router_id): router_id for router_id in routers}
            for future in as_completed(futures):
                router_id = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    self.logger.error("Router %s generated an exception: %s", router_id, exc)
        self._poll_firmware()

    # --- control ops ---

    def invoke_operation(self, operation: str, params: Optional[dict] = None) -> dict:
        params = params or {}
        if operation == "poll_now":
            self._poll_routers()
            return {"ok": True, "operation": operation}
        if operation == "check_firmware":
            router_id = int(params.get("router_id"))
            with self._cache_lock:
                router = deepcopy(self.routers_cache.get(router_id))
            if not router:
                raise ValueError(f"Router not found: {router_id}")
            api = self._get_api(router)
            fw = api.check_firmware()
            self._apply_firmware_result(router_id, router, fw)
            return {"ok": True, "operation": operation, "result": fw}

        router_id = params.get("router_id")
        device_id = params.get("device_id")
        vpn_id = params.get("vpn_id")

        if operation in ("reboot", "save_config", "apply_update"):
            router_id = int(router_id)
            with self._cache_lock:
                router = deepcopy(self.routers_cache.get(router_id))
            if not router:
                raise ValueError(f"Router not found: {router_id}")
            api = self._get_api(router)
            if operation == "reboot":
                ok = api.reboot(params.get("interval"))
            elif operation == "save_config":
                ok = api.save_config()
            else:
                with self._cache_lock:
                    rt = self.router_runtime.get(router_id) or {}
                channel = (
                    params.get("channel")
                    or rt.get("update_channel")
                    or router.get("update_channel")
                )
                ok = api.apply_update(channel=channel)
                if ok:
                    # Hide badge immediately; poll will confirm new version after reboot
                    with self._cache_lock:
                        rt = self.router_runtime.setdefault(router_id, {})
                        rt["update_available"] = 0
                        rt["update_version"] = ""
                        rt["firmware_notified_version"] = ""
                    self._ws_notify(
                        "updateRouter",
                        router_id,
                        rt,
                        {"update_available": 0, "update_version": ""},
                        ("update_available", "update_version"),
                    )
                if not ok:
                    self.logger.error(
                        "apply_update failed for router %s (channel=%s)",
                        router_id,
                        channel or "",
                    )
            return {"ok": bool(ok), "operation": operation}

        if operation in ("wake", "set_access", "set_name", "set_policy"):
            device_id = int(device_id)
            with self._cache_lock:
                device = deepcopy(self.devices_cache.get(device_id))
            if not device:
                raise ValueError(f"Device not found: {device_id}")
            router_id = int(device.get("router_id"))
            with self._cache_lock:
                router = deepcopy(self.routers_cache.get(router_id))
            api = self._get_api(router)
            mac = device.get("mac")
            if operation == "wake":
                ok = api.wake_host(mac)
            elif operation == "set_access":
                access = str(params.get("access") or "").strip().lower()
                ok = api.set_host_access(mac, access)
                if ok:
                    self._upsert_device_stable(
                        router_id,
                        mac,
                        {
                            "title": device.get("title"),
                            "mac": mac,
                            "hostname": device.get("hostname"),
                            "interface": device.get("interface"),
                            "ssid": device.get("ssid"),
                            "ap": device.get("ap"),
                            "registered": device.get("registered"),
                            "access": access,
                            "device_hint": device.get("device_hint"),
                            "icon": device.get("icon"),
                        },
                    )
                    api.save_config()
                    with self._cache_lock:
                        rt = self.device_runtime.setdefault(device_id, {})
                    self._ws_notify(
                        "updateDevice",
                        device_id,
                        rt,
                        {"access": access},
                        ("access",),
                    )
            elif operation == "set_name":
                name = str(params.get("name") or "").strip()
                ok = api.set_host_name(mac, name)
                if ok and name:
                    self._upsert_device_stable(
                        router_id,
                        mac,
                        {
                            "title": name,
                            "mac": mac,
                            "hostname": device.get("hostname"),
                            "interface": device.get("interface"),
                            "ssid": device.get("ssid"),
                            "ap": device.get("ap"),
                            "registered": device.get("registered"),
                            "access": device.get("access"),
                            "device_hint": device.get("device_hint"),
                            "icon": device.get("icon"),
                        },
                    )
                    api.save_config()
            else:
                ok = api.set_host_policy(mac, params.get("policy"))
                if ok:
                    api.save_config()
            return {"ok": bool(ok), "operation": operation}

        if operation in ("vpn_connect", "vpn_disconnect", "vpn_enable", "vpn_disable", "vpn_kick"):
            if vpn_id not in (None, ""):
                vpn_id = int(vpn_id)
                with self._cache_lock:
                    vpn = deepcopy(self.vpns_cache.get(vpn_id))
            else:
                router_id = int(router_id)
                key = str(params.get("key") or "").strip()
                with self._cache_lock:
                    vpn_id = self.vpns_by_router_key.get((router_id, key))
                    vpn = deepcopy(self.vpns_cache.get(vpn_id)) if vpn_id else None
            if not vpn:
                raise ValueError("VPN not found")
            router_id = int(vpn.get("router_id"))
            with self._cache_lock:
                router = deepcopy(self.routers_cache.get(router_id))
            api = self._get_api(router)
            role = str(vpn.get("role") or "")
            key = str(vpn.get("key") or "")
            if operation == "vpn_kick":
                if role != "server" and not api.vpn_service_name(key):
                    raise ValueError("vpn_kick is for VPN servers")
                session_id = params.get("session_id") or params.get("session")
                ok = api.vpn_logout_session(key, session_id)
                if ok:
                    vpn_entity_id = int(vpn.get("id"))
                    with self._cache_lock:
                        rt = self.vpn_runtime.setdefault(vpn_entity_id, {})
                        prev_sessions = list(rt.get("sessions") or [])
                        kicked = next(
                            (
                                s
                                for s in prev_sessions
                                if str(s.get("session_id")) == str(session_id)
                            ),
                            None,
                        )
                        if kicked is None:
                            # Fall back to sessions_by_id snapshot
                            for s in (rt.get("sessions_by_id") or {}).values():
                                if str(s.get("session_id")) == str(session_id):
                                    kicked = s
                                    break
                        sessions = [
                            s
                            for s in prev_sessions
                            if str(s.get("session_id")) != str(session_id)
                        ]
                        rt["sessions"] = sessions
                        rt["clients_online"] = sum(1 for s in sessions if s.get("connected"))
                        live_ws = {
                            "clients_online": rt.get("clients_online", 0),
                            "sessions": sessions,
                        }
                    sync_fields = resolve_sync_live(
                        vpn.get("sync_live"),
                        vpn.get("linked_object"),
                        VPN_SERVER_SYNC_DEFAULT,
                    )
                    if kicked:
                        self._notify_vpn_session(vpn, rt, "disconnect", kicked, sync_fields)
                    with self._cache_lock:
                        rt["sessions_by_id"] = sessions_by_identity(
                            [s for s in sessions if s.get("connected")]
                        )
                    self._ws_notify(
                        "updateVpn",
                        vpn_entity_id,
                        rt,
                        live_ws,
                        ("clients_online", "sessions"),
                    )
                return {"ok": bool(ok), "operation": operation, "session_id": session_id}
            if operation in ("vpn_enable", "vpn_disable"):
                if role != "server" and not api.vpn_service_name(key):
                    raise ValueError("vpn_enable/disable is for VPN servers")
                enabled = operation == "vpn_enable"
                ok = api.set_vpn_server_enabled(key, enabled)
                if ok:
                    api.save_config()
                    vpn_entity_id = int(vpn.get("id"))
                    with self._cache_lock:
                        rt = self.vpn_runtime.setdefault(vpn_entity_id, {})
                        rt["enabled"] = 1 if enabled else 0
                        if not enabled:
                            rt["online"] = 0
                            rt["clients_online"] = 0
                            rt["sessions"] = []
                            rt["sessions_by_id"] = {}
                        live_ws = {
                            "enabled": rt.get("enabled", 0),
                            "online": rt.get("online", 0),
                            "clients_online": rt.get("clients_online", 0),
                            "sessions": rt.get("sessions") or [],
                        }
                    self._ws_notify(
                        "updateVpn",
                        vpn_entity_id,
                        rt,
                        live_ws,
                        ("enabled", "online", "clients_online", "sessions"),
                    )
                return {"ok": bool(ok), "operation": operation, "enabled": enabled}
            if role == "server" or api.vpn_service_name(key):
                raise ValueError("vpn_connect/disconnect is for client tunnels only")
            ok = api.set_interface_state(key, up=(operation == "vpn_connect"))
            if ok:
                with self._cache_lock:
                    rt = self.vpn_runtime.setdefault(int(vpn.get("id")), {})
                    rt["online"] = 1 if operation == "vpn_connect" else 0
                    rt["enabled"] = 1 if operation == "vpn_connect" else rt.get("enabled", 0)
                self._ws_notify(
                    "updateVpn",
                    int(vpn.get("id")),
                    self.vpn_runtime.get(int(vpn.get("id")), {}),
                    {"online": 1 if operation == "vpn_connect" else 0},
                    ("online",),
                )
            return {"ok": bool(ok), "operation": operation}

        raise ValueError(f"Unsupported operation: {operation}")

    def cyclic_task(self):
        self._poll_routers()
        if self.event:
            self.event.wait(float(self.config.get("interval", 5.0)))

    def changeObject(self, event, object_name, property_name, method_name, new_value):
        with session_scope() as session:
            for model in (KeeneticDevice, Router, KeeneticVpn, KeeneticLogRule):
                rows = session.query(model).filter(model.linked_object == object_name).all()
                for row in rows:
                    row.linked_object = new_value
            session.commit()
        self._load_entity_cache()

    def search(self, query: str) -> list:
        res = []
        q = (query or "").lower()
        for router in self.list_merged_routers():
            hay = " ".join(
                str(router.get(k) or "") for k in ("title", "ip", "linked_object", "firmware_version")
            ).lower()
            if q in hay:
                res.append(
                    {
                        "url": f'/admin/Keenetic?op=edit&router={router["id"]}',
                        "title": f'Router: {router.get("title")}',
                        "tags": [{"name": "Keenetic", "color": "info"}],
                    }
                )
        for device in self.list_merged_devices():
            hay = " ".join(
                str(device.get(k) or "") for k in ("title", "ip", "mac", "linked_object")
            ).lower()
            if q in hay:
                res.append(
                    {
                        "url": f'/admin/Keenetic?op=edit&device={device["id"]}',
                        "title": f'Device: {device.get("title")}',
                        "tags": [{"name": "Keenetic", "color": "warning"}],
                    }
                )
        for vpn in self.list_merged_vpns():
            hay = " ".join(
                str(vpn.get(k) or "") for k in ("title", "key", "vpn_type", "linked_object")
            ).lower()
            if q in hay:
                res.append(
                    {
                        "url": f'/admin/Keenetic?op=edit&vpn={vpn["id"]}',
                        "title": f'VPN: {vpn.get("title")}',
                        "tags": [{"name": "Keenetic", "color": "success"}],
                    }
                )
        for rule in self.list_log_rules():
            hay = " ".join(
                str(rule.get(k) or "")
                for k in ("title", "pattern", "linked_object", "linked_method")
            ).lower()
            if q in hay:
                res.append(
                    {
                        "url": f'/admin/Keenetic?op=edit&log_rule={rule["id"]}',
                        "title": f'Log rule: {rule.get("title")}',
                        "tags": [{"name": "Keenetic", "color": "secondary"}],
                    }
                )
        return res

    def widget(self):
        content = {
            "routers": len(self.routers_cache),
            "devices": len(self.devices_cache),
            "vpns": len(self.vpns_cache),
        }
        return render_template("widget_keenetic.html", **content)

    def _normalize_linked(self, value):
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return None
        return str(value).strip()

    def _icon_from_form(self, form) -> Optional[str]:
        value = None
        if form is not None and hasattr(form, "icon"):
            value = form.icon.data
        if value is None:
            value = request.form.get("icon")
        text = str(value or "").strip()
        return text or None

    def _sync_live_from_form(self, form) -> Optional[str]:
        selected = request.form.getlist("sync_live")
        if not selected:
            # distinguish "nothing selected" vs field absent
            if "sync_live" in request.form or request.form.get("sync_live_present"):
                return ""
            return None
        return ",".join(selected)

    def admin(self, request_):
        op = request_.args.get("op", None)
        if op == "delete":
            router_id = int(request_.args.get("router", 0))
            device_id = int(request_.args.get("device", 0))
            vpn_id = int(request_.args.get("vpn", 0))
            log_rule_id = int(request_.args.get("log_rule", 0))
            with session_scope() as session:
                if router_id > 0:
                    session.execute(delete(KeeneticDevice).where(KeeneticDevice.router_id == router_id))
                    session.execute(delete(KeeneticVpn).where(KeeneticVpn.router_id == router_id))
                    session.execute(delete(KeeneticLogRule).where(KeeneticLogRule.router_id == router_id))
                    session.execute(delete(Router).where(Router.id == router_id))
                    session.commit()
                    self._cache_delete_router(router_id)
                if device_id > 0:
                    session.execute(delete(KeeneticDevice).where(KeeneticDevice.id == device_id))
                    session.commit()
                    self._cache_delete_device(device_id)
                if vpn_id > 0:
                    session.execute(delete(KeeneticVpn).where(KeeneticVpn.id == vpn_id))
                    session.commit()
                    self._cache_delete_vpn(vpn_id)
                if log_rule_id > 0:
                    rule = session.get(KeeneticLogRule, log_rule_id)
                    parent_id = int(rule.router_id) if rule else 0
                    session.execute(delete(KeeneticLogRule).where(KeeneticLogRule.id == log_rule_id))
                    session.commit()
                    self._cache_delete_log_rule(log_rule_id)
                    if parent_id:
                        return redirect(f"Keenetic?router={parent_id}#keenetic-log")
            return redirect("Keenetic")

        if op in (
            "wake",
            "permit",
            "deny",
            "reboot",
            "check_firmware",
            "apply_update",
            "vpn_connect",
            "vpn_disconnect",
            "vpn_enable",
            "vpn_disable",
            "vpn_kick",
        ):
            try:
                if op in ("wake", "permit", "deny"):
                    device_id = int(request_.args.get("device"))
                    if op == "wake":
                        self.invoke_operation("wake", {"device_id": device_id})
                    else:
                        self.invoke_operation(
                            "set_access",
                            {"device_id": device_id, "access": "permit" if op == "permit" else "deny"},
                        )
                    device = self.merge_device(device_id) or {}
                    return redirect(f"?router={device.get('router_id')}#keenetic-devices")
                if op in ("reboot", "check_firmware", "apply_update"):
                    router_id = int(request_.args.get("router"))
                    self.invoke_operation(op if op != "apply_update" else "apply_update", {"router_id": router_id})
                    return redirect("Keenetic")
                vpn_id = int(request_.args.get("vpn"))
                params = {"vpn_id": vpn_id}
                if op == "vpn_kick":
                    params["session_id"] = request_.args.get("session") or request_.args.get("session_id")
                self.invoke_operation(op, params)
                vpn = self.merge_vpn(vpn_id) or {}
                return redirect(f"?router={vpn.get('router_id')}#keenetic-vpn")
            except Exception as ex:
                self.logger.exception("Control op failed: %s", op)
                return redirect("Keenetic")

        if op == "add":
            if "log_rule" in request_.args:
                parent_id = int(request_.args.get("router") or 0)
                parent = self.merge_router(parent_id) if parent_id else None
                if not parent:
                    return redirect("Keenetic")
                form = LogRuleForm()
                if form.validate_on_submit():
                    with session_scope() as session:
                        rule = KeeneticLogRule(
                            router_id=parent_id,
                            title=form.title.data,
                            pattern=(form.pattern.data or "").strip(),
                            write_to_file=1 if form.write_to_file.data else 0,
                            linked_object=self._normalize_linked(form.linked_object.data),
                            linked_method=self._normalize_linked(form.linked_method.data),
                            active=1 if form.active.data else 0,
                        )
                        session.add(rule)
                        session.commit()
                        data = self._row_to_cache(rule)
                    self._cache_upsert_log_rule(data)
                    return redirect(f"Keenetic?router={parent_id}#keenetic-log")
                if request_.method == "GET":
                    form.active.data = True
                return self.render(
                    "keenetic_log_rule.html",
                    {
                        "form": form,
                        "router_id": parent_id,
                        "router_title": parent.get("title") or str(parent_id),
                        "rule_id": None,
                    },
                )
            form = RouterForm()
            if form.validate_on_submit():
                with session_scope() as session:
                    router = Router()
                    form.populate_obj(router)
                    router.linked_object = self._normalize_linked(form.linked_object.data)
                    router.linked_method = self._normalize_linked(form.linked_method.data)
                    router.poll_log = 1 if form.poll_log.data else 0
                    router.poll_vpn = 1 if form.poll_vpn.data else 0
                    router.log_to_file = 1 if form.log_to_file.data else 0
                    router.icon = self._icon_from_form(form)
                    sync = self._sync_live_from_form(form)
                    if sync is not None:
                        router.sync_live = sync
                    if not router.icon:
                        router.icon = default_router_icon()
                    session.add(router)
                    session.commit()
                    data = self._row_to_cache(router)
                self._cache_upsert_router(data)
                return redirect("Keenetic")
            return self.render("keenetic_router.html", {"form": form, "sync_live": []})

        if op == "edit":
            router_id = request_.args.get("router")
            device_id = request_.args.get("device")
            vpn_id = request_.args.get("vpn")
            log_rule_id = request_.args.get("log_rule")
            if log_rule_id:
                with session_scope() as session:
                    rule = session.get(KeeneticLogRule, int(log_rule_id))
                    if not rule:
                        return redirect("Keenetic")
                    form = LogRuleForm(obj=rule)
                    if request_.method == "GET":
                        form.write_to_file.data = bool(rule.write_to_file)
                        form.active.data = bool(rule.active)
                    if form.validate_on_submit():
                        rule.title = form.title.data
                        rule.pattern = (form.pattern.data or "").strip()
                        rule.write_to_file = 1 if form.write_to_file.data else 0
                        rule.linked_object = self._normalize_linked(form.linked_object.data)
                        rule.linked_method = self._normalize_linked(form.linked_method.data)
                        rule.active = 1 if form.active.data else 0
                        session.commit()
                        self._cache_upsert_log_rule(self._row_to_cache(rule))
                        return redirect(f"Keenetic?router={rule.router_id}#keenetic-log")
                    parent = self.merge_router(int(rule.router_id)) or {}
                    return self.render(
                        "keenetic_log_rule.html",
                        {
                            "form": form,
                            "router_id": rule.router_id,
                            "router_title": parent.get("title") or str(rule.router_id),
                            "rule_id": rule.id,
                        },
                    )
            if router_id:
                with session_scope() as session:
                    router = session.get(Router, router_id)
                    form = RouterForm(obj=router)
                    if request_.method == "GET":
                        form.poll_log.data = bool(router.poll_log)
                        form.poll_vpn.data = bool(router.poll_vpn)
                        form.log_to_file.data = bool(router.log_to_file)
                    if form.validate_on_submit():
                        form.populate_obj(router)
                        router.linked_object = self._normalize_linked(form.linked_object.data)
                        router.linked_method = self._normalize_linked(form.linked_method.data)
                        router.poll_log = 1 if form.poll_log.data else 0
                        router.poll_vpn = 1 if form.poll_vpn.data else 0
                        router.log_to_file = 1 if form.log_to_file.data else 0
                        router.icon = self._icon_from_form(form)
                        sync = self._sync_live_from_form(form)
                        if sync is not None:
                            router.sync_live = sync
                        if not router.icon:
                            router.icon = default_router_icon()
                        session.commit()
                        self._cache_upsert_router(self._row_to_cache(router))
                        if router.poll_log:
                            with self._cache_lock:
                                rt = self.router_runtime.setdefault(int(router.id), {})
                                rt["log_baseline_done"] = False
                        return redirect(f"?router={router.id}")
                    sync_live = resolve_sync_live(router.sync_live, router.linked_object, ROUTER_SYNC_DEFAULT)
                    return self.render(
                        "keenetic_router.html",
                        {
                            "form": form,
                            "sync_live": sorted(sync_live),
                            "router_id": router.id,
                            "router_title": router.title,
                        },
                    )
            if device_id:
                with session_scope() as session:
                    device = session.get(KeeneticDevice, device_id)
                    form = DeviceForm(obj=device)
                    if form.validate_on_submit():
                        form.populate_obj(device)
                        device.linked_object = self._normalize_linked(form.linked_object.data)
                        device.icon = self._icon_from_form(form)
                        sync = self._sync_live_from_form(form)
                        if sync is not None:
                            device.sync_live = sync
                        session.commit()
                        self._cache_upsert_device(self._row_to_cache(device))
                        return redirect(f"?router={device.router_id}#keenetic-devices")
                    default = (
                        INTERNET_SYNC_DEFAULT
                        if normalize_mac(device.mac) == "0.0.0.0.0.0"
                        else DEVICE_SYNC_DEFAULT
                    )
                    sync_live = resolve_sync_live(device.sync_live, device.linked_object, default)
                    parent = self.merge_router(int(device.router_id)) or {}
                    return self.render(
                        "keenetic_device.html",
                        {
                            "form": form,
                            "router_id": device.router_id,
                            "router_title": parent.get("title") or str(device.router_id),
                            "device_id": device.id,
                            "sync_live": sorted(sync_live),
                            "is_internet": normalize_mac(device.mac) == "0.0.0.0.0.0",
                        },
                    )
            if vpn_id:
                with session_scope() as session:
                    vpn = session.get(KeeneticVpn, vpn_id)
                    form = VpnForm(obj=vpn)
                    if form.validate_on_submit():
                        form.populate_obj(vpn)
                        vpn.linked_object = self._normalize_linked(form.linked_object.data)
                        vpn.linked_method = self._normalize_linked(form.linked_method.data)
                        vpn.icon = self._icon_from_form(form)
                        sync = self._sync_live_from_form(form)
                        if sync is not None:
                            vpn.sync_live = sync
                        if not vpn.icon:
                            vpn.icon = default_vpn_icon(vpn.role or "client")
                        session.commit()
                        self._cache_upsert_vpn(self._row_to_cache(vpn))
                        return redirect("?router=" + str(vpn.router_id) + "#keenetic-vpn")
                    default = VPN_SERVER_SYNC_DEFAULT if vpn.role == "server" else VPN_CLIENT_SYNC_DEFAULT
                    sync_live = resolve_sync_live(vpn.sync_live, vpn.linked_object, default)
                    parent = self.merge_router(int(vpn.router_id)) or {}
                    return self.render(
                        "keenetic_vpn.html",
                        {
                            "form": form,
                            "router_id": vpn.router_id,
                            "router_title": parent.get("title") or str(vpn.router_id),
                            "vpn_id": vpn.id,
                            "sync_live": sorted(sync_live),
                            "role": vpn.role,
                        },
                    )

        router_id = request_.args.get("router")
        if router_id:
            router = self.merge_router(int(router_id))
            if not router:
                return redirect("Keenetic")
            devices = self.list_merged_devices(int(router_id))
            vpns = self.list_merged_vpns(int(router_id))
            log_rules = self.list_log_rules(int(router_id))
            with self._cache_lock:
                rt = self.router_runtime.get(int(router_id), {})
                log_entries = list(rt.get("log_entries") or [])
            return self.render(
                "keenetic_devices.html",
                {
                    "router": router,
                    "devices": devices,
                    "vpns": vpns,
                    "log_rules": log_rules,
                    "log_entries": log_entries,
                    "poll_log": bool(router.get("poll_log")),
                    "poll_vpn": bool(router.get("poll_vpn")),
                },
            )

        settings = SettingsForm()
        if request_.method == "GET":
            settings.interval.data = self.config.get("interval", 5)
            settings.firmware_check_interval.data = self.config.get("firmware_check_interval", 3600)
            settings.journal_buffer_limit.data = self.config.get(
                "journal_buffer_limit", DEFAULT_JOURNAL_BUFFER_LIMIT
            )
        else:
            if settings.validate_on_submit():
                self.config["interval"] = settings.interval.data
                self.config["firmware_check_interval"] = settings.firmware_check_interval.data
                self.config["journal_buffer_limit"] = settings.journal_buffer_limit.data
                self.config.pop("journal_file_log", None)
                self.saveConfig()
                self._trim_all_journal_buffers()
                return redirect("Keenetic")

        routers = self.list_merged_routers()
        return self.render("keenetic_main.html", {"routers": routers, "form": settings})

    # --- MCP ---

    def mcp_capabilities(self):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_capabilities()

    def mcp_config_schema(self):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_config_schema()

    def mcp_entity_schema(self, collection: str):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_entity_schema(collection)

    def mcp_list_entities(
        self,
        collection: str,
        query: str = None,
        limit: int = 100,
        router_id=None,
        linked_object=None,
        has_linked_object=None,
    ):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_list_entities(
            collection,
            query=query,
            limit=limit,
            router_id=router_id,
            linked_object=linked_object,
            has_linked_object=has_linked_object,
        )

    def mcp_get_entity(self, collection: str, entity_id):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_get_entity(collection, entity_id)

    def mcp_upsert_entity(self, collection: str, payload: dict, entity_id=None):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_upsert_entity(collection, payload, entity_id=entity_id)

    def mcp_delete_entity(self, collection: str, entity_id):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_delete_entity(collection, entity_id)

    def mcp_validate_entity_code(self, collection: str, code: str):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_validate_entity_code(collection, code)

    def mcp_run_entity_dry(self, collection: str, code: str, context: dict = None):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_run_entity_dry(collection, code, context=context)

    def mcp_invoke(self, operation: str, params: dict = None):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_invoke(operation, params or {})

    def mcp_entity_revision(self, collection: str, entity_id):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_entity_revision(collection, entity_id)

    def mcp_validate_entity(self, collection: str, payload: dict, entity_id=None):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_validate_entity(collection, payload, entity_id=entity_id)

    def mcp_tools(self):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_descriptors()[0]

    def mcp_resources(self):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_descriptors()[1]

    def mcp_prompts(self):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_descriptors()[2]

    def mcp_get_prompt(self, name: str, arguments: dict = None):
        from plugins.Keenetic import mcp_support

        return mcp_support.mcp_get_prompt(name, arguments or {})
