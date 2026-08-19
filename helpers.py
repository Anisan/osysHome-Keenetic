"""Helpers for Keenetic poll parsing, icons, and sync_live."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Pattern, Set, Tuple

# Compiled regexp cache: pattern string → (compiled|None, error message)
_LOG_RULE_RE_CACHE: Dict[str, Tuple[Optional[Pattern[str]], str]] = {}


def log_entry_match_text(entry: dict) -> str:
    """Text used for log-rule regexp matching: level + facility + message."""
    level = str((entry or {}).get("level") or "").strip()
    facility = str((entry or {}).get("facility") or "").strip()
    message = str((entry or {}).get("message") or "").strip()
    return " ".join(p for p in (level, facility, message) if p)


def compile_log_rule_pattern(pattern: str) -> Tuple[Optional[Pattern[str]], str]:
    """Compile regexp; cache results. Returns (compiled, error)."""
    text = str(pattern or "")
    cached = _LOG_RULE_RE_CACHE.get(text)
    if cached is not None:
        return cached
    try:
        compiled: Optional[Pattern[str]] = re.compile(text)
        err = ""
    except re.error as ex:
        compiled = None
        err = str(ex)
    _LOG_RULE_RE_CACHE[text] = (compiled, err)
    return compiled, err


def match_log_rule(pattern: str, entry: dict) -> bool:
    """True if pattern matches entry text. Invalid patterns → False."""
    compiled, err = compile_log_rule_pattern(pattern)
    if compiled is None:
        return False
    return compiled.search(log_entry_match_text(entry)) is not None


VPN_INTERFACE_TYPES = {
    "wireguard",
    "openvpn",
    "ipsec",
    "l2tp",
    "pptp",
    "sstp",
    "zerotier",
    "ike",
    "vpn",
    "sstpserver",
    "vpnserver",
    "pptpserver",
    "l2tpserver",
    "ikev2",
}

VPN_SERVER_HINTS = (
    "server",
    "sstpserver",
    "vpnserver",
    "pptpserver",
    "l2tpserver",
    "ikev2-server",
)

DEVICE_SYNC_DEFAULT = "online,ip,signal_strength,rxbytes,txbytes,uptime"
INTERNET_SYNC_DEFAULT = "online,ip,rxbytes,txbytes"
ROUTER_SYNC_DEFAULT = "online,cpu,ram,uptime,firmware_version,update_available,update_version"
VPN_CLIENT_SYNC_DEFAULT = "online,ip"
VPN_SERVER_SYNC_DEFAULT = "online,clients_online"
VPN_SESSION_SYNC_FIELDS = "last_event,last_user,last_ip,last_rxbytes,last_txbytes,last_uptime"
DEFAULT_JOURNAL_BUFFER_LIMIT = 200
LOG_BUFFER_LIMIT = DEFAULT_JOURNAL_BUFFER_LIMIT
LOG_POLL_LIMIT = 100


def parse_sync_live(value: Optional[str], default: Optional[str] = None) -> Set[str]:
    raw = value
    if raw is None:
        raw = default
    if raw is None:
        return set()
    text = str(raw).strip()
    if not text:
        return set()
    return {part.strip() for part in text.split(",") if part.strip()}


def resolve_sync_live(value: Optional[str], linked_object: Optional[str], default: str) -> Set[str]:
    """NULL sync_live + existing linked → default; empty string → no push."""
    linked = str(linked_object or "").strip()
    if not linked:
        return set()
    if value is None:
        return parse_sync_live(default)
    return parse_sync_live(value)


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def interface_name(value: Any) -> str:
    data = _as_dict(value)
    if data:
        return str(data.get("name") or data.get("id") or data.get("description") or "").strip()
    return str(value or "").strip()


def extract_rssi(dev: Any) -> Optional[int]:
    rssi = getattr(dev, "rssi", None)
    if rssi is None:
        mws = _as_dict(getattr(dev, "mws", None))
        rssi = mws.get("rssi")
    if rssi is None:
        return None
    try:
        return int(rssi)
    except (TypeError, ValueError):
        return None


def device_online(dev: Any) -> int:
    link = getattr(dev, "link", None)
    if link is not None:
        return 1 if str(link).lower() == "up" else 0
    active = getattr(dev, "active", None)
    if active is None:
        return 0
    return 1 if bool(active) else 0


def device_hint(dev: Any) -> str:
    ssdp = _as_dict(getattr(dev, "ssdp", None))
    ws = _as_dict(getattr(dev, "ws_discovery", None))
    if ws.get("onvif-device") or ws.get("onvif_device"):
        return "onvif"
    if ssdp:
        model = str(ssdp.get("model") or ssdp.get("type") or ssdp.get("name") or "").strip()
        if model:
            return ("ssdp:" + model)[:100]
    if getattr(dev, "mws", None) or getattr(dev, "mws_backhaul", None):
        return "mws"
    if getattr(dev, "ssid", None) or getattr(dev, "ap", None):
        return "wifi"
    if getattr(dev, "speed", None) or getattr(dev, "port", None):
        return "ethernet"
    return ""


def default_device_icon(title: str = "", mac: str = "", hint: str = "", ssid: str = "", ap: str = "") -> str:
    if mac == "0.0.0.0.0.0" or str(title).lower() == "internet":
        return "fas fa-globe"
    hint_l = str(hint or "").lower()
    title_l = str(title or "").lower()
    if "onvif" in hint_l or "cam" in title_l or "camera" in title_l:
        return "fas fa-video"
    if "ssdp" in hint_l or "tv" in title_l or "renderer" in hint_l:
        return "fas fa-tv"
    if ssid or ap or hint_l in {"wifi", "mws"}:
        return "fas fa-wifi"
    if hint_l == "ethernet":
        return "fas fa-network-wired"
    return "fas fa-laptop"


def default_router_icon() -> str:
    return "fas fa-router"


def default_vpn_icon(role: str = "client") -> str:
    if role == "server":
        return "fas fa-shield-alt"
    return "fas fa-network-wired"


def normalize_mac(mac: Optional[str]) -> str:
    return str(mac or "").strip().lower()


def _normalize_vpn_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _looks_like_vpn_interface(itype: str, key: str, item: dict) -> bool:
    normalized = _normalize_vpn_type(itype)
    if normalized in VPN_INTERFACE_TYPES:
        return True
    if any(token in normalized for token in VPN_INTERFACE_TYPES):
        return True
    blob = " ".join(
        [
            itype,
            key,
            str(item.get("description") or ""),
            str(item.get("interface-name") or ""),
            str(item.get("name") or ""),
        ]
    ).lower()
    return any(token in blob for token in ("wireguard", "openvpn", "sstp", "pptp", "l2tp", "ipsec", "zerotier", "vpn"))


def _vpn_interface_role(itype: str, key: str, item: dict) -> str:
    blob = " ".join(
        [
            itype,
            key,
            str(item.get("description") or ""),
            str(item.get("role") or ""),
        ]
    ).lower().replace("_", "").replace("-", "").replace(" ", "")
    if any(hint in blob for hint in VPN_SERVER_HINTS):
        return "server"
    return "client"


def parse_vpn_interfaces(interfaces: Any) -> List[dict]:
    result: List[dict] = []
    data = _as_dict(interfaces)
    items: Iterable = data.items() if data else []
    if isinstance(interfaces, list):
        items = ((item.get("id") or item.get("name"), item) for item in interfaces if isinstance(item, dict))

    for iface_id, raw in items:
        item = _as_dict(raw)
        itype = str(item.get("type") or "")
        key = str(iface_id or item.get("id") or item.get("interface-name") or item.get("name") or "").strip()
        if not key or not _looks_like_vpn_interface(itype, key, item):
            continue
        summary = _as_dict(item.get("summary"))
        layer = _as_dict(summary.get("layer"))
        conf = str(layer.get("conf") or "").lower()
        state = str(item.get("state") or "").lower()
        connected = item.get("connected")
        if connected is None:
            connected = str(item.get("link") or "").lower() == "up" or state == "up"
        enabled = not (conf == "disabled" or (state == "down" and not connected))
        role = _vpn_interface_role(itype, key, item)
        result.append(
            {
                "key": key,
                "role": role,
                "vpn_type": item.get("type") or itype or key,
                "title": str(item.get("description") or item.get("interface-name") or key),
                "online": 1 if connected else 0,
                "enabled": 1 if enabled else 0,
                "address": str(item.get("address") or ""),
            }
        )
    return result


def _extract_sessions(clients_data: Any) -> List[dict]:
    if clients_data is None:
        return []
    if isinstance(clients_data, dict):
        # Keenetic PPTP/SSTP use "tunnel"; other firmwares use session/clients/peers
        for nested_key in (
            "tunnel",
            "session",
            "sessions",
            "client",
            "clients",
            "host",
            "peer",
            "peers",
        ):
            if nested_key in clients_data:
                return _extract_sessions(clients_data.get(nested_key))
        # Avoid treating server status dict (enabled/ndns-name/…) as a session map
        if any(k in clients_data for k in ("enabled", "running", "type", "ndns-name", "secret", "pool-start", "pool_start")):
            return []
        clients_list = [v for v in clients_data.values() if isinstance(v, dict)]
        if not clients_list:
            return []
    elif isinstance(clients_data, list):
        clients_list = clients_data
    else:
        return []

    sessions = []
    for client in clients_list:
        row = _as_dict(client)
        if not row:
            continue
        # Skip pure RCI status/error rows
        if set(row.keys()) <= {"status", "message", "code", "ident"}:
            continue
        connected = row.get("connected")
        if connected is None:
            connected = row.get("active")
        if connected is None:
            # Active VPN tunnel rows from Keenetic have session-id / username
            connected = True
        sessions.append(
            {
                "name": str(
                    row.get("name")
                    or row.get("peer")
                    or row.get("user")
                    or row.get("username")
                    or row.get("remote")
                    or ""
                ).strip(),
                "address": str(
                    row.get("clientaddress")
                    or row.get("address")
                    or row.get("ip")
                    or row.get("virtual-address")
                    or ""
                ).strip(),
                # Public/WAN side of client when firmware exposes it (often absent for PPTP/SSTP)
                "remote": str(
                    row.get("endpoint")
                    or row.get("peer-address")
                    or row.get("remote-address")
                    or row.get("caller-id")
                    or row.get("source-address")
                    or row.get("from")
                    or ""
                ).strip(),
                "connected": 1 if connected else 0,
                "uptime": row.get("uptime"),
                "session_id": row.get("session-id") or row.get("session_id"),
                "rxbytes": _session_counter(row, "rxbytes", "rx_bytes", "received"),
                "txbytes": _session_counter(row, "txbytes", "tx_bytes", "sent"),
            }
        )
    return sessions


def _session_counter(row: dict, *keys: str) -> int:
    stats = _as_dict(row.get("statistic") or row.get("stats") or row.get("statistics"))
    for key in keys:
        for source in (row, stats):
            value = source.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def session_identity(session: dict) -> str:
    sid = session.get("session_id")
    if sid is not None and str(sid).strip() != "":
        return f"id:{sid}"
    name = str(session.get("name") or "").strip().lower()
    address = str(session.get("address") or "").strip().lower()
    return f"ua:{name}|{address}"


def sessions_by_identity(sessions: Any) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        result[session_identity(session)] = session
    return result


def parse_vpn_server(
    server_data: Any,
    clients_data: Any = None,
    *,
    key: str = "vpn-server",
    vpn_type: str = "vpn-server",
    title: str = "VPN Server",
) -> Optional[dict]:
    data = _as_dict(server_data)
    if not data and clients_data is None:
        return None

    sessions = _extract_sessions(clients_data)
    if not sessions and clients_data is None:
        # Status payload itself often embeds tunnels (Keenetic show vpn-server / sstp-server)
        sessions = _extract_sessions(data)

    enabled = data.get("enabled")
    running = data.get("running")
    if running is None:
        running = data.get("up")
    if running is None and sessions:
        running = True
    if running is None:
        running = enabled
    # PPTP often has no enabled/running flags — presence of show payload means server exists
    if enabled is None and (sessions or data.get("tunnel") is not None or data.get("ndns-name") is not None):
        enabled = True

    # Empty/disabled stub without useful fields — skip
    if not data and not sessions:
        return None

    resolved_type = str(data.get("type") or vpn_type or "vpn-server")
    resolved_title = str(data.get("description") or data.get("name") or title or resolved_type)
    return {
        "key": key,
        "role": "server",
        "vpn_type": resolved_type,
        "title": resolved_title,
        "online": 1 if running else 0,
        "enabled": 1 if (enabled if enabled is not None else running) else 0,
        "address": str(data.get("address") or data.get("interface") or data.get("local") or data.get("ndns-name") or ""),
        "clients_online": sum(1 for s in sessions if s.get("connected")),
        "sessions": sessions,
    }


def stable_device_fields(dev: Any, title: str, mac: str) -> Dict[str, Any]:
    iface = interface_name(getattr(dev, "interface", None))
    ssid = str(getattr(dev, "ssid", None) or "").strip()
    ap = str(getattr(dev, "ap", None) or "").strip()
    mws = _as_dict(getattr(dev, "mws", None))
    if not ap and mws.get("ap"):
        ap = str(mws.get("ap"))
    registered = getattr(dev, "registered", None)
    registered_int = None
    if registered is not None:
        registered_int = 1 if registered in (True, 1, "yes", "true") else 0
    hint = device_hint(dev)
    return {
        "title": title,
        "mac": mac,
        "hostname": str(getattr(dev, "hostname", None) or "").strip() or None,
        "interface": iface or None,
        "ssid": ssid or None,
        "ap": ap or None,
        "registered": registered_int,
        "access": str(getattr(dev, "access", None) or "").strip() or None,
        "device_hint": hint or None,
    }


def live_device_fields(dev: Any) -> Dict[str, Any]:
    return {
        "ip": str(getattr(dev, "ip", None) or ""),
        "online": device_online(dev),
        "active": 1 if getattr(dev, "active", False) else 0,
        "rssi": extract_rssi(dev),
        "rxbytes": getattr(dev, "rxbytes", None) or 0,
        "txbytes": getattr(dev, "txbytes", None) or 0,
        "uptime": getattr(dev, "uptime", None) or 0,
        "speed": getattr(dev, "speed", None),
        "duplex": getattr(dev, "duplex", None),
        "port": getattr(dev, "port", None),
        "txrate": getattr(dev, "txrate", None) or _as_dict(getattr(dev, "mws", None)).get("txrate"),
        "mode": getattr(dev, "mode", None) or _as_dict(getattr(dev, "mws", None)).get("mode"),
        "security": getattr(dev, "security", None) or _as_dict(getattr(dev, "mws", None)).get("security"),
        "last_seen": getattr(dev, "last_seen", None),
    }


def _log_fingerprint(time_v: Any, level: Any, message: Any, facility: Any = "") -> str:
    return f"{time_v}|{level}|{facility}|{message}"


def log_entry_dedup_key(entry: dict) -> str:
    """Stable dedup key for journal entries (content-based, not router id)."""
    row = entry or {}
    time_v = str(row.get("time") or "").strip()
    level = str(row.get("level") or "").strip()
    facility = str(row.get("facility") or "").strip()
    message = str(row.get("message") or "").strip()
    if not message:
        return ""
    return _log_fingerprint(time_v, level, message, facility)


def _unwrap_log_message(message: Any) -> tuple:
    """Keenetic often nests {level,label,message,repeated} inside message."""
    level = ""
    label = ""
    facility = ""
    repeated = None
    text = message
    if isinstance(message, dict):
        level = str(message.get("level") or "").strip()
        label = str(message.get("label") or "").strip()
        repeated = message.get("repeated")
        text = message.get("message") or message.get("text") or message.get("msg") or ""
        if not text and message.get("data") is not None:
            text = message.get("data")
        text = str(text).strip() if text is not None else ""
    else:
        text = str(message or "").strip()
    # Facility often prefixes the text: "Core::Syslog: rest of message"
    if text and ": " in text and not facility:
        head, rest = text.split(": ", 1)
        if "::" in head or "." in head or head[:1].isalpha():
            facility = head.strip()
            text = rest.strip()
    return level, label, facility, text, repeated


def map_keenetic_log_level(level: Any, label: Any = "") -> str:
    """Normalize Keenetic level/label to a canonical name for logging/UI."""
    text = str(level or "").strip().lower()
    lab = str(label or "").strip().upper()
    if not text and lab:
        text = {
            "E": "error",
            "C": "critical",
            "W": "warning",
            "N": "notice",
            "I": "info",
            "D": "debug",
            "T": "debug",
        }.get(lab, "")
    if text in ("err", "error", "fatal", "crit", "critical", "alert", "emerg", "emergency"):
        return "error"
    if text in ("warn", "warning"):
        return "warning"
    if text in ("notice", "not"):
        return "notice"
    if text in ("debug", "trace", "dbg"):
        return "debug"
    if text in ("info", "informational"):
        return "info"
    return text or "info"


def normalize_log_entries(raw: Any) -> List[dict]:
    """Normalize Keenetic show log payload into [{id,time,level,message,facility}, ...]."""
    data = raw
    if isinstance(data, dict):
        nested = data.get("log")
        if isinstance(nested, (dict, list)):
            data = nested
        elif set(data.keys()) <= {"status", "message", "code", "ident", "continued"}:
            return []
    entries: List[dict] = []
    if isinstance(data, list):
        iterable: Iterable[tuple] = ((str(i), row) for i, row in enumerate(data))
    elif isinstance(data, dict):
        iterable = data.items()
    else:
        return []
    for key, row in iterable:
        if not isinstance(row, dict):
            if row is None:
                continue
            level, label, facility, message, repeated = _unwrap_log_message(row)
            if not message:
                continue
            level = map_keenetic_log_level(level, label)
            eid = _log_fingerprint("", level, message, facility)
            entries.append(
                {
                    "id": eid,
                    "time": "",
                    "level": level,
                    "label": label,
                    "message": message,
                    "facility": facility,
                    "repeated": repeated,
                }
            )
            continue
        if set(row.keys()) <= {"status", "message", "code", "ident"} and "level" not in row:
            # May still be a nested log payload under "message"
            if not isinstance(row.get("message"), dict):
                continue
        time_v = row.get("time") or row.get("timestamp") or row.get("date") or ""
        top_level = row.get("level") or row.get("severity") or row.get("type") or ""
        top_label = row.get("label") or ""
        top_facility = row.get("facility") or row.get("source") or row.get("component") or ""
        raw_message = row.get("message") if "message" in row else (row.get("msg") or row.get("text") or "")
        nest_level, nest_label, nest_facility, message, repeated = _unwrap_log_message(raw_message)
        if repeated is None:
            repeated = row.get("repeated")
        level = map_keenetic_log_level(nest_level or top_level, nest_label or top_label)
        label = nest_label or str(top_label or "")
        facility = nest_facility or str(top_facility or "")
        if not message and not time_v:
            continue
        router_id = str(row.get("id") or row.get("uid") or key or "").strip()
        eid = _log_fingerprint(time_v, level, message, facility)
        if not eid and router_id:
            eid = router_id
        entries.append(
            {
                "id": eid,
                "router_log_id": router_id,
                "time": str(time_v) if time_v is not None else "",
                "level": level,
                "label": label,
                "message": message,
                "facility": facility,
                "repeated": repeated,
            }
        )
    return entries
