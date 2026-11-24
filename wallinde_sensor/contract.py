"""Service-owned v1 wire models, independent of the backend's consumer models."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from pydantic.alias_generators import to_camel


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def trim_fields(cls, value):
        return value.strip() if isinstance(value, str) else value


Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]


class SensorMeasurements(Model):
    smoke_alarm: StrictBool | None = None
    co_alarm: StrictBool | None = None
    water_leak: StrictBool | None = None
    tamper: StrictBool | None = None
    door_open: StrictBool | None = None
    temperature_c: float | None = Field(
        default=None, ge=-100, le=250, allow_inf_nan=False, strict=True
    )
    humidity_percent: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False, strict=True
    )
    co_ppm: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False, strict=True)
    battery_percent: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False, strict=True
    )


class SensorReading(Model):
    schema_version: Literal[1] = 1
    event_id: UUID
    device_id: Identifier
    observed_at: datetime
    health: Literal["healthy", "fault", "offline", "unknown"]
    measurements: SensorMeasurements = Field(default_factory=SensorMeasurements)

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value.astimezone(UTC)


class SensorEvent(Model):
    sequence: int = Field(ge=1)
    gateway_id: Identifier
    mode: Literal["test", "hardware"]
    received_at: datetime
    reading: SensorReading

    @field_validator("received_at")
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value.astimezone(UTC)


class SensorFeed(Model):
    stream_id: UUID
    school_code: str
    cursor: int = Field(ge=0)
    events: list[SensorEvent] = Field(max_length=100)
