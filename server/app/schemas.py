from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class LiveRoomJoin(BaseModel):
    room_name: str = Field(min_length=1, max_length=120)
    volume_signature: str = Field(min_length=16, max_length=160)

    @field_validator("room_name", "volume_signature")
    @classmethod
    def strip_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class LivePreflightRequest(BaseModel):
    room_name: str = Field(min_length=1, max_length=120)
    volume_signature: str = Field(min_length=16, max_length=160)
    plugin_version: str = Field(min_length=1, max_length=40)
    protocol_version: int = Field(ge=1, le=1000)

    @field_validator("room_name", "volume_signature", "plugin_version")
    @classmethod
    def strip_preflight_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class LiveOperationCreate(BaseModel):
    client_operation_id: str = Field(min_length=8, max_length=120)
    segment_id: str = Field(min_length=1, max_length=200)
    segment_name: str = Field(min_length=1, max_length=200)
    color_hex: str = "#4A90E2"
    operation_kind: Literal["patch", "snapshot"] = "patch"
    volume_shape: list[int] = Field(min_length=3, max_length=3)
    voxel_bbox: list[int] = Field(min_length=6, max_length=6)
    encoding: Literal["zlib-packbits-v1"] = "zlib-packbits-v1"
    payload: str = Field(min_length=1)
    base_sequence: int = Field(default=0, ge=0)
    snapshot_group_id: str | None = Field(default=None, max_length=120)
    snapshot_group_index: int | None = Field(default=None, ge=0)
    snapshot_group_count: int | None = Field(default=None, ge=1)
    system_snapshot: bool = False
    snapshot_label: str | None = Field(default=None, max_length=200)
    segment_deleted: bool = False
    undo_of_sequence: int | None = Field(default=None, ge=1)

    @field_validator("color_hex")
    @classmethod
    def validate_color(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 7 or not value.startswith("#"):
            raise ValueError("color_hex must use #RRGGBB format")
        try:
            int(value[1:], 16)
        except ValueError as exc:
            raise ValueError("color_hex must use #RRGGBB format") from exc
        return value

    @field_validator("volume_shape")
    @classmethod
    def validate_volume_shape(cls, value: list[int]) -> list[int]:
        if any(int(item) <= 0 for item in value):
            raise ValueError("volume_shape values must be positive")
        return [int(item) for item in value]

    @field_validator("voxel_bbox")
    @classmethod
    def validate_voxel_bbox(cls, value: list[int]) -> list[int]:
        value = [int(item) for item in value]
        if any(item < 0 for item in value):
            raise ValueError("voxel_bbox values must be non-negative")
        if not (value[0] < value[1] and value[2] < value[3] and value[4] < value[5]):
            raise ValueError("voxel_bbox must contain non-empty z, y, and x ranges")
        return value


class LivePresenceUpdate(BaseModel):
    presence_session_id: str | None = Field(default=None, max_length=64)
    active_segment_id: str | None = Field(default=None, max_length=200)
    active_segment_name: str | None = Field(default=None, max_length=200)
    crosshair_ras: list[float] | None = Field(default=None, min_length=3, max_length=3)
    slice_offsets: dict[str, float] | None = None
    active_effect: str | None = Field(default=None, max_length=200)
    layout: int | None = None
    color: list[float] | None = Field(default=None, min_length=3, max_length=3)
    role: str | None = Field(default=None, max_length=40)
    plugin_version: str | None = Field(default=None, max_length=40)
    protocol_version: int | None = Field(default=None, ge=1, le=1000)
    volume_signature_hash: str | None = Field(default=None, max_length=64)
    transport: str | None = Field(default=None, max_length=80)


class LiveChatMessageCreate(BaseModel):
    client_message_id: str = Field(min_length=8, max_length=120)
    text: str = Field(min_length=1, max_length=4000)
    anchor: dict[str, Any] | None = None

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class LiveSegmentLockUpdate(BaseModel):
    locked: bool
    expires_minutes: int = Field(default=0, ge=0, le=1440)


class LiveRoleUpdate(BaseModel):
    role: Literal["viewer", "editor", "reviewer", "admin"]


class LiveReviewUpdate(BaseModel):
    state: Literal[
        "draft",
        "in_progress",
        "ready_for_review",
        "changes_requested",
        "approved",
    ]
    note: str = Field(default="", max_length=2000)


class LiveAccessRequestCreate(BaseModel):
    segment_id: str = Field(min_length=1, max_length=200)
    message: str = Field(default="", max_length=1000)


class LiveOwnerTransfer(BaseModel):
    target_user: str = Field(min_length=1, max_length=120)


class LiveConflictResolution(BaseModel):
    resolution: Literal["latest", "mine", "other", "union"]


class LiveMaterialTemplate(BaseModel):
    format: str = "live-segmentation-material-template-v1"
    name: str = Field(default="Room material template", max_length=200)
    segments: list[dict[str, Any]]
