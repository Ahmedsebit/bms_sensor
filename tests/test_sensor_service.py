import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from wallinde_sensor import sender
from wallinde_sensor.contract import SensorReading
from wallinde_sensor.service import ServiceConfig, create_app, local_config

SOURCE_A = "synthetic-source-alpha-not-a-deployment-secret"
SOURCE_B = "synthetic-source-beta-not-a-deployment-secret"
READER_A = "synthetic-reader-alpha-not-a-deployment-secret"
READER_B = "synthetic-reader-beta-not-a-deployment-secret"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def service_config(device="smoke-01"):
    return ServiceConfig.model_validate(
        {
            "sources": [
                {
                    "schoolCode": code,
                    "gatewayId": "gateway-01",
                    "token": token,
                    "deviceIds": [device],
                    "mode": mode,
                }
                for code, token, mode in [
                    ("ALPHA", SOURCE_A, "test"),
                    ("BETA", SOURCE_B, "hardware"),
                ]
            ],
            "readers": [
                {"schoolCode": code, "token": token}
                for code, token in [("ALPHA", READER_A), ("BETA", READER_B)]
            ],
        }
    )


def reading(device="smoke-01", **changes):
    return {
        "eventId": str(uuid4()),
        "deviceId": device,
        "observedAt": datetime.now(UTC).isoformat(),
        "health": "healthy",
        "measurements": {"smokeAlarm": False, "temperatureC": 24.2},
        **changes,
    }


@pytest.fixture
def ingress(tmp_path):
    config = service_config()
    database = tmp_path / "readings.db"
    with TestClient(create_app(database, config)) as client:
        yield client, database, config


def test_authenticated_device_scoped_ingress_and_separate_reader(ingress):
    client, _, _ = ingress
    body = reading()
    assert client.post("/api/v1/readings", json=body).status_code == 401
    assert client.post("/api/v1/readings", headers=auth(READER_A), json=body).status_code == 401
    assert client.get("/api/v1/events", headers=auth(SOURCE_A)).status_code == 401
    assert (
        client.post(
            "/api/v1/readings", headers=auth(SOURCE_A), json=reading(device="unregistered")
        ).status_code
        == 403
    )
    assert client.post("/api/v1/readings", headers=auth(SOURCE_A), json=body).status_code == 200
    body_b = reading(measurements={"smokeAlarm": True})
    assert client.post("/api/v1/readings", headers=auth(SOURCE_B), json=body_b).status_code == 200
    alpha = client.get("/api/v1/events", headers=auth(READER_A)).json()
    beta = client.get("/api/v1/events", headers=auth(READER_B)).json()
    assert len(alpha["events"]) == len(beta["events"]) == 1
    assert alpha["events"][0]["reading"]["eventId"] == body["eventId"]
    assert alpha["events"][0]["mode"] == "test"
    assert beta["events"][0]["mode"] == "hardware"
    assert (
        client.post(
            "/api/v1/readings", headers=auth(SOURCE_A), json={**reading(), "mode": "hardware"}
        ).status_code
        == 422
    )


def test_exact_replay_persists_original_receipt_across_restart_and_conflicts(ingress):
    client, database, config = ingress
    body = reading()
    accepted = client.post("/api/v1/readings", headers=auth(SOURCE_A), json=body).json()
    stream = client.get("/api/v1/events", headers=auth(READER_A)).json()["streamId"]
    with TestClient(create_app(database, config)) as restarted:
        replayed = restarted.post("/api/v1/readings", headers=auth(SOURCE_A), json=body).json()
        assert replayed == {**accepted, "duplicate": True}
        assert (
            restarted.post(
                "/api/v1/readings",
                headers=auth(SOURCE_A),
                json={**body, "measurements": {"smokeAlarm": True}},
            ).status_code
            == 409
        )
        feed = restarted.get("/api/v1/events", headers=auth(READER_A)).json()
        assert feed["streamId"] == stream
        assert len(feed["events"]) == 1


def test_bounded_cursor_and_stream_replacement(ingress):
    client, _, _ = ingress
    for _ in range(3):
        assert (
            client.post("/api/v1/readings", headers=auth(SOURCE_A), json=reading()).status_code
            == 200
        )
    first = client.get("/api/v1/events?limit=2", headers=auth(READER_A)).json()
    second = client.get(f"/api/v1/events?after={first['cursor']}", headers=auth(READER_A)).json()
    assert len(first["events"]) == 2 and len(second["events"]) == 1
    restarted = client.get(
        f"/api/v1/events?after=1000&streamId={uuid4()}", headers=auth(READER_A)
    ).json()
    assert len(restarted["events"]) == 3
    assert client.get("/api/v1/events?limit=101", headers=auth(READER_A)).status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"observedAt": "2026-11-21T10:00:00"},
        {"observedAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        {"health": "safe"},
        {"measurements": {"smokeAlarm": "false"}},
        {"measurements": {"humidityPercent": 101}},
        {"measurements": {"temperatureC": "NaN"}},
        {"measurements": {"temperatureC": True}},
        {"measurements": {"unknownVendorField": "undecoded"}},
    ],
)
def test_malformed_readings_never_enter_journal(ingress, changes):
    client, _, _ = ingress
    assert (
        client.post("/api/v1/readings", headers=auth(SOURCE_A), json=reading(**changes)).status_code
        == 422
    )
    assert client.get("/api/v1/events", headers=auth(READER_A)).json()["events"] == []


def test_body_limit_applies_to_actual_body_not_only_headers(ingress):
    client, _, _ = ingress
    response = client.post(
        "/api/v1/readings",
        headers={**auth(SOURCE_A), "Content-Type": "application/json"},
        content=b" " * 33_000,
    )
    assert response.status_code == 413


def test_standalone_sender_uses_explicit_private_config_and_test_source(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "private-config.json"
    config = local_config(config_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "wallinde-sensor-send",
            "--config",
            str(config_path),
            "--port",
            "18010",
            "--scenario",
            "smoke",
        ],
    )
    sent = []

    def accept(request, timeout):
        assert request.full_url == "http://127.0.0.1:18010/api/v1/readings"
        assert request.get_header("Authorization") == (
            "Bearer " + config.sources[0].token.get_secret_value()
        )
        sent.append(SensorReading.model_validate_json(request.data))
        return io.BytesIO(json.dumps({"sequence": 1}).encode())

    monkeypatch.setattr(sender, "urlopen", accept)
    sender.main()
    assert len(sent) == 1 and sent[0].measurements.smoke_alarm is True
    assert sent[0].device_id == "local-smoke-01"
    assert "not an alarm or emergency dispatch" in capsys.readouterr().out
