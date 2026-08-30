from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    api_key: str | None = None
    max_upload_bytes: int = 64 * 1024 * 1024
    user_tokens: dict[str, str] | None = None
    require_https: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        default_data_root = Path(__file__).resolve().parents[1] / "data"
        data_root = Path(os.getenv("LIVESEG_DATA_ROOT", str(default_data_root))).resolve()
        raw_tokens = os.getenv("LIVESEG_USER_TOKENS_JSON", "").strip()
        user_tokens = None
        if raw_tokens:
            parsed = json.loads(raw_tokens)
            if not isinstance(parsed, dict):
                raise ValueError("LIVESEG_USER_TOKENS_JSON must be a JSON object")
            user_tokens = {str(token): str(user) for token, user in parsed.items()}
        return cls(
            database_path=Path(
                os.getenv("LIVESEG_DATABASE_PATH", str(data_root / "liveseg.sqlite3"))
            ).resolve(),
            api_key=os.getenv("LIVESEG_API_KEY") or None,
            max_upload_bytes=max(
                1024 * 1024,
                int(os.getenv("LIVESEG_MAX_UPLOAD_BYTES", str(64 * 1024 * 1024))),
            ),
            user_tokens=user_tokens,
            require_https=os.getenv("LIVESEG_REQUIRE_HTTPS", "").lower()
            in {"1", "true", "yes"},
        )
