"""MCP integration helpers for Keenetic plugin."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import delete

from app.core.lib.mcp_contract import (
    build_plugin_mcp_descriptors,
    revision_from_datetime,
    revision_from_dict,
    validate_entity_payload,
)
from app.core.lib.plugin_binding import sync_object_link, validate_object_exists
from app.database import row2dict, session_scope

from plugins.Keenetic.helpers import default_router_icon, default_vpn_icon
from plugins.Keenetic.models.Device import KeeneticDevice
from plugins.Keenetic.models.LogRule import KeeneticLogRule
from plugins.Keenetic.models.Router import Router
from plugins.Keenetic.models.Vpn import KeeneticVpn

ROUTERS = "routers"
DEVICES = "devices"
VPN = "vpn"
LOG_RULES = "log_rules"
JOURNAL = "journal"
PLUGIN_NAME = "Keenetic"
_ENTITY_AUTHORING_PROMPT = "osys_keenetic_entity_authoring"

_ROUTER_WRITABLE_FIELDS = (
    "title",
    "ip",
    "port",
    "login",
    "password",
    "linked_object",
    "linked_method",
    "poll_log",
    "poll_vpn",
    "log_to_file",
    "icon",
    "sync_live",
)
_DEVICE_WRITABLE_FIELDS = (
    "title",
    "icon",
    "linked_object",
    "sync_live",
)
_VPN_WRITABLE_FIELDS = ("title", "icon", "linked_object", "linked_method", "sync_live")
_LOG_RULE_WRITABLE_FIELDS = (
    "router_id",
    "title",
    "pattern",
    "write_to_file",
    "linked_object",
    "linked_method",
    "active",
)
_ROUTER_READONLY_FIELDS = (
    "id",
    "model",
    "online",
    "updated",
    "firmware_version",
    "update_available",
    "update_version",
    "cpu",
    "ram",
    "uptime",
)
_DEVICE_READONLY_FIELDS = (
    "id",
    "router_id",
    "mac",
    "ip",
    "online",
    "updated",
    "hostname",
    "interface",
    "ssid",
    "ap",
    "registered",
    "access",
    "device_hint",
    "rssi",
    "rxbytes",
    "txbytes",
    "uptime",
)
_VPN_READONLY_FIELDS = (
    "id",
    "router_id",
    "key",
    "role",
    "vpn_type",
    "online",
    "address",
    "ip",
    "clients_online",
    "updated",
)
_LOG_RULE_READONLY_FIELDS = ("id",)

_PLUGIN_NOTES = [
    "Keenetic monitors routers/devices/VPN and can push live metrics to linked objects via sync_live.",
    "Volatile metrics stay in memory; DB stores stable fields and bindings.",
    "password is write-only: sent on upsert, never returned by list/get.",
    "Operations: poll_now, reboot, wake, set_access, set_name, set_policy, save_config, check_firmware, apply_update, vpn_connect, vpn_disconnect, vpn_enable, vpn_disable, vpn_kick.",
    "IMPORTANT: devices and vpn rows are discovered only by router poll - do NOT create them via upsert. Upsert requires entity_id and only updates bindings (linked_object/method, title, icon, sync_live).",
    "VPN server linked_method is called on client connect/disconnect with USER, IP, EVENT, REMOTE; disconnect includes RXBYTES/TXBYTES.",
    "Optional router poll_log: snapshot journal each poll; Log tab and MCP collection journal expose in-memory buffer.",
    "Optional router poll_vpn: discover/update VPN tunnels and servers each poll (sessions for VPN servers).",
    "Collection journal: read-only in-memory cache (filter router_id). Entity id format r{router_id}#{log_id}.",
    "Plugin config journal_buffer_limit (10..5000, default 200): max lines kept per router in memory.",
    "Collection log_rules: CRUD allowed. Upsert with entity_id updates the existing rule in place (does not delete/recreate). Omit entity_id only to create a new rule.",
    "Without active log_rules: log_to_file writes all new lines; router linked_method is called for each line (legacy).",
    "With active log_rules: method calls only from matching rules; file write only for matches with write_to_file (when router log_to_file is on).",
    "Firmware check calls linked_object.linked_method once per new update_version with EVENT=firmware_update, FIRMWARE_VERSION, UPDATE_VERSION.",
    "Critical events use addNotify: firmware update (Warning).",
    "Deleting a router also deletes its devices, vpn, and log_rules rows.",
]


def _plugin_instance():
    try:
        from app.core.main.PluginsHelper import plugins

        return plugins.get(PLUGIN_NAME, {}).get("instance")
    except Exception:
        return None


def mcp_capabilities() -> dict:
    return {
        "mcp_version": 1,
        "entities": True,
        "config_schema": True,
        "notes": list(_PLUGIN_NOTES),
        "collections": [
            {
                "id": ROUTERS,
                "title": "Keenetic Routers",
                "binding_mode": "object",
                "writable": True,
                "has_code": False,
                "list_filters": ["query", "linked_object", "has_linked_object"],
                "default_sort": "title asc, id asc",
                "writable_fields": list(_ROUTER_WRITABLE_FIELDS),
                "description": "Keenetic router connections with optional live sync and firmware status.",
            },
            {
                "id": DEVICES,
                "title": "Keenetic LAN Devices",
                "binding_mode": "object",
                "writable": True,
                "has_code": False,
                "list_filters": ["query", "router_id", "linked_object", "has_linked_object"],
                "default_sort": "title asc, id asc",
                "writable_fields": list(_DEVICE_WRITABLE_FIELDS),
                "description": (
                    "LAN clients discovered by router poll only - do not create via upsert. "
                    "Provide entity_id to update title/icon/linked_object/sync_live."
                ),
            },
            {
                "id": VPN,
                "title": "Keenetic VPN",
                "binding_mode": "object",
                "writable": True,
                "has_code": False,
                "list_filters": ["query", "router_id", "linked_object", "has_linked_object"],
                "default_sort": "title asc, id asc",
                "writable_fields": list(_VPN_WRITABLE_FIELDS),
                "description": (
                    "VPN tunnels/servers discovered by router poll only - do not create via upsert. "
                    "Provide entity_id to update title/icon/linked_object/linked_method/sync_live."
                ),
            },
            {
                "id": LOG_RULES,
                "title": "Keenetic Log Rules",
                "binding_mode": "object",
                "writable": True,
                "has_code": False,
                "list_filters": ["query", "router_id", "linked_object", "has_linked_object"],
                "default_sort": "title asc, id asc",
                "writable_fields": list(_LOG_RULE_WRITABLE_FIELDS),
                "description": (
                    "Regexp journal rules. Upsert with entity_id updates the row in place; "
                    "omit entity_id to create a new rule."
                ),
            },
            {
                "id": JOURNAL,
                "title": "Keenetic Journal Buffer",
                "binding_mode": "none",
                "writable": False,
                "has_code": False,
                "list_filters": ["query", "router_id"],
                "default_sort": "time desc",
                "description": (
                    "In-memory journal lines from poll_log (not the file on disk). "
                    "Size capped by plugin config journal_buffer_limit."
                ),
            },
        ],
        "operations": [
            "poll_now",
            "reboot",
            "wake",
            "set_access",
            "set_name",
            "set_policy",
            "save_config",
            "check_firmware",
            "apply_update",
            "vpn_connect",
            "vpn_disconnect",
            "vpn_enable",
            "vpn_disable",
            "vpn_kick",
        ],
        "operation_schemas": {
            "poll_now": {"description": "Force poll", "params": {"type": "object", "properties": {}}},
            "reboot": {
                "description": "Reboot router",
                "params": {
                    "type": "object",
                    "properties": {
                        "router_id": {"type": "integer"},
                        "interval": {"type": "integer"},
                    },
                    "required": ["router_id"],
                },
            },
            "wake": {
                "description": "Wake-on-LAN via router",
                "params": {
                    "type": "object",
                    "properties": {"device_id": {"type": "integer"}},
                    "required": ["device_id"],
                },
            },
            "set_access": {
                "description": "Permit or deny host",
                "params": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "integer"},
                        "access": {"type": "string", "enum": ["permit", "deny"]},
                    },
                    "required": ["device_id", "access"],
                },
            },
            "set_name": {
                "description": "Rename known host",
                "params": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "integer"},
                        "name": {"type": "string"},
                    },
                    "required": ["device_id", "name"],
                },
            },
            "set_policy": {
                "description": "Set or clear host policy",
                "params": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "integer"},
                        "policy": {"type": "string"},
                    },
                    "required": ["device_id"],
                },
            },
            "save_config": {
                "description": "Save router configuration",
                "params": {
                    "type": "object",
                    "properties": {"router_id": {"type": "integer"}},
                    "required": ["router_id"],
                },
            },
            "check_firmware": {
                "description": "Check firmware version/update",
                "params": {
                    "type": "object",
                    "properties": {"router_id": {"type": "integer"}},
                    "required": ["router_id"],
                },
            },
            "apply_update": {
                "description": "Apply available firmware update",
                "params": {
                    "type": "object",
                    "properties": {"router_id": {"type": "integer"}},
                    "required": ["router_id"],
                },
            },
            "vpn_connect": {
                "description": "Bring VPN client tunnel up",
                "params": {
                    "type": "object",
                    "properties": {
                        "vpn_id": {"type": "integer"},
                        "router_id": {"type": "integer"},
                        "key": {"type": "string"},
                    },
                },
            },
            "vpn_disconnect": {
                "description": "Bring VPN client tunnel down",
                "params": {
                    "type": "object",
                    "properties": {
                        "vpn_id": {"type": "integer"},
                        "router_id": {"type": "integer"},
                        "key": {"type": "string"},
                    },
                },
            },
            "vpn_enable": {
                "description": "Enable built-in VPN server (PPTP/SSTP/…)",
                "params": {
                    "type": "object",
                    "properties": {
                        "vpn_id": {"type": "integer"},
                        "router_id": {"type": "integer"},
                        "key": {"type": "string"},
                    },
                },
            },
            "vpn_disable": {
                "description": "Disable built-in VPN server (PPTP/SSTP/…)",
                "params": {
                    "type": "object",
                    "properties": {
                        "vpn_id": {"type": "integer"},
                        "router_id": {"type": "integer"},
                        "key": {"type": "string"},
                    },
                },
            },
            "vpn_kick": {
                "description": "Disconnect one VPN server client session (session-logout)",
                "params": {
                    "type": "object",
                    "properties": {
                        "vpn_id": {"type": "integer"},
                        "session_id": {"type": ["integer", "string"]},
                        "session": {"type": ["integer", "string"]},
                    },
                    "required": ["vpn_id"],
                },
            },
        },
    }


def mcp_config_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "interval": {
                "type": "number",
                "default": 5,
                "description": "Router poll interval in seconds",
            },
            "firmware_check_interval": {
                "type": "number",
                "default": 3600,
                "description": "Firmware check interval in seconds",
            },
            "journal_buffer_limit": {
                "type": "integer",
                "default": 200,
                "minimum": 10,
                "maximum": 5000,
                "description": "Max in-memory journal lines per router (Log tab and MCP journal collection)",
            },
        },
    }


def _collection_meta(collection: str) -> dict:
    for item in mcp_capabilities()["collections"]:
        if item["id"] == collection:
            return item
    raise ValueError(f"Unsupported collection: {collection}")


def _parse_optional_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _mask_router(data: dict) -> dict:
    out = dict(data)
    out.pop("password", None)
    out.pop("last_pushed", None)
    out.pop("sessions", None)
    # Runtime-only journal state (sets are not JSON-serializable)
    out.pop("log_seen", None)
    out.pop("log_entries", None)
    out.pop("log_baseline_done", None)
    out.pop("firmware_notified_version", None)
    return out


def _sanitize_entity_row(row: dict) -> dict:
    """Drop non-JSON runtime fields before MCP serialization."""
    if "password" in row or "poll_log" in row or "poll_vpn" in row or "log_to_file" in row or "firmware_version" in row:
        return _mask_router(row)
    out = dict(row)
    out.pop("last_pushed", None)
    out.pop("sessions", None)
    out.pop("sessions_by_id", None)
    return out


def _router_to_dict(row: Router) -> dict:
    data = row2dict(row)
    data.pop("password", None)
    # online/updated are runtime-only (merged by plugin instance)
    data.pop("online", None)
    data.pop("updated", None)
    return data


def _device_to_dict(row: KeeneticDevice) -> dict:
    data = row2dict(row)
    data.pop("online", None)
    data.pop("updated", None)
    return data


def _vpn_to_dict(row: KeeneticVpn) -> dict:
    return row2dict(row)


def _log_rule_to_dict(row: KeeneticLogRule) -> dict:
    return row2dict(row)


def _readonly_fields(collection: str) -> tuple:
    if collection == ROUTERS:
        return _ROUTER_READONLY_FIELDS
    if collection == DEVICES:
        return _DEVICE_READONLY_FIELDS
    if collection == VPN:
        return _VPN_READONLY_FIELDS
    if collection == LOG_RULES:
        return _LOG_RULE_READONLY_FIELDS
    return ("id",)


def _writable_fields(collection: str) -> tuple:
    if collection == ROUTERS:
        return _ROUTER_WRITABLE_FIELDS
    if collection == DEVICES:
        return _DEVICE_WRITABLE_FIELDS
    if collection == VPN:
        return _VPN_WRITABLE_FIELDS
    if collection == LOG_RULES:
        return _LOG_RULE_WRITABLE_FIELDS
    return ()


def _merge_payload(collection: str, payload: dict, entity_id=None) -> dict:
    merged = dict(payload or {})
    if entity_id in (None, ""):
        return merged
    try:
        current = mcp_get_entity(collection, entity_id)
    except ValueError:
        return merged
    for field in _writable_fields(collection):
        if field == "password":
            continue
        if field not in merged and field in current:
            merged[field] = current[field]
    return merged


def mcp_entity_schema(collection: str) -> dict:
    _collection_meta(collection)
    if collection == ROUTERS:
        return {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "readOnly": True},
                "title": {"type": "string"},
                "ip": {"type": "string"},
                "port": {"type": "integer", "default": 80},
                "login": {"type": "string"},
                "password": {"type": "string", "writeOnly": True},
                "linked_object": {"type": "string"},
                "linked_method": {
                    "type": "string",
                    "description": "Called for firmware_update; also for each journal line when no active log_rules (legacy)",
                },
                "poll_log": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": "1 = poll router journal each cycle",
                },
                "poll_vpn": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": "1 = poll VPN tunnels/servers each cycle",
                },
                "log_to_file": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": "1 = allow writing matched log_rules (write_to_file) to logs/KeeneticJournal_<id>.log",
                },
                "icon": {"type": "string"},
                "sync_live": {"type": "string"},
                "model": {"type": "string", "readOnly": True},
                "firmware_version": {"type": "string", "readOnly": True},
                "online": {"type": "integer", "readOnly": True},
                "update_available": {"type": "integer", "readOnly": True},
                "updated": {"type": "string", "readOnly": True},
            },
            "required": ["title", "ip"],
        }
    if collection == DEVICES:
        return {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "readOnly": True},
                "router_id": {
                    "type": "integer",
                    "readOnly": True,
                    "description": "Set by poll discovery; not writable via upsert",
                },
                "title": {"type": "string"},
                "ip": {"type": "string", "readOnly": True},
                "mac": {
                    "type": "string",
                    "readOnly": True,
                    "description": "Discovered by poll; devices cannot be created via MCP",
                },
                "linked_object": {"type": "string"},
                "icon": {"type": "string"},
                "sync_live": {"type": "string"},
                "online": {"type": "integer", "readOnly": True},
                "updated": {"type": "string", "readOnly": True},
            },
            "required": [],
        }
    if collection == VPN:
        return {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "readOnly": True},
                "router_id": {"type": "integer", "readOnly": True},
                "key": {"type": "string", "readOnly": True},
                "role": {"type": "string", "readOnly": True},
                "vpn_type": {"type": "string", "readOnly": True},
                "title": {"type": "string"},
                "icon": {"type": "string"},
                "linked_object": {"type": "string"},
                "linked_method": {
                    "type": "string",
                    "description": "Called on VPN client connect/disconnect with USER, IP, EVENT; disconnect also has RXBYTES/TXBYTES",
                },
                "sync_live": {"type": "string"},
                "online": {"type": "integer", "readOnly": True},
                "ip": {"type": "string", "readOnly": True},
                "clients_online": {"type": "integer", "readOnly": True},
            },
            "required": [],
        }
    if collection == LOG_RULES:
        return {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "readOnly": True},
                "router_id": {"type": "integer"},
                "title": {"type": "string"},
                "pattern": {
                    "type": "string",
                    "description": "Regexp matched against 'level facility message'",
                },
                "write_to_file": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": "1 = write matching lines to journal file only when router log_to_file is on (master switch)",
                },
                "linked_object": {
                    "type": "string",
                    "description": "Object that owns linked_method; required to call a method on match",
                },
                "linked_method": {
                    "type": "string",
                    "description": "Called on match with EVENT=log, RULE_ID, RULE_TITLE, PATTERN",
                },
                "active": {"type": "integer", "enum": [0, 1]},
            },
            "required": ["router_id", "title", "pattern"],
        }
    if collection == JOURNAL:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "readOnly": True,
                    "description": "Composite id r{router_id}#{log_id}",
                },
                "router_id": {"type": "integer", "readOnly": True},
                "log_id": {"type": "string", "readOnly": True},
                "time": {"type": "string", "readOnly": True},
                "level": {"type": "string", "readOnly": True},
                "label": {"type": "string", "readOnly": True},
                "facility": {"type": "string", "readOnly": True},
                "message": {"type": "string", "readOnly": True},
                "repeated": {"type": "integer", "readOnly": True},
            },
            "required": ["id", "router_id", "log_id"],
        }
    raise ValueError(f"Unsupported collection: {collection}")


def _filter_rows(rows: List[dict], query=None, linked_object=None, has_linked_object=None, limit=100):
    linked_obj = str(linked_object or "").strip()
    binding_filter = _parse_optional_bool(has_linked_object)
    q = (query or "").strip().lower()
    result = []
    for row in rows:
        if linked_obj and str(row.get("linked_object") or "") != linked_obj:
            continue
        linked = str(row.get("linked_object") or "").strip()
        if binding_filter is True and not linked:
            continue
        if binding_filter is False and linked:
            continue
        if q:
            hay = " ".join(str(v or "") for v in row.values()).lower()
            if q not in hay:
                continue
        result.append(_sanitize_entity_row(row))
        if len(result) >= limit:
            break
    return result


def mcp_list_entities(
    collection: str,
    query: str = None,
    limit: int = 100,
    router_id: Optional[int] = None,
    linked_object: Optional[str] = None,
    has_linked_object: Optional[bool] = None,
) -> List[dict]:
    limit = max(1, min(int(limit or 100), 5000))
    instance = _plugin_instance()
    if instance is not None:
        if collection == ROUTERS:
            rows = instance.list_merged_routers()
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
        if collection == DEVICES:
            rows = instance.list_merged_devices(int(router_id) if router_id not in (None, "") else None)
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
        if collection == VPN:
            rows = instance.list_merged_vpns(int(router_id) if router_id not in (None, "") else None)
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
        if collection == LOG_RULES:
            rows = instance.list_log_rules(int(router_id) if router_id not in (None, "") else None)
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
        if collection == JOURNAL:
            return instance.list_journal_entries(
                router_id=int(router_id) if router_id not in (None, "") else None,
                query=query,
                limit=limit,
            )

    # fallback DB-only
    if collection == ROUTERS:
        with session_scope() as session:
            rows = [_router_to_dict(row) for row in session.query(Router).all()]
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
    if collection == DEVICES:
        with session_scope() as session:
            q = session.query(KeeneticDevice)
            if router_id not in (None, ""):
                q = q.filter(KeeneticDevice.router_id == int(router_id))
            rows = [_device_to_dict(row) for row in q.all()]
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
    if collection == VPN:
        with session_scope() as session:
            q = session.query(KeeneticVpn)
            if router_id not in (None, ""):
                q = q.filter(KeeneticVpn.router_id == int(router_id))
            rows = [_vpn_to_dict(row) for row in q.all()]
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
    if collection == LOG_RULES:
        with session_scope() as session:
            q = session.query(KeeneticLogRule)
            if router_id not in (None, ""):
                q = q.filter(KeeneticLogRule.router_id == int(router_id))
            rows = [_log_rule_to_dict(row) for row in q.all()]
            return _filter_rows(rows, query, linked_object, has_linked_object, limit)
    if collection == JOURNAL:
        raise ValueError("journal collection requires the Keenetic plugin instance")
    raise ValueError(f"Unsupported collection: {collection}")


def mcp_get_entity(collection: str, entity_id) -> dict:
    instance = _plugin_instance()
    if instance is not None:
        if collection == ROUTERS:
            data = instance.merge_router(int(entity_id))
            if not data:
                raise ValueError(f"Router not found: {entity_id}")
            return _mask_router(data)
        if collection == DEVICES:
            data = instance.merge_device(int(entity_id))
            if not data:
                raise ValueError(f"Device not found: {entity_id}")
            return data
        if collection == VPN:
            data = instance.merge_vpn(int(entity_id))
            if not data:
                raise ValueError(f"VPN not found: {entity_id}")
            return data
        if collection == LOG_RULES:
            data = instance.get_log_rule(int(entity_id))
            if not data:
                raise ValueError(f"Log rule not found: {entity_id}")
            return data
        if collection == JOURNAL:
            return instance.get_journal_entry(entity_id)
    with session_scope() as session:
        if collection == ROUTERS:
            row = session.query(Router).filter(Router.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"Router not found: {entity_id}")
            return _router_to_dict(row)
        if collection == DEVICES:
            row = session.query(KeeneticDevice).filter(KeeneticDevice.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"Device not found: {entity_id}")
            return _device_to_dict(row)
        if collection == VPN:
            row = session.query(KeeneticVpn).filter(KeeneticVpn.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"VPN not found: {entity_id}")
            return _vpn_to_dict(row)
        if collection == LOG_RULES:
            row = session.query(KeeneticLogRule).filter(KeeneticLogRule.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"Log rule not found: {entity_id}")
            return _log_rule_to_dict(row)
    if collection == JOURNAL:
        raise ValueError("journal collection requires the Keenetic plugin instance")
    raise ValueError(f"Unsupported collection: {collection}")


def mcp_upsert_entity(collection: str, payload: dict, entity_id=None) -> dict:
    meta = _collection_meta(collection)
    if not meta.get("writable"):
        raise ValueError(f"Collection '{collection}' is read-only")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    clean_payload = dict(payload)
    for field in _readonly_fields(collection):
        clean_payload.pop(field, None)

    validation = mcp_validate_entity(collection, clean_payload, entity_id=entity_id)
    if not validation.get("ok"):
        raise ValueError(f"validation failed: {validation}")

    merged = _merge_payload(collection, clean_payload, entity_id=entity_id)
    instance = _plugin_instance()

    if collection == ROUTERS:
        with session_scope() as session:
            if entity_id not in (None, ""):
                row = session.query(Router).filter(Router.id == int(entity_id)).one_or_none()
                if row is None:
                    raise ValueError(f"Router not found: {entity_id}")
            else:
                ip = str(merged.get("ip") or "").strip()
                port = merged.get("port") or 80
                row = (
                    session.query(Router)
                    .filter(Router.ip == ip, Router.port == int(port))
                    .order_by(Router.id)
                    .first()
                    if ip
                    else None
                )
                if row is None:
                    row = Router()
                    session.add(row)
            for field in ("title", "ip", "login", "icon", "sync_live"):
                if field in merged:
                    setattr(row, field, merged.get(field))
            if "password" in clean_payload:
                row.password = clean_payload.get("password")
            if "port" in merged and merged["port"] is not None:
                row.port = int(merged["port"])
            elif entity_id in (None, "") and row.port is None:
                row.port = 80
            if "linked_object" in merged:
                linked = str(merged.get("linked_object") or "").strip() or None
                if linked:
                    ok, err = sync_object_link(linked)
                    if not ok:
                        raise ValueError(err or "object link validation failed")
                row.linked_object = linked
            if "linked_method" in merged:
                row.linked_method = str(merged.get("linked_method") or "").strip() or None
            if "poll_log" in merged:
                row.poll_log = 1 if merged.get("poll_log") in (1, "1", True, "true", "True") else 0
            if "poll_vpn" in merged:
                row.poll_vpn = 1 if merged.get("poll_vpn") in (1, "1", True, "true", "True") else 0
            if "log_to_file" in merged:
                row.log_to_file = 1 if merged.get("log_to_file") in (1, "1", True, "true", "True") else 0
            if not row.icon:
                row.icon = default_router_icon()
            session.commit()
            cache_data = row2dict(row)
            for key, value in list(cache_data.items()):
                if isinstance(value, datetime):
                    cache_data[key] = value.isoformat(sep=" ", timespec="seconds")
            data = _router_to_dict(row)
            router_pk = int(row.id)
            poll_log_on = bool(row.poll_log)
        if instance is not None:
            instance._cache_upsert_router(cache_data)
            # Reset journal baseline when enabling poll_log so next snapshot is quiet
            if "poll_log" in merged and poll_log_on:
                with instance._cache_lock:
                    rt = instance.router_runtime.setdefault(router_pk, {})
                    rt["log_baseline_done"] = False
                    rt["log_seen"] = set()
                    rt["log_entries"] = []
        return data

    if collection == DEVICES:
        if entity_id in (None, ""):
            raise ValueError(
                "LAN devices are discovered on poll; provide entity_id to update binding "
                "(title/icon/linked_object/sync_live). Do not create devices via upsert."
            )
        with session_scope() as session:
            row = session.query(KeeneticDevice).filter(KeeneticDevice.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"Device not found: {entity_id}")
            for field in ("title", "icon", "sync_live"):
                if field in merged:
                    setattr(row, field, merged.get(field))
            if "linked_object" in merged:
                linked = str(merged.get("linked_object") or "").strip() or None
                if linked:
                    ok, err = sync_object_link(linked)
                    if not ok:
                        raise ValueError(err or "object link validation failed")
                row.linked_object = linked
            session.commit()
            data = _device_to_dict(row)
        if instance is not None:
            instance._cache_upsert_device(data)
        return data

    if collection == VPN:
        if entity_id in (None, ""):
            raise ValueError(
                "VPN entities are discovered on poll; provide entity_id to update binding "
                "(title/icon/linked_object/linked_method/sync_live). Do not create VPN via upsert."
            )
        with session_scope() as session:
            row = session.query(KeeneticVpn).filter(KeeneticVpn.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"VPN not found: {entity_id}")
            for field in ("title", "icon", "sync_live"):
                if field in merged:
                    setattr(row, field, merged.get(field))
            if "linked_object" in merged:
                linked = str(merged.get("linked_object") or "").strip() or None
                if linked:
                    ok, err = sync_object_link(linked)
                    if not ok:
                        raise ValueError(err or "object link validation failed")
                row.linked_object = linked
            if "linked_method" in merged:
                row.linked_method = str(merged.get("linked_method") or "").strip() or None
            if not row.icon:
                row.icon = default_vpn_icon(row.role or "client")
            session.commit()
            data = _vpn_to_dict(row)
        if instance is not None:
            instance._cache_upsert_vpn(data)
        return data

    if collection == LOG_RULES:
        with session_scope() as session:
            if entity_id not in (None, ""):
                row = session.query(KeeneticLogRule).filter(KeeneticLogRule.id == int(entity_id)).one_or_none()
                if row is None:
                    raise ValueError(f"Log rule not found: {entity_id}")
            else:
                row = KeeneticLogRule()
                session.add(row)
            if "router_id" in merged:
                row.router_id = int(merged["router_id"])
            for field in ("title", "pattern"):
                if field in merged:
                    setattr(row, field, str(merged.get(field) or "").strip() or None)
            if "linked_object" in merged:
                linked = str(merged.get("linked_object") or "").strip() or None
                if linked:
                    ok, err = sync_object_link(linked)
                    if not ok:
                        raise ValueError(err or "object link validation failed")
                row.linked_object = linked
            if "linked_method" in merged:
                row.linked_method = str(merged.get("linked_method") or "").strip() or None
            if "write_to_file" in merged:
                row.write_to_file = (
                    1 if merged.get("write_to_file") in (1, "1", True, "true", "True") else 0
                )
            elif entity_id in (None, "") and row.write_to_file is None:
                row.write_to_file = 0
            if "active" in merged:
                row.active = 1 if merged.get("active") in (1, "1", True, "true", "True") else 0
            elif entity_id in (None, "") and row.active is None:
                row.active = 1
            session.commit()
            data = _log_rule_to_dict(row)
        if instance is not None:
            instance._cache_upsert_log_rule(data)
        return data

    raise ValueError(f"Unsupported collection: {collection}")


def mcp_delete_entity(collection: str, entity_id) -> bool:
    meta = _collection_meta(collection)
    if not meta.get("writable"):
        raise ValueError(f"Collection '{collection}' is read-only")
    instance = _plugin_instance()
    with session_scope() as session:
        if collection == ROUTERS:
            session.execute(delete(KeeneticDevice).where(KeeneticDevice.router_id == int(entity_id)))
            session.execute(delete(KeeneticVpn).where(KeeneticVpn.router_id == int(entity_id)))
            session.execute(delete(KeeneticLogRule).where(KeeneticLogRule.router_id == int(entity_id)))
            session.execute(delete(Router).where(Router.id == int(entity_id)))
            session.commit()
            if instance is not None:
                instance._cache_delete_router(int(entity_id))
            return True
        if collection == DEVICES:
            session.execute(delete(KeeneticDevice).where(KeeneticDevice.id == int(entity_id)))
            session.commit()
            if instance is not None:
                instance._cache_delete_device(int(entity_id))
            return True
        if collection == VPN:
            session.execute(delete(KeeneticVpn).where(KeeneticVpn.id == int(entity_id)))
            session.commit()
            if instance is not None:
                instance._cache_delete_vpn(int(entity_id))
            return True
        if collection == LOG_RULES:
            session.execute(delete(KeeneticLogRule).where(KeeneticLogRule.id == int(entity_id)))
            session.commit()
            if instance is not None:
                instance._cache_delete_log_rule(int(entity_id))
            return True
    raise ValueError(f"Unsupported collection: {collection}")


def mcp_validate_entity_code(collection: str, code: str) -> dict:
    raise ValueError(f"Collection '{collection}' does not support code validation")


def mcp_run_entity_dry(collection: str, code: str, context: dict = None) -> dict:
    raise ValueError(f"Collection '{collection}' does not support dry-run code")


def mcp_invoke(operation: str, params: dict = None) -> dict:
    instance = _plugin_instance()
    if instance is None:
        raise ValueError("Keenetic plugin not loaded")
    return instance.invoke_operation(operation, params or {})


def mcp_descriptors() -> Tuple[list, list, list]:
    return build_plugin_mcp_descriptors(PLUGIN_NAME, mcp_capabilities())


def mcp_get_prompt(name: str, arguments: dict = None) -> dict:
    arguments = arguments or {}
    if name != _ENTITY_AUTHORING_PROMPT:
        raise ValueError(f"Unsupported prompt: {name}")
    task = str(arguments.get("task") or "").strip()
    collection = str(arguments.get("collection") or ROUTERS).strip()
    if not task:
        raise ValueError("task is required")
    notes_block = "\n".join(f"- {note}" for note in _PLUGIN_NOTES)
    prompt_text = (
        "Create Keenetic plugin entity payload by schema.\n"
        f"Plugin: {PLUGIN_NAME}\nCollection: {collection}\nTask: {task}\n\n"
        f"Plugin notes:\n{notes_block}\n"
    )
    return {"messages": [{"role": "user", "content": {"type": "text", "text": prompt_text}}]}


def mcp_entity_revision(collection: str, entity_id) -> str:
    entity = mcp_get_entity(collection, entity_id)
    updated = revision_from_datetime(entity.get("updated"))
    if updated:
        return updated
    if collection == LOG_RULES:
        return revision_from_dict(
            entity, keys=["id", "title", "pattern", "linked_object", "linked_method", "active", "write_to_file"]
        )
    if collection == JOURNAL:
        return revision_from_dict(entity, keys=["id", "time", "level", "message", "repeated"])
    return revision_from_dict(entity, keys=["id", "title", "linked_object", "sync_live"])


def mcp_validate_entity(collection: str, payload: dict, entity_id=None) -> dict:
    if collection == JOURNAL:
        return {"ok": False, "errors": [{"field": "_", "message": "journal is read-only"}]}
    if collection not in (ROUTERS, DEVICES, VPN, LOG_RULES):
        raise ValueError(f"Unsupported collection: {collection}")
    if not isinstance(payload, dict):
        return {"ok": False, "errors": [{"field": "_", "message": "payload must be an object"}]}

    merged = _merge_payload(collection, payload, entity_id=entity_id)
    schema = mcp_entity_schema(collection)
    result = validate_entity_payload(merged, schema)
    if not result.get("ok"):
        return result

    errors = list(result.get("errors") or [])
    warnings: List[dict] = []

    disallowed = [key for key in payload if key in _readonly_fields(collection)]
    if disallowed:
        return {"ok": False, "errors": [{"field": disallowed[0], "message": "field is read-only"}]}

    linked = str(merged.get("linked_object") or "").strip()
    if linked and not validate_object_exists(linked):
        errors.append({"field": "linked_object", "message": f"Object not found: {linked}"})

    if collection == ROUTERS:
        port = merged.get("port")
        if port is not None:
            try:
                port_int = int(port)
                if port_int <= 0 or port_int > 65535:
                    errors.append({"field": "port", "message": "must be between 1 and 65535"})
            except (TypeError, ValueError):
                errors.append({"field": "port", "message": "must be an integer"})
        if entity_id not in (None, ""):
            with session_scope() as session:
                row = session.query(Router).filter(Router.id == int(entity_id)).one_or_none()
                if row is None:
                    errors.append({"field": "id", "message": f"Router not found: {entity_id}"})

    if collection == DEVICES:
        if entity_id in (None, ""):
            errors.append(
                {
                    "field": "id",
                    "message": (
                        "devices are discovered on poll only; provide entity_id to update "
                        "(do not create via upsert)"
                    ),
                }
            )
        else:
            with session_scope() as session:
                row = session.query(KeeneticDevice).filter(KeeneticDevice.id == int(entity_id)).one_or_none()
                if row is None:
                    errors.append({"field": "id", "message": f"Device not found: {entity_id}"})

    if collection == VPN:
        if entity_id in (None, ""):
            errors.append(
                {
                    "field": "id",
                    "message": (
                        "vpn entities are discovered on poll only; provide entity_id to update "
                        "(do not create via upsert)"
                    ),
                }
            )
        else:
            with session_scope() as session:
                row = session.query(KeeneticVpn).filter(KeeneticVpn.id == int(entity_id)).one_or_none()
                if row is None:
                    errors.append({"field": "id", "message": f"VPN not found: {entity_id}"})

    if collection == LOG_RULES:
        router_id = merged.get("router_id")
        if router_id in (None, "") and entity_id in (None, ""):
            errors.append({"field": "router_id", "message": "required"})
        elif router_id not in (None, ""):
            try:
                router_pk = int(router_id)
            except (TypeError, ValueError):
                errors.append({"field": "router_id", "message": "must be an integer"})
                router_pk = None
            if router_pk is not None:
                with session_scope() as session:
                    router = session.query(Router).filter(Router.id == router_pk).one_or_none()
                    if router is None:
                        errors.append({"field": "router_id", "message": f"Router not found: {router_pk}"})
        pattern = str(merged.get("pattern") or "").strip()
        if not pattern and entity_id in (None, ""):
            errors.append({"field": "pattern", "message": "required"})
        elif pattern:
            import re

            try:
                re.compile(pattern)
            except re.error as ex:
                errors.append({"field": "pattern", "message": f"invalid regexp: {ex}"})
        if entity_id not in (None, ""):
            with session_scope() as session:
                row = session.query(KeeneticLogRule).filter(KeeneticLogRule.id == int(entity_id)).one_or_none()
                if row is None:
                    errors.append({"field": "id", "message": f"Log rule not found: {entity_id}"})

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    response = {"ok": True, "errors": []}
    if warnings:
        response["warnings"] = warnings
    return response
