# MCP — Keenetic

Плагин опрашивает роутеры Keenetic, хранит клиентов LAN и обновляет свойства привязанных объектов osysHome. Управление клиентами через Keenetic API не выполняется (только мониторинг).

## Plugin notes

- Роутеры опрашиваются по `config.interval` (секунды). Принудительный опрос — `poll_now`.
- Привязка роутера — `linked_object`. Обновляются: `online`, `cpu`, `ram`, `uptime`.
- Привязка устройства — `linked_object`. Обновляются: `ip`, `online`, `signal_strength`, `rxbytes`, `txbytes`, `uptime`.
- Для каждого роутера создаётся псевдо-устройство `Internet` (`mac=0.0.0.0.0.0`) для статуса WAN.
- У `Internet` при `linked_object` обновляются: `ip`, `online`, `rxbytes`, `txbytes`.
- Устройства обычно появляются при опросе; ручное создание требует `router_id`.
- `password` — write-only: передаётся в `upsert`, не возвращается в `list`/`get`.
- Удаление роутера удаляет и его устройства.

## Collections

| ID | binding_mode | writable | writable_fields | list_filters |
|----|--------------|----------|-----------------|--------------|
| `routers` | `object` | yes | `title`, `ip`, `port`, `login`, `password`, `linked_object` | `query`, `linked_object`, `has_linked_object` |
| `devices` | `object` | yes | `router_id`, `title`, `ip`, `mac`, `linked_object` | `query`, `router_id`, `linked_object`, `has_linked_object` |

## Операции (invoke)

| operation | Описание |
|-----------|----------|
| `poll_now` | Немедленный опрос всех настроенных роутеров |

## Примеры

### Создать роутер с привязкой

```json
{
  "plugin": "Keenetic",
  "action": "upsert_entity",
  "args": {
    "collection": "routers",
    "payload": {
      "title": "Home Keenetic",
      "ip": "192.168.1.1",
      "port": 80,
      "login": "admin",
      "password": "secret",
      "linked_object": "Router.Home"
    }
  }
}
```

### Список устройств роутера

```json
{
  "plugin": "Keenetic",
  "action": "list_entities",
  "args": {
    "collection": "devices",
    "router_id": 1,
    "limit": 100
  }
}
```

### Привязать найденное устройство к объекту

```json
{
  "plugin": "Keenetic",
  "action": "upsert_entity",
  "args": {
    "collection": "devices",
    "entity_id": 12,
    "payload": {
      "linked_object": "Phone.Alice"
    }
  }
}
```

### Принудительный опрос

```json
{
  "plugin": "Keenetic",
  "action": "invoke",
  "args": {
    "operation": "poll_now",
    "params": {}
  }
}
```

### Только устройства с привязкой

```json
{
  "plugin": "Keenetic",
  "action": "list_entities",
  "args": {
    "collection": "devices",
    "has_linked_object": true
  }
}
```
