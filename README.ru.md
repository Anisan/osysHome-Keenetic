# Keenetic — интеграция с роутерами Keenetic

![Keenetic Icon](static/Keenetic.png)

Плагин osysHome для роутеров Keenetic: устройства LAN, VPN, прошивка, опциональный системный журнал, live-админка и MCP.

## Возможности

- **Роутеры**: несколько устройств, учётные данные, CPU/RAM/uptime, проверка/установка прошивки, reboot
- **Устройства**: discovery, online/IP/RSSI/трафик/uptime, wake / permit / deny, `linked_object` + `sync_live`
- **VPN**: клиентские туннели и встроенные серверы (PPTP/SSTP/…); вкл/выкл сервера; connect/disconnect клиента; kick сессии; список клиентов с трафиком
- **Журнал** (опционально на роутере): опрос `show log`, вкладка Log, **правила журнала** (regexp → файл и/или метод), legacy-вызов `linked_method` роутера без правил
- **Live UI**: обновления по WebSocket (устройства / VPN / роутеры / дописывание лога)
- **MCP**: коллекции `routers`, `devices`, `vpn`, `log_rules` + операции управления

## Админка

- Список роутеров, Settings (интервал опроса, интервал проверки прошивки)
- Страница роутера: вкладки **Devices**, **VPN**, **Log** (таблица правил + буфер журнала)
- Edit роутера / устройства / VPN: иконка, linked object, sync_live; у VPN-серверов ещё `linked_method`; у роутера — опции журнала
- Правила журнала: Add/Edit на вкладке Log (`pattern`, `write_to_file`, `linked_object`/`linked_method`, `active`)

## Конфигурация

### Настройки плагина

| Ключ | По умолчанию | Описание |
|------|--------------|----------|
| `interval` | `5` | Интервал опроса роутеров (сек) |
| `firmware_check_interval` | `3600` | Интервал проверки прошивки (сек) |

### На роутере

| Поле | Описание |
|------|----------|
| host / port / login / password | доступ к RCI |
| `linked_object` | объект для live-метрик (`sync_live`) и метода прошивки |
| `linked_method` | `EVENT=firmware_update`; для журнала — только если **нет** активных правил (legacy: каждая новая строка) |
| `poll_log` | опрашивать системный журнал каждый цикл |
| `log_to_file` | мастер-флаг записи в `logs/KeeneticJournal_<id>.log` |
| `sync_live` | список/чекбоксы: `online`, `cpu`, `ram`, `uptime`, `firmware_version`, … |

### Правила журнала (`keenetic_log_rules`)

Включаются при `poll_log`. Каждая **новая** строка журнала (после первого baseline) проходит через активные правила.

| Поле | Описание |
|------|----------|
| `pattern` | regexp; матч по строке `level facility message` (флаги в pattern, напр. `(?i)`) |
| `write_to_file` | 1 = писать совпадение в файл (если у роутера `log_to_file`) |
| `linked_object` | объект для вызова метода (обязателен вместе с `linked_method`) |
| `linked_method` | метод при совпадении |
| `active` | 0/1 |

**Файл**

- `log_to_file` выкл → ничего не пишется;
- есть активные правила → в файл только совпадения с `write_to_file`;
- активных правил нет → legacy: все новые строки (если `log_to_file` вкл).

**Методы**

- есть хотя бы одно активное правило → вызовы **только** из совпавших правил с `linked_method` (роутерный метод для `EVENT=log` не зовётся); один и тот же `linked_object.linked_method` на строку — **один раз**, даже при нескольких совпавших правилах (первое по порядку);
- активных правил нет → legacy: каждая строка → `router.linked_object.linked_method`;
- прошивка (`EVENT=firmware_update`) всегда через метод роутера.

Параметры вызова правила: `EVENT=log`, `MESSAGE`, `LEVEL`, `TIME`, `FACILITY`, `LABEL`, `VALUE`, `REPEATED`, `ROUTER_ID`, `ROUTER_TITLE`, `RULE_ID`, `RULE_TITLE`, `PATTERN`, `SOURCE=Keenetic`.

### Параметры метода обновления прошивки

При периодической/ручной проверке прошивки, когда появляется **новая** `update_version` (один раз на версию):

`EVENT=firmware_update`, `FIRMWARE_VERSION`, `UPDATE_VERSION`, `UPDATE_CHANNEL`, `UPDATE_AVAILABLE=1`, `VALUE`, `ROUTER_ID`, `ROUTER_TITLE`, `MODEL`, `SOURCE=Keenetic`

Также создаётся системное уведомление через `addNotify` (Warning). Для автоматизаций по свойствам можно включить `update_available` / `update_version` в `sync_live`.

### Параметры метода VPN-сервера

При connect/disconnect клиента: `EVENT`, `USER`, `IP`, `REMOTE`, `VALUE`; при disconnect также `RXBYTES` / `TXBYTES` / `UPTIME`.

## MCP

- Пароль write-only (не возвращается в list/get)
- **`devices` / `vpn`** — только из опроса; upsert с `entity_id` обновляет привязки, создавать нельзя
- **`log_rules`** — полный CRUD; upsert с `entity_id` обновляет строку на месте
- Операции: `poll_now`, `reboot`, `wake`, `set_access`, `check_firmware`, `apply_update`, `vpn_connect` / `vpn_disconnect`, `vpn_enable` / `vpn_disable`, `vpn_kick`

## Технические детали

- Keenetic RCI с постоянной HTTP-сессией (повторный login только на 401/403)
- Отсутствующие VPN show-эндпоинты (`not found`) кэшируются на сессию и не опрашиваются каждый цикл (без спама в syslog роутера)
- Волатильные метрики в памяти; в БД — стабильные поля и привязки
- Параллельный опрос роутеров в `cycle`

## Версия

**0.6**

## Категория

Devices

## Действия

- `cycle` — фоновый опрос
- `search` — поиск роутеров/устройств/правил журнала
- `widget` — виджет дашборда

## Требования

Flask, SQLAlchemy, Requests, ядро osysHome

## Автор / лицензия

Команда osysHome — см. лицензию основного проекта
