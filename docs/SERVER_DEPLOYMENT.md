# Secure server deployment

The optional HTTP server is intended to run behind an institutional TLS reverse
proxy. Do not expose the development Uvicorn port directly to an untrusted
network.

## Identity modes

- `LIVESEG_USER_TOKENS_JSON` is the preferred mode. It maps bearer tokens to fixed
  user names, for example `{"random-token-for-alice":"alice"}`. The name in the
  Slicer client must match the authenticated token identity.
- `LIVESEG_API_KEY` is a compatibility mode with one shared bearer key. It does
  not verify individual identities and is unsuitable for an untrusted network.
- With neither variable set, the server is intentionally open for local testing.

Set `LIVESEG_REQUIRE_HTTPS=true` when the reverse proxy supplies
`X-Forwarded-Proto: https`. Use a secrets manager rather than committing tokens
to Docker Compose or source control.

The server stores operations, chat, roles, review state, conflicts, templates,
locks, and audit events in SQLite. Back up the data directory, restrict its file
permissions, and define institutional retention and deletion rules.

