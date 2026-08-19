# Keenetic — Keenetic router integration

![Keenetic Icon](static/Keenetic.png)

osysHome plugin for Keenetic routers: LAN devices, VPN, firmware, optional system journal, live admin UI, and MCP.

## Features

- **Routers**: multi-router, credentials, CPU/RAM/uptime, firmware check/update, reboot
- **Devices**: discovery, online/IP/RSSI/traffic/uptime, wake / permit / deny, `linked_object` + `sync_live`
- **VPN**: client tunnels and built-in servers (PPTP/SSTP/…); enable/disable server; connect/disconnect client; kick server session; session list with traffic
- **Journal** (optional per router): poll `show log`, Log tab, **log rules** (regexp → file and/or method), legacy router `linked_method` when no rules
- **Live UI**: WebSocket updates for devices / VPN / routers / log append
- **MCP**: collections `routers`, `devices`, `vpn`, `log_rules` + control operations

## Admin UI

- Main list: routers, settings (poll interval, firmware check interval)
- Router page tabs: **Devices**, **VPN**, **Log** (rules table + journal buffer)
- Edit router / device / VPN: icon, linked object, sync_live; VPN servers also `linked_method`; routers also journal options
- Log rules: Add/Edit on the Log tab (`pattern`, `write_to_file`, `linked_object`/`linked_method`, `active`)

## Configuration

### Plugin settings

| Key | Default | Description |
|-----|---------|-------------|
| `interval` | `5` | Router poll interval (seconds) |
| `firmware_check_interval` | `3600` | Firmware check interval (seconds) |

### Per router

| Field | Description |
|-------|-------------|
| host / port / login / password | RCI access |
| `linked_object` | Object for live metrics (`sync_live`) and firmware method |
| `linked_method` | `EVENT=firmware_update`; for journal — only if **no** active log rules (legacy: every new line) |
| `poll_log` | Poll system journal each cycle |
| `log_to_file` | Master switch for `logs/KeeneticJournal_<id>.log` |
| `sync_live` | Comma list / checkboxes: `online`, `cpu`, `ram`, `uptime`, `firmware_version`, … |

### Log rules (`keenetic_log_rules`)

Used when `poll_log` is on. Each **new** journal line (after the first baseline) is matched against active rules.

| Field | Description |
|-------|-------------|
| `pattern` | regexp against `level facility message` (inline flags OK, e.g. `(?i)`) |
| `write_to_file` | 1 = write match to file (if router `log_to_file` is on) |
| `linked_object` | Object for method call (required together with `linked_method`) |
| `linked_method` | method called on match |
| `active` | 0/1 |

**File**

- `log_to_file` off → nothing written;
- active rules exist → only matches where the rule has `write_to_file`;
- no active rules → legacy: all new lines (if `log_to_file` on).

**Methods**

- at least one active rule → calls **only** from matching rules with `linked_method` (router method not used for `EVENT=log`); same `linked_object.linked_method` is called **once** per line even if multiple rules match (first match wins);
- no active rules → legacy: every line → `router.linked_object.linked_method`;
- firmware (`EVENT=firmware_update`) always uses the router method.

Rule call params: `EVENT=log`, `MESSAGE`, `LEVEL`, `TIME`, `FACILITY`, `LABEL`, `VALUE`, `REPEATED`, `ROUTER_ID`, `ROUTER_TITLE`, `RULE_ID`, `RULE_TITLE`, `PATTERN`, `SOURCE=Keenetic`.

### Firmware update method params

On periodic/manual firmware check, when a **new** `update_version` appears (once per version):

`EVENT=firmware_update`, `FIRMWARE_VERSION`, `UPDATE_VERSION`, `UPDATE_CHANNEL`, `UPDATE_AVAILABLE=1`, `VALUE`, `ROUTER_ID`, `ROUTER_TITLE`, `MODEL`, `SOURCE=Keenetic`

Also creates a system notification via `addNotify` (Warning). Keep `update_available` / `update_version` in `sync_live` for property-based automations.

### VPN server method params

On client connect/disconnect: `EVENT`, `USER`, `IP`, `REMOTE`, `VALUE`; on disconnect also `RXBYTES` / `TXBYTES` / `UPTIME`.

## MCP notes

- Password is write-only (never returned on list/get)
- **`devices` / `vpn`** — discovered on poll only; upsert with `entity_id` updates bindings; do not create
- **`log_rules`** — full CRUD; upsert with `entity_id` updates the row in place
- Operations include: `poll_now`, `reboot`, `wake`, `set_access`, `check_firmware`, `apply_update`, `vpn_connect` / `vpn_disconnect`, `vpn_enable` / `vpn_disable`, `vpn_kick`

## Technical

- Keenetic RCI with a persistent HTTP session (re-auth on 401/403 only)
- Optional VPN show endpoints that return “not found” are cached per session and not re-probed every poll (avoids syslog spam)
- Volatile metrics stay in memory; DB stores stable fields and bindings
- Parallel router processing in `cycle`

## Version

**0.6**

## Category

Devices

## Actions

- `cycle` — background polling
- `search` — search routers/devices/log rules
- `widget` — dashboard widget

## Requirements

Flask, SQLAlchemy, Requests, osysHome core

## Author / License

osysHome Team — see main project license
