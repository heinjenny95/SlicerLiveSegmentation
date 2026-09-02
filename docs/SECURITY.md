# Security and privacy

This development release is intended for controlled research environments and
trusted internal networks.

## Implemented safeguards

- room separation and source-volume signature checks;
- idempotent operation IDs;
- atomic room/operation publication and short shared-folder lock directories;
- presence-only direct-overwrite fallback for restrictive SMB shares;
- optional per-user bearer tokens that bind authentication to a fixed identity;
- compatibility-only shared bearer API key mode;
- optional HTTPS enforcement behind a reverse proxy;
- persistent room roles, review state, conflict decisions, and audit events;
- bounded server request fields and payload sizes;
- release allowlist excluding databases, image data, virtual environments, and
  secrets.
- direct-LAN requests restricted to an allowlisted collaboration API and
  protected by constant-time comparison of a temporary session code;
- anonymized metrics export excludes identities, room names, connection
  locations, images, and voxel payloads;
- crash-recovery journals are stored locally and loaded only for an exact
  context match.

## Direct-LAN responsibilities

The one-click relay uses plain HTTP and a bearer-like temporary session code; it
does not provide TLS, institutional identity, or protection against another
authorized participant. Use it only on a trusted institutional LAN or inside an
approved VPN. Do not expose its port to the public internet. A `.livesegroom`
invitation for direct LAN contains that session code, so distribute it through a
private approved channel and replace it for later sessions. Use the FastAPI
server behind TLS with per-user tokens when stronger transport and identity
controls are required.

## Shared-folder responsibilities

Segmentation operations may represent sensitive research or medical information.
Use only an institutionally approved folder with appropriate access-control
lists, encryption, backup, retention, and deletion policies. Anyone with write
access to the room directory can read or modify its operation files. This mode
does not provide identity verification or an immutable audit trail. Its audit
JSON records are traceability aids on a trusted share and can be altered by
anyone with write access. Complete `.mrb` backups may include source volumes,
scene metadata, paths, and other sensitive content—not just the segmentation.

## Before production or clinical use

- integration of per-user tokens with verified institutional identity;
- transport encryption where required;
- immutable audit events and validated timestamps;
- encrypted backups and tested restoration;
- retention and deletion rules;
- monitoring and payload limits appropriate to the institution;
- privacy, security, and regulatory review.

The extension is research software and not a certified medical device.
