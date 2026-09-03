"""Create a private Docker Compose environment file with per-user access tokens."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path


def generate_user_tokens(user_names):
    users = []
    seen = set()
    for value in user_names:
        user = " ".join(str(value or "").split())
        key = user.casefold()
        if not user or len(user) > 80:
            raise ValueError("User names must contain 1 to 80 visible characters")
        if key in seen:
            raise ValueError(f"Duplicate user name: {user}")
        seen.add(key)
        users.append(user)
    if not users:
        raise ValueError("Provide at least one user name")
    return {secrets.token_urlsafe(32): user for user in users}


def main():
    parser = argparse.ArgumentParser(
        description="Generate deploy/public/.env and print each user's private access token."
    )
    parser.add_argument("users", nargs="+", help="One or more Slicer display names")
    parser.add_argument("--domain", required=True, help="Public DNS name without https://")
    parser.add_argument("--email", default="", help="Optional TLS administrator email")
    parser.add_argument(
        "--output", type=Path, default=Path("deploy/public/.env"), help="Private .env path"
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing output file")
    args = parser.parse_args()
    domain = args.domain.strip().lower()
    if "://" in domain or "/" in domain or not domain:
        parser.error("--domain must be a DNS name without a scheme or path")
    if args.output.exists() and not args.force:
        parser.error(f"Refusing to replace existing file: {args.output}")
    tokens = generate_user_tokens(args.users)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(
            (
                f"LIVESEG_DOMAIN={domain}",
                f"LIVESEG_EMAIL={args.email.strip()}",
                "LIVESEG_USER_TOKENS_JSON="
                + json.dumps(tokens, ensure_ascii=False, separators=(",", ":")),
                "LIVESEG_MAX_UPLOAD_BYTES=67108864",
                "",
            )
        ),
        encoding="utf-8",
    )
    print(f"Created private environment file: {args.output}")
    print("Give each person only their own token:")
    for token, user in tokens.items():
        print(f"  {user}: {token}")


if __name__ == "__main__":
    main()

