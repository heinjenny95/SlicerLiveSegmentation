# Public HTTPS server deployment

Live Segmentation can connect collaborators on different institutional networks
or ordinary home internet connections. They do not need a shared drive, KIT
intranet, or VPN. One administrator must provide a public Linux server or cloud
VM with a DNS name and inbound TCP ports 80 and 443.

The included `deploy/public` stack runs the application behind Caddy. Caddy
obtains and renews the TLS certificate; the application port remains private to
the Docker network. Every collaborator receives a separate random bearer token
bound to the display name used in Slicer.

## Requirements

- a public Linux host with Docker Engine and Docker Compose v2;
- a DNS `A`/`AAAA` record pointing a name such as
  `collaboration.example.org` to that host;
- inbound TCP 80 and 443 allowed by the host and cloud firewall;
- a backup target for the persistent Docker volume.

Do not forward or expose Uvicorn port 8000. Direct LAN mode is unencrypted and
must never be port-forwarded to the public internet.

## First deployment

From the repository root on the server, generate a private environment file:

```bash
python3 scripts/generate_user_tokens.py --domain collaboration.example.org Alice Bob
```

The command creates `deploy/public/.env` and prints one private token per user.
The file is ignored by Git. Give each person only their own token through an
approved private channel.

Start the service:

```bash
docker compose --env-file deploy/public/.env -f deploy/public/docker-compose.yml up -d --build
```

Verify the public endpoint:

```bash
curl https://collaboration.example.org/health
```

The response reports the server version, collaboration protocol, required
minimum plugin version, server time, authentication mode, and HTTPS policy. It
does not reveal tokens or room data.

## Connect from Slicer

On every computer:

1. install the same current Live Segmentation release and load the exact same
   source volume;
2. choose **Remote HTTPS server**;
3. enter `https://collaboration.example.org`, the assigned display name, the
   matching private access token, and the same room name;
4. click **Check connection** on both computers, then rerun it on the first;
5. resolve every failed item and click **Join live room**.

The preflight is non-mutating: it does not create or join a room. It checks
reachability and authentication, TLS use, endpoint and plugin protocol versions,
source-volume compatibility, server/client clock difference, and whether the
other computer reached the same room preflight during the last two minutes.

## Identity modes

- `LIVESEG_USER_TOKENS_JSON` is the required public-deployment mode. It maps
  random bearer tokens to fixed user names, for example
  `{"random-token-for-alice":"Alice"}`. The Slicer name must match exactly.
- `LIVESEG_API_KEY` is a compatibility mode with one shared key. It does not
  verify individual identities and is unsuitable for public exposure.
- With neither variable set, the server is open for local testing only. The
  preflight displays a warning.

The public Compose bundle sets `LIVESEG_REQUIRE_HTTPS=true`. Caddy supplies the
forwarded HTTPS scheme, adds transport-security headers, compresses responses,
and rejects request bodies above 64 MiB before proxying them.

## Operation and backup

The server stores ordered operations, chat, roles, review state, conflicts,
templates, locks, and audit events in the `liveseg-data` Docker volume. Back up
that volume regularly and perform a restoration drill before relying on it.
Protect the host, Docker socket, `.env`, backups, and DNS account with normal
server security controls.

To update, fetch a reviewed release and run the same Compose command with
`--build`. Keep all collaborators on the same current plugin release and run the
preflight again before resuming work.

For institutional production, additionally define retention/deletion rules,
monitor availability and storage, rotate tokens, restrict administrative access,
and complete the relevant research-data/privacy review. This is research
software, not a certified medical device.

