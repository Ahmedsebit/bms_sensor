"""Separate authenticated HTTP ingress and durable, school-scoped event journal."""

import argparse
import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, SecretStr, ValidationError, model_validator

from wallinde_sensor.contract import Identifier, Model, SensorEvent, SensorFeed, SensorReading


class ReadingBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > 32_768:
                raise HTTPException(413, "Reading exceeds 32 KiB")
            return message

        await self.app(scope, limited_receive if scope["type"] == "http" else receive, send)


class SourceCredential(Model):
    school_code: str = Field(pattern=r"^[A-Z0-9-]{1,64}$")
    gateway_id: Identifier
    token: SecretStr = Field(min_length=32)
    device_ids: list[Identifier] = Field(min_length=1, max_length=2000)
    mode: Literal["test", "hardware"] = "test"


class ReaderCredential(Model):
    school_code: str = Field(pattern=r"^[A-Z0-9-]{1,64}$")
    token: SecretStr = Field(min_length=32)


class ServiceConfig(Model):
    sources: list[SourceCredential] = Field(min_length=1, max_length=1000)
    readers: list[ReaderCredential] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_credentials(self):
        tokens = [entry.token.get_secret_value() for entry in self.sources + self.readers]
        if len(set(tokens)) != len(tokens):
            raise ValueError("credentials_must_be_distinct")
        identities = [(entry.school_code, entry.gateway_id) for entry in self.sources]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate_gateway")
        for entry in self.sources:
            if len(set(entry.device_ids)) != len(entry.device_ids):
                raise ValueError("duplicate_device")
        return self


def create_app(database: Path, config: ServiceConfig, *, clock=lambda: datetime.now(UTC)):
    database.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def journal():
        conn = sqlite3.connect(database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with journal() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO metadata VALUES ('stream_id', ?)", (str(uuid4()),))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS readings ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, school_code TEXT NOT NULL,"
            "gateway_id TEXT NOT NULL, device_id TEXT NOT NULL, event_id TEXT NOT NULL,"
            "mode TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL,"
            "payload_hash TEXT NOT NULL, UNIQUE(school_code,gateway_id,event_id))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS school_events ON readings(school_code,sequence)")

    app = FastAPI(title="Wallinde sensor ingestion service", version="1.0")
    app.add_middleware(ReadingBodyLimit)
    bearer = HTTPBearer(auto_error=False)
    bearer_dependency = Depends(bearer)

    def principal(credentials, entries):
        if credentials is None:
            raise HTTPException(
                401, "Sensor credential required", headers={"WWW-Authenticate": "Bearer"}
            )
        supplied = hashlib.sha256(credentials.credentials.encode()).digest()
        for entry in entries:
            expected = hashlib.sha256(entry.token.get_secret_value().encode()).digest()
            if secrets.compare_digest(supplied, expected):
                return entry
        raise HTTPException(
            401, "Invalid sensor credential", headers={"WWW-Authenticate": "Bearer"}
        )

    def source(credentials: HTTPAuthorizationCredentials | None = bearer_dependency):
        return principal(credentials, config.sources)

    def reader(credentials: HTTPAuthorizationCredentials | None = bearer_dependency):
        return principal(credentials, config.readers)

    source_dependency = Depends(source)
    reader_dependency = Depends(reader)

    @app.get("/health")
    def health():
        with journal() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "physicalAlarmIntegration": False, "delivery": "http_journal_only"}

    @app.post("/api/v1/readings")
    def ingest(body: SensorReading, sender: SourceCredential = source_dependency):
        if body.device_id not in sender.device_ids:
            raise HTTPException(403, "Device is not registered to this gateway")
        now = clock()
        if body.observed_at > now + timedelta(seconds=120):
            raise HTTPException(422, "Device clock is more than 120 seconds ahead")
        payload = body.model_dump_json(by_alias=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with journal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT sequence,payload_hash,received_at FROM readings "
                "WHERE school_code=? AND gateway_id=? AND event_id=?",
                (sender.school_code, sender.gateway_id, str(body.event_id)),
            ).fetchone()
            if previous:
                if previous["payload_hash"] != digest:
                    raise HTTPException(409, "Event ID reused with different data")
                return {
                    "sequence": previous["sequence"],
                    "receivedAt": previous["received_at"],
                    "duplicate": True,
                }
            sequence = conn.execute(
                "INSERT INTO readings(school_code,gateway_id,device_id,event_id,mode,"
                "received_at,payload,payload_hash) VALUES (?,?,?,?,?,?,?,?)",
                (
                    sender.school_code,
                    sender.gateway_id,
                    body.device_id,
                    str(body.event_id),
                    sender.mode,
                    now.isoformat(),
                    payload,
                    digest,
                ),
            ).lastrowid
        return {"sequence": sequence, "receivedAt": now.isoformat(), "duplicate": False}

    @app.get("/api/v1/events", response_model=SensorFeed)
    def events(
        after: int = Query(default=0, ge=0, le=9_223_372_036_854_775_807),
        limit: int = Query(default=100, ge=1, le=100),
        stream_id: str | None = Query(default=None, alias="streamId", max_length=36),
        receiver: ReaderCredential = reader_dependency,
    ):
        with journal() as conn:
            current_stream = conn.execute(
                "SELECT value FROM metadata WHERE key='stream_id'"
            ).fetchone()["value"]
            if stream_id is not None and stream_id != current_stream:
                after = 0
            rows = conn.execute(
                "SELECT * FROM readings WHERE school_code=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (receiver.school_code, after, limit),
            ).fetchall()
        return SensorFeed(
            stream_id=current_stream,
            school_code=receiver.school_code,
            cursor=rows[-1]["sequence"] if rows else after,
            events=[
                SensorEvent(
                    sequence=row["sequence"],
                    gateway_id=row["gateway_id"],
                    mode=row["mode"],
                    received_at=row["received_at"],
                    reading=SensorReading.model_validate_json(row["payload"]),
                )
                for row in rows
            ],
        )

    return app


def local_config(path: Path):
    """Provision a deliberately test-only local sender without printing credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        config = {
            "sources": [
                {
                    "schoolCode": "DEMO",
                    "gatewayId": "local-test-gateway",
                    "token": secrets.token_urlsafe(48),
                    "deviceIds": ["local-smoke-01"],
                    "mode": "test",
                }
            ],
            "readers": [{"schoolCode": "DEMO", "token": secrets.token_urlsafe(48)}],
        }
        with path.open("x", encoding="utf-8") as file:
            json.dump(config, file, indent=2)
    return load_config(path)


def load_config(path: Path):
    try:
        return ServiceConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        raise SystemExit(
            "Invalid sensor service configuration (credential values hidden)."
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    config = load_config(args.config)
    uvicorn.run(create_app(args.database, config), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
