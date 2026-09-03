from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class LivePresenceRegistry:
    def __init__(self) -> None:
        self._presence: dict[str, dict[str, dict]] = defaultdict(dict)
        self._guard = asyncio.Lock()

    async def mark_presence(
        self,
        room_id: str,
        user: str,
        details: dict | None = None,
        expiry_seconds: float = 45,
    ) -> list[dict]:
        now = time.time()
        async with self._guard:
            users = self._presence[room_id]
            previous_details = users.get(user, {}).get("details", {})
            users[user] = {
                "seen": now,
                "details": previous_details if details is None else dict(details),
            }
            expired = [
                name
                for name, presence in users.items()
                if now - float(presence["seen"]) > expiry_seconds
            ]
            for name in expired:
                users.pop(name, None)

            result = []
            for name, presence in sorted(users.items()):
                entry = {
                    "user": name,
                    "last_seen_seconds_ago": round(now - float(presence["seen"]), 1),
                }
                entry.update(presence.get("details") or {})
                result.append(entry)
            return result

    async def remove_presence(
        self, room_id: str, user: str, presence_session_id: str | None = None
    ) -> None:
        async with self._guard:
            users = self._presence.get(room_id)
            if users is None:
                return
            if presence_session_id:
                current_session_id = str(
                    users.get(user, {}).get("details", {}).get(
                        "presence_session_id", ""
                    )
                )
                if current_session_id and current_session_id != presence_session_id:
                    return
            users.pop(user, None)
            if not users:
                self._presence.pop(room_id, None)
