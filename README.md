# Wallinde sensor app

Standalone Python 3.13+ service and explicitly TEST-only sender. No PostgreSQL,
backend package, Android SDK or frontend is required. This is HTTP sensor ingress,
not a hardware/vendor adapter or a mobile sensor app.

## School deployment architecture

See [Dormitory sensor architecture](docs/school-sensor-architecture.md) for the
proposed physical layout, school network, BMS integration, commissioning checks
and responsibilities. It separates independent fire protection from supplementary
BMS monitoring and identifies the hardware integration still to be built.

## Install and run

Run from this folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -c "from pathlib import Path; from wallinde_sensor.service import local_config; local_config(Path('.local') / 'settings.json')"
.\.venv\Scripts\wallinde-sensors.exe --config .local\settings.json --database .local\readings.db --port 8010
```

Provisioning creates private random source/reader credentials only when the file
does not exist. It does not print them. The generated source is DEMO /
local-test-gateway / local-smoke-01, mode `test`. Keep configuration files private.
Configure the backend's worker separately with this school's **reader** token.

In another terminal:

```powershell
.\.venv\Scripts\wallinde-sensor-send.exe --config .local\settings.json --scenario normal
.\.venv\Scripts\wallinde-sensor-send.exe --config .local\settings.json --scenario smoke
```

Other scenarios: `heartbeat`, `fault`, `offline`. `normal` explicitly clears the
test smoke alarm; a heartbeat does not. This sender only selects the local DEMO
test gateway, never a hardware credential.

## HTTP boundary

| Endpoint | Authentication | Purpose |
|---|---|---|
| `GET /health` | None | Journal availability, not physical safety |
| `POST /api/v1/readings` | Gateway source bearer token | Device-scoped v1 observations |
| `GET /api/v1/events` | School reader bearer token | Ordered school-scoped journal |

`/docs` describes the wire schema. The service owns `wallinde_sensor.contract`;
the backend maintains its own consumer DTOs. Neither project imports the other.
Workspace contract and real HTTP E2E checks guard their compatibility.

Exact event replays preserve receipts; changed payloads with the same event ID
conflict. Stream IDs let the backend detect journal replacement. Keep the journal
and its WAL files intact; do not delete them to reset a running deployment.
The existing combined demo still uses the workspace root's original
`.local\sensor-service` configuration and journal, not this folder's `.local`.

## Checks

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use temporary SQLite journals, no backend or database server.
The service binds to loopback. Remote use requires an approved TLS gateway,
device provisioning, monitoring and field/fault testing. No physical alarms,
emergency calls, SMS or push notifications are dispatched.
