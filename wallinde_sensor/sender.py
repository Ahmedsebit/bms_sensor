"""Send one explicitly synthetic message through the real local sensor service."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from wallinde_sensor.service import load_config


def main(*, default_config: Path | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=["normal", "smoke", "heartbeat", "fault", "offline"], default="normal"
    )
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument(
        "--config", type=Path, default=default_config, required=default_config is None
    )
    args = parser.parse_args()
    config = load_config(args.config)
    sender = next(
        entry
        for entry in config.sources
        if entry.school_code == "DEMO"
        and entry.gateway_id == "local-test-gateway"
        and entry.mode == "test"
    )
    measurements = {}
    if args.scenario in {"normal", "smoke"}:
        measurements = {
            "smokeAlarm": args.scenario == "smoke",
            "temperatureC": 24.2,
            "humidityPercent": 52.0,
            "batteryPercent": 86.0,
        }
    body = {
        "schemaVersion": 1,
        "eventId": str(uuid4()),
        "deviceId": "local-smoke-01",
        "observedAt": datetime.now(UTC).isoformat(),
        "health": args.scenario if args.scenario in {"fault", "offline"} else "healthy",
        "measurements": measurements,
    }
    request = Request(
        f"http://127.0.0.1:{args.port}/api/v1/readings",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + sender.token.get_secret_value(),
        },
    )
    with urlopen(request, timeout=5) as response:
        receipt = json.load(response)
    print(
        f"TEST reading accepted: scenario={args.scenario}, sequence={receipt['sequence']}. "
        "This is service receipt, not an alarm or emergency dispatch."
    )


if __name__ == "__main__":
    main()
