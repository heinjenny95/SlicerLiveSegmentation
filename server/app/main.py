from __future__ import annotations

import base64
import binascii
import json
import secrets
import sqlite3
import uuid
import zlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from .config import Settings
from .db import Database
from .events import LivePresenceRegistry
from .schemas import (
    LiveAccessRequestCreate,
    LiveChatMessageCreate,
    LiveConflictResolution,
    LiveMaterialTemplate,
    LiveOperationCreate,
    LiveOwnerTransfer,
    LivePresenceUpdate,
    LiveReviewUpdate,
    LiveRoleUpdate,
    LiveRoomJoin,
    LiveSegmentLockUpdate,
)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_live_operation(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["volume_shape"] = json.loads(data["volume_shape"])
    data["voxel_bbox"] = json.loads(data["voxel_bbox"])
    data["system_snapshot"] = bool(data.get("system_snapshot", False))
    return data


def public_chat_message(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["anchor"] = json.loads(data["anchor"]) if data.get("anchor") else None
    return data


def changed_voxel_coordinates(operation: dict[str, Any]) -> set[tuple[int, int, int]]:
    bounds = [int(value) for value in operation["voxel_bbox"]]
    z0, z1, y0, y1, x0, x1 = bounds
    shape = (z1 - z0, y1 - y0, x1 - x0)
    count = shape[0] * shape[1] * shape[2]
    packed_bytes = (count + 7) // 8
    raw = zlib.decompress(base64.b64decode(operation["payload"], validate=True))
    changed = raw[:packed_bytes]
    result = set()
    plane = shape[1] * shape[2]
    for index in range(count):
        if changed[index // 8] & (1 << (index % 8)):
            local_z, remainder = divmod(index, plane)
            local_y, local_x = divmod(remainder, shape[2])
            result.add((z0 + local_z, y0 + local_y, x0 + local_x))
    return result


def changed_voxel_count(operation: dict[str, Any]) -> int:
    bounds = [int(value) for value in operation["voxel_bbox"]]
    z0, z1, y0, y1, x0, x1 = bounds
    voxel_count = (z1 - z0) * (y1 - y0) * (x1 - x0)
    packed_bytes = (voxel_count + 7) // 8
    raw = zlib.decompress(base64.b64decode(operation["payload"], validate=True))
    return sum(byte.bit_count() for byte in raw[:packed_bytes])


def operation_overlap(first: dict[str, Any], second: dict[str, Any]) -> int:
    if str(first.get("segment_id")) != str(second.get("segment_id")):
        return 0
    return len(changed_voxel_coordinates(first) & changed_voxel_coordinates(second))


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    database = Database(resolved_settings.database_path)
    presence_registry = LivePresenceRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        app.state.settings = resolved_settings
        app.state.database = database
        app.state.presence_registry = presence_registry
        yield

    app = FastAPI(
        title="Live Segmentation Server",
        version="0.9.1",
        description="Optional relay for the Live Segmentation 3D Slicer extension.",
        lifespan=lifespan,
    )

    def require_user(
        request: Request,
        x_liveseg_user: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        if resolved_settings.require_https:
            forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
            if forwarded.lower() != "https":
                raise HTTPException(status_code=426, detail="HTTPS is required")
        if resolved_settings.user_tokens:
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            user = resolved_settings.user_tokens.get(token)
            if not user:
                raise HTTPException(status_code=401, detail="Invalid user token")
            if x_liveseg_user and x_liveseg_user.strip() != user:
                raise HTTPException(status_code=401, detail="User header does not match token")
            return user
        if resolved_settings.api_key:
            expected = f"Bearer {resolved_settings.api_key}"
            if not authorization or not secrets.compare_digest(authorization, expected):
                raise HTTPException(status_code=401, detail="Invalid API key")
        user = (x_liveseg_user or "").strip()
        if not user or len(user) > 80:
            raise HTTPException(status_code=401, detail="X-LiveSeg-User header is required")
        return user

    def role_for(connection: sqlite3.Connection, room_id: str, user: str) -> str:
        room = connection.execute(
            "SELECT created_by FROM live_rooms WHERE id = ?", (room_id,)
        ).fetchone()
        if room is None:
            raise HTTPException(status_code=404, detail="Live room not found")
        if room["created_by"] == user:
            return "admin"
        role = connection.execute(
            "SELECT role FROM live_room_roles WHERE room_id = ? AND user = ?",
            (room_id, user),
        ).fetchone()
        return str(role["role"]) if role is not None else "editor"

    def append_audit(
        connection: sqlite3.Connection,
        room_id: str,
        event: str,
        user: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO live_audit(id, room_id, event, user, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                room_id,
                event,
                user,
                json.dumps(details or {}, separators=(",", ":")),
                iso_now(),
            ),
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    @app.post("/api/live/rooms/join")
    async def join_live_room(
        payload: LiveRoomJoin, user: str = Depends(require_user)
    ) -> dict[str, Any]:
        created = False
        with database.transaction(immediate=True) as connection:
            room = connection.execute(
                "SELECT * FROM live_rooms WHERE name = ? COLLATE NOCASE",
                (payload.room_name,),
            ).fetchone()
            if room is None:
                created = True
                room_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO live_rooms(id, name, volume_signature, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (room_id, payload.room_name, payload.volume_signature, user, iso_now()),
                )
                room = connection.execute(
                    "SELECT * FROM live_rooms WHERE id = ?", (room_id,)
                ).fetchone()
            elif room["volume_signature"] != payload.volume_signature:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "The room is linked to a different source volume",
                        "room_name": room["name"],
                    },
                )
            latest_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM live_operations WHERE room_id = ?",
                (room["id"],),
            ).fetchone()[0]
            if created:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO live_room_roles(
                        room_id, user, role, updated_by, updated_at
                    ) VALUES (?, ?, 'admin', ?, ?)
                    """,
                    (room["id"], user, user, iso_now()),
                )
            append_audit(
                connection,
                room["id"],
                "room.join",
                user,
                {"created": created},
            )
        presence = await presence_registry.mark_presence(room["id"], user)
        return {
            **dict(room),
            "created": created,
            "latest_sequence": int(latest_sequence),
            "presence": presence,
        }

    @app.get("/api/live/rooms/{room_id}/operations")
    def list_live_operations(
        room_id: str,
        after: int = 0,
        limit: int = 200,
        user: str = Depends(require_user),
    ) -> list[dict[str, Any]]:
        del user
        if after < 0:
            raise HTTPException(status_code=400, detail="after must be non-negative")
        limit = max(1, min(int(limit), 500))
        with database.connect() as connection:
            room = connection.execute(
                "SELECT id FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            rows = connection.execute(
                """
                SELECT * FROM live_operations
                WHERE room_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (room_id, after, limit),
            ).fetchall()
        return [public_live_operation(row) for row in rows]

    @app.post("/api/live/rooms/{room_id}/operations", status_code=201)
    async def create_live_operation(
        room_id: str,
        payload: LiveOperationCreate,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        if len(payload.payload) > resolved_settings.max_upload_bytes * 2:
            raise HTTPException(status_code=413, detail="Live operation exceeds size limit")
        try:
            encoded_bytes = base64.b64decode(payload.payload.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="Invalid live operation payload") from exc
        if len(encoded_bytes) > resolved_settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Live operation exceeds size limit")

        range_ends = (payload.voxel_bbox[1], payload.voxel_bbox[3], payload.voxel_bbox[5])
        if any(end > size for end, size in zip(range_ends, payload.volume_shape, strict=True)):
            raise HTTPException(status_code=422, detail="voxel_bbox exceeds volume_shape")

        duplicate = False
        conflicts = []
        with database.transaction(immediate=True) as connection:
            room = connection.execute(
                "SELECT id FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            existing = connection.execute(
                """
                SELECT * FROM live_operations
                WHERE room_id = ? AND author = ? AND client_operation_id = ?
                """,
                (room_id, user, payload.client_operation_id),
            ).fetchone()
            if existing is not None:
                duplicate = True
                operation = existing
            else:
                if role_for(connection, room_id, user) == "viewer":
                    raise HTTPException(status_code=403, detail="Viewers cannot edit labels")
                lock_state = connection.execute(
                    """
                    SELECT owner, locked, expires_epoch FROM live_segment_locks
                    WHERE room_id = ? AND segment_id = ?
                    """,
                    (room_id, payload.segment_id),
                ).fetchone()
                active_lock = (
                    lock_state is not None
                    and bool(lock_state["locked"])
                    and not (
                        float(lock_state["expires_epoch"] or 0.0)
                        and datetime.now(timezone.utc).timestamp()
                        >= float(lock_state["expires_epoch"])
                    )
                )
                if active_lock and lock_state["owner"] != user:
                    raise HTTPException(
                        status_code=423,
                        detail=f"Label is locked by {lock_state['owner']}",
                    )
                incoming = payload.model_dump()
                # Count packed mask bits directly. A full-volume checkpoint must not
                # materialize millions of Python coordinate tuples just for metrics.
                changed_voxels = changed_voxel_count(incoming)
                if payload.base_sequence and not payload.system_snapshot:
                    concurrent = connection.execute(
                        """
                        SELECT * FROM live_operations
                        WHERE room_id = ? AND sequence > ? AND segment_id = ?
                          AND author != ? AND system_snapshot = 0
                        ORDER BY sequence ASC
                        """,
                        (room_id, payload.base_sequence, payload.segment_id, user),
                    ).fetchall()
                    for previous_row in concurrent:
                        previous = public_live_operation(previous_row)
                        overlap = operation_overlap(previous, incoming)
                        if overlap:
                            conflicts.append(
                                {
                                    "id": str(uuid.uuid4()),
                                    "segment_id": payload.segment_id,
                                    "other_author": previous["author"],
                                    "other_sequence": int(previous["sequence"]),
                                    "overlap_voxels": overlap,
                                }
                            )
                operation_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO live_operations(
                        id, room_id, client_operation_id, author, segment_id,
                        segment_name, color_hex, operation_kind, volume_shape,
                        voxel_bbox, encoding, payload, base_sequence,
                        snapshot_group_id, snapshot_group_index,
                        snapshot_group_count, system_snapshot, snapshot_label,
                        changed_voxels,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        room_id,
                        payload.client_operation_id,
                        user,
                        payload.segment_id,
                        payload.segment_name,
                        payload.color_hex,
                        payload.operation_kind,
                        json.dumps(payload.volume_shape, separators=(",", ":")),
                        json.dumps(payload.voxel_bbox, separators=(",", ":")),
                        payload.encoding,
                        payload.payload,
                        payload.base_sequence,
                        payload.snapshot_group_id,
                        payload.snapshot_group_index,
                        payload.snapshot_group_count,
                        int(payload.system_snapshot),
                        payload.snapshot_label,
                        changed_voxels,
                        iso_now(),
                    ),
                )
                operation = connection.execute(
                    "SELECT * FROM live_operations WHERE id = ?", (operation_id,)
                ).fetchone()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO live_segment_locks(
                        room_id, segment_id, owner, locked, updated_by, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    (room_id, payload.segment_id, user, user, iso_now()),
                )
                for conflict in conflicts:
                    connection.execute(
                        """
                        INSERT INTO live_conflicts(
                            id, room_id, sequence, author, segment_id,
                            other_author, other_sequence, overlap_voxels,
                            resolution, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unresolved', ?)
                        """,
                        (
                            conflict["id"],
                            room_id,
                            int(operation["sequence"]),
                            user,
                            payload.segment_id,
                            conflict["other_author"],
                            conflict["other_sequence"],
                            conflict["overlap_voxels"],
                            iso_now(),
                        ),
                    )
                    conflict.update(
                        {
                            "sequence": int(operation["sequence"]),
                            "author": user,
                            "resolution": "unresolved",
                            "created_at": iso_now(),
                        }
                    )
                append_audit(
                    connection,
                    room_id,
                    "segmentation.operation",
                    user,
                    {
                        "sequence": int(operation["sequence"]),
                        "segment_id": payload.segment_id,
                        "changed_voxels": changed_voxels,
                        "conflict_count": len(conflicts),
                    },
                )
        result = public_live_operation(operation)
        result["duplicate"] = duplicate
        if conflicts:
            result["conflicts"] = conflicts
        return result

    @app.post("/api/live/rooms/{room_id}/presence")
    async def update_live_presence(
        room_id: str,
        payload: LivePresenceUpdate,
        user: str = Depends(require_user),
    ) -> list[dict[str, Any]]:
        with database.connect() as connection:
            room = connection.execute(
                "SELECT id FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            details = payload.model_dump(exclude_none=True)
            details["role"] = role_for(connection, room_id, user)
        return await presence_registry.mark_presence(
            room_id, user, details=details
        )

    @app.delete("/api/live/rooms/{room_id}/presence")
    async def leave_live_room(
        room_id: str, user: str = Depends(require_user)
    ) -> dict[str, bool]:
        with database.transaction() as connection:
            room = connection.execute(
                "SELECT id FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            append_audit(connection, room_id, "room.leave", user)
        await presence_registry.remove_presence(room_id, user)
        return {"left": True}

    @app.get("/api/live/rooms/{room_id}/messages")
    def list_chat_messages(
        room_id: str,
        after: int = 0,
        limit: int = 200,
        user: str = Depends(require_user),
    ) -> list[dict[str, Any]]:
        del user
        if after < 0:
            raise HTTPException(status_code=400, detail="after must be non-negative")
        limit = max(1, min(int(limit), 500))
        with database.connect() as connection:
            room = connection.execute(
                "SELECT id FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            rows = connection.execute(
                """
                SELECT * FROM live_chat_messages
                WHERE room_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (room_id, after, limit),
            ).fetchall()
        return [public_chat_message(row) for row in rows]

    @app.post("/api/live/rooms/{room_id}/messages", status_code=201)
    def create_chat_message(
        room_id: str,
        payload: LiveChatMessageCreate,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        duplicate = False
        with database.transaction(immediate=True) as connection:
            room = connection.execute(
                "SELECT id FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            message = connection.execute(
                """
                SELECT * FROM live_chat_messages
                WHERE room_id = ? AND author = ? AND client_message_id = ?
                """,
                (room_id, user, payload.client_message_id),
            ).fetchone()
            if message is None:
                message_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO live_chat_messages(
                        id, room_id, client_message_id, author, text, anchor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        room_id,
                        payload.client_message_id,
                        user,
                        payload.text,
                        json.dumps(payload.anchor, separators=(",", ":"))
                        if payload.anchor
                        else None,
                        iso_now(),
                    ),
                )
                message = connection.execute(
                    "SELECT * FROM live_chat_messages WHERE id = ?", (message_id,)
                ).fetchone()
                append_audit(
                    connection,
                    room_id,
                    "chat.message",
                    user,
                    {"has_anchor": bool(payload.anchor)},
                )
            else:
                duplicate = True
        result = public_chat_message(message)
        result["duplicate"] = duplicate
        return result

    @app.get("/api/live/rooms/{room_id}/locks")
    def list_segment_locks(
        room_id: str, user: str = Depends(require_user)
    ) -> list[dict[str, Any]]:
        del user
        with database.connect() as connection:
            room = connection.execute(
                "SELECT id FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            rows = connection.execute(
                """
                SELECT segment_id, owner, locked, expires_at, expires_epoch,
                       updated_by, updated_at
                FROM live_segment_locks
                WHERE room_id = ?
                ORDER BY segment_id COLLATE NOCASE
                """,
                (room_id,),
            ).fetchall()
        now_epoch = datetime.now(timezone.utc).timestamp()
        return [
            {
                **dict(row),
                "locked": bool(row["locked"])
                and not (float(row["expires_epoch"] or 0.0) and now_epoch >= float(row["expires_epoch"])),
            }
            for row in rows
        ]

    @app.put("/api/live/rooms/{room_id}/locks/{segment_id}")
    def update_segment_lock(
        room_id: str,
        segment_id: str,
        payload: LiveSegmentLockUpdate,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            lock_state = connection.execute(
                """
                SELECT * FROM live_segment_locks
                WHERE room_id = ? AND segment_id = ?
                """,
                (room_id, segment_id),
            ).fetchone()
            if lock_state is None:
                raise HTTPException(
                    status_code=409,
                    detail="The label must synchronize once before it can be locked",
                )
            if lock_state["owner"] != user and role_for(connection, room_id, user) != "admin":
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Only {lock_state['owner']}, who created this label, "
                        "can change its lock"
                    ),
                )
            expires_epoch = (
                datetime.now(timezone.utc).timestamp() + payload.expires_minutes * 60.0
                if payload.locked and payload.expires_minutes
                else 0.0
            )
            expires_at = (
                datetime.fromtimestamp(expires_epoch, timezone.utc).isoformat()
                if expires_epoch
                else None
            )
            connection.execute(
                """
                UPDATE live_segment_locks
                SET locked = ?, expires_at = ?, expires_epoch = ?,
                    updated_by = ?, updated_at = ?
                WHERE room_id = ? AND segment_id = ?
                """,
                (
                    int(payload.locked),
                    expires_at,
                    expires_epoch,
                    user,
                    iso_now(),
                    room_id,
                    segment_id,
                ),
            )
            updated = connection.execute(
                """
                SELECT segment_id, owner, locked, expires_at, expires_epoch,
                       updated_by, updated_at
                FROM live_segment_locks
                WHERE room_id = ? AND segment_id = ?
                """,
                (room_id, segment_id),
            ).fetchone()
            append_audit(
                connection,
                room_id,
                "label.lock",
                user,
                {
                    "segment_id": segment_id,
                    "locked": payload.locked,
                    "expires_at": expires_at,
                },
            )
        return {**dict(updated), "locked": bool(updated["locked"])}

    @app.get("/api/live/rooms/{room_id}/roles")
    def list_room_roles(
        room_id: str, user: str = Depends(require_user)
    ) -> list[dict[str, str]]:
        del user
        with database.connect() as connection:
            room = connection.execute(
                "SELECT created_by FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="Live room not found")
            rows = connection.execute(
                "SELECT user, role FROM live_room_roles WHERE room_id = ? ORDER BY user COLLATE NOCASE",
                (room_id,),
            ).fetchall()
        roles = {str(row["user"]): str(row["role"]) for row in rows}
        roles[str(room["created_by"])] = "admin"
        return [{"user": name, "role": role} for name, role in sorted(roles.items())]

    @app.put("/api/live/rooms/{room_id}/roles/{target_user}")
    def update_room_role(
        room_id: str,
        target_user: str,
        payload: LiveRoleUpdate,
        user: str = Depends(require_user),
    ) -> dict[str, str]:
        with database.transaction(immediate=True) as connection:
            if role_for(connection, room_id, user) != "admin":
                raise HTTPException(status_code=403, detail="Only an administrator can change roles")
            room = connection.execute(
                "SELECT created_by FROM live_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if target_user == room["created_by"] and payload.role != "admin":
                raise HTTPException(status_code=409, detail="Room creator must remain administrator")
            connection.execute(
                """
                INSERT INTO live_room_roles(room_id, user, role, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(room_id, user) DO UPDATE SET
                    role=excluded.role, updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (room_id, target_user, payload.role, user, iso_now()),
            )
            append_audit(
                connection,
                room_id,
                "room.role",
                user,
                {"target_user": target_user, "role": payload.role},
            )
        return {"user": target_user, "role": payload.role}

    @app.get("/api/live/rooms/{room_id}/reviews")
    def list_reviews(
        room_id: str, user: str = Depends(require_user)
    ) -> list[dict[str, Any]]:
        del user
        with database.connect() as connection:
            role_for(connection, room_id, "__room-check__")
            rows = connection.execute(
                "SELECT * FROM live_reviews WHERE room_id = ? ORDER BY segment_id COLLATE NOCASE",
                (room_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @app.put("/api/live/rooms/{room_id}/reviews/{segment_id}")
    def update_review(
        room_id: str,
        segment_id: str,
        payload: LiveReviewUpdate,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            role = role_for(connection, room_id, user)
            if role == "viewer":
                raise HTTPException(status_code=403, detail="Viewers cannot change review state")
            if payload.state in {"approved", "changes_requested"} and role not in {
                "reviewer",
                "admin",
            }:
                raise HTTPException(status_code=403, detail="Reviewer role required")
            connection.execute(
                """
                INSERT INTO live_reviews(
                    room_id, segment_id, state, note, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id, segment_id) DO UPDATE SET
                    state=excluded.state, note=excluded.note,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at
                """,
                (room_id, segment_id, payload.state, payload.note, user, iso_now()),
            )
            if payload.state == "approved":
                connection.execute(
                    """
                    UPDATE live_segment_locks
                    SET locked = 1, expires_at = NULL, expires_epoch = 0,
                        updated_by = ?, updated_at = ?
                    WHERE room_id = ? AND segment_id = ?
                    """,
                    (user, iso_now(), room_id, segment_id),
                )
            append_audit(
                connection,
                room_id,
                "label.review",
                user,
                {"segment_id": segment_id, "state": payload.state, "note": payload.note},
            )
            row = connection.execute(
                "SELECT * FROM live_reviews WHERE room_id = ? AND segment_id = ?",
                (room_id, segment_id),
            ).fetchone()
        return dict(row)

    @app.post("/api/live/rooms/{room_id}/access-requests", status_code=201)
    def create_access_request(
        room_id: str,
        payload: LiveAccessRequestCreate,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        record = {
            "id": request_id,
            "room_id": room_id,
            "segment_id": payload.segment_id,
            "requester": user,
            "message": payload.message,
            "status": "pending",
            "created_at": iso_now(),
        }
        with database.transaction(immediate=True) as connection:
            role_for(connection, room_id, user)
            connection.execute(
                """
                INSERT INTO live_access_requests(
                    id, room_id, segment_id, requester, message, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record[key] for key in ("id", "room_id", "segment_id", "requester", "message", "status", "created_at")),
            )
            append_audit(connection, room_id, "label.access-request", user, record)
        return record

    @app.get("/api/live/rooms/{room_id}/access-requests")
    def list_access_requests(
        room_id: str,
        segment_id: str | None = None,
        user: str = Depends(require_user),
    ) -> list[dict[str, Any]]:
        del user
        with database.connect() as connection:
            role_for(connection, room_id, "__room-check__")
            if segment_id:
                rows = connection.execute(
                    "SELECT * FROM live_access_requests WHERE room_id = ? AND segment_id = ? ORDER BY created_at",
                    (room_id, segment_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM live_access_requests WHERE room_id = ? ORDER BY created_at",
                    (room_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/live/rooms/{room_id}/locks/{segment_id}/transfer")
    def transfer_owner(
        room_id: str,
        segment_id: str,
        payload: LiveOwnerTransfer,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            lock = connection.execute(
                "SELECT * FROM live_segment_locks WHERE room_id = ? AND segment_id = ?",
                (room_id, segment_id),
            ).fetchone()
            if lock is None:
                raise HTTPException(status_code=404, detail="Label lock record not found")
            if lock["owner"] != user and role_for(connection, room_id, user) != "admin":
                raise HTTPException(status_code=403, detail="Owner or administrator required")
            connection.execute(
                """
                UPDATE live_segment_locks SET owner = ?, updated_by = ?, updated_at = ?
                WHERE room_id = ? AND segment_id = ?
                """,
                (payload.target_user, user, iso_now(), room_id, segment_id),
            )
            connection.execute(
                """
                UPDATE live_access_requests
                SET status = 'accepted'
                WHERE room_id = ? AND segment_id = ? AND requester = ?
                  AND status = 'pending'
                """,
                (room_id, segment_id, payload.target_user),
            )
            append_audit(
                connection,
                room_id,
                "label.owner-transfer",
                user,
                {"segment_id": segment_id, "from": lock["owner"], "to": payload.target_user},
            )
            updated = connection.execute(
                "SELECT * FROM live_segment_locks WHERE room_id = ? AND segment_id = ?",
                (room_id, segment_id),
            ).fetchone()
        return {**dict(updated), "locked": bool(updated["locked"])}

    @app.get("/api/live/rooms/{room_id}/conflicts")
    def list_conflicts(
        room_id: str,
        unresolved_only: bool = False,
        user: str = Depends(require_user),
    ) -> list[dict[str, Any]]:
        del user
        with database.connect() as connection:
            role_for(connection, room_id, "__room-check__")
            query = "SELECT * FROM live_conflicts WHERE room_id = ?"
            values: list[Any] = [room_id]
            if unresolved_only:
                query += " AND resolution = 'unresolved'"
            query += " ORDER BY sequence DESC"
            rows = connection.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    @app.put("/api/live/rooms/{room_id}/conflicts/{conflict_id}")
    def resolve_conflict(
        room_id: str,
        conflict_id: str,
        payload: LiveConflictResolution,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            role_for(connection, room_id, user)
            connection.execute(
                """
                UPDATE live_conflicts
                SET resolution = ?, resolved_by = ?, resolved_at = ?
                WHERE room_id = ? AND id = ?
                """,
                (payload.resolution, user, iso_now(), room_id, conflict_id),
            )
            row = connection.execute(
                "SELECT * FROM live_conflicts WHERE room_id = ? AND id = ?",
                (room_id, conflict_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Conflict not found")
            append_audit(connection, room_id, "conflict.resolve", user, dict(row))
        return dict(row)

    @app.get("/api/live/rooms/{room_id}/material-template")
    def get_material_template(
        room_id: str, user: str = Depends(require_user)
    ) -> dict[str, Any] | None:
        del user
        with database.connect() as connection:
            role_for(connection, room_id, "__room-check__")
            row = connection.execute(
                "SELECT payload FROM live_material_templates WHERE room_id = ?", (room_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    @app.put("/api/live/rooms/{room_id}/material-template")
    def update_material_template(
        room_id: str,
        payload: LiveMaterialTemplate,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            if role_for(connection, room_id, user) != "admin":
                raise HTTPException(status_code=403, detail="Administrator role required")
            value = payload.model_dump()
            connection.execute(
                """
                INSERT INTO live_material_templates(room_id, payload, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    payload=excluded.payload, updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (room_id, json.dumps(value, separators=(",", ":")), user, iso_now()),
            )
            append_audit(
                connection,
                room_id,
                "room.material-template",
                user,
                {"segments": len(value["segments"])},
            )
        return value

    @app.get("/api/live/rooms/{room_id}/audit")
    def list_audit(
        room_id: str,
        limit: int = 100,
        user: str = Depends(require_user),
    ) -> list[dict[str, Any]]:
        del user
        with database.connect() as connection:
            role_for(connection, room_id, "__room-check__")
            rows = connection.execute(
                """
                SELECT * FROM live_audit WHERE room_id = ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (room_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    return app


app = create_app()
