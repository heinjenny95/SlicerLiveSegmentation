# Live Segmentation for 3D Slicer

[![CI](https://github.com/heinjenny95/SlicerLiveSegmentation/actions/workflows/ci.yml/badge.svg)](https://github.com/heinjenny95/SlicerLiveSegmentation/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Public source repository: https://github.com/heinjenny95/SlicerLiveSegmentation

Live Segmentation is a small, independent 3D Slicer extension that synchronizes
a standard MRML Segmentation node between collaborators. It does not contain a
segmentation algorithm, inference backend, custom drawing workflow, or project
management system.

Users draw with Slicer's built-in **Segment Editor**. Any separately installed
module that modifies the same Segmentation node can be used as well; Live
Segmentation observes the resulting MRML changes without importing or replacing
that module.

## Windows installation

1. Extract `SlicerLiveSegmentation-module-0.8.0.zip` completely.
2. Double-click `Install-LiveSegmentation.cmd` in the extracted folder.
3. Close all running Slicer windows.
4. Open the new desktop shortcut **Live Segmentation**.

The installer copies only this module to
`Documents\SlicerExtensions\LiveSegmentation-0.8.0`. Other Slicer extensions and
their settings remain unchanged.

Alternatively, add the extracted `LiveSegmentation` directory under
**Edit > Application Settings > Modules > Additional module paths** and restart
Slicer.

## Collaboration through a shared folder

Both users:

1. load the exact same source volume;
2. open **Live Segmentation**;
3. select the source volume; leave **Shared segmentation** empty unless creating
   a new room from an existing segmentation;
4. select **Shared/network folder** and choose the same writable folder;
5. enter different display names but the same room name;
6. click **Join live room**;
7. click **Open Segment Editor** and edit normally.

The room manages the shared segmentation automatically. The first participant
may seed a newly created room from an explicitly selected existing segmentation.
Every participant joining an existing room receives a clean local replica that
is rebuilt from the room history with identical segment IDs. A local node exists
on each computer because Slicer scenes are local, but they represent and update
the same room segmentation.

Drive letters may differ between computers as long as both paths refer to the
same shared directory. The extension creates a `LiveSegmentation/rooms`
subdirectory containing room metadata, ordered voxel operations, and expiring
presence records. It does not copy the source image into that directory.

The client checks a signature derived from volume geometry and distributed
content samples. A different source volume cannot join the same room.
Independent voxel edits are combined. If two operations assign different values
to the same voxel, the operation ordered later by the shared transport wins.

## Collaboration controls

- **Connection monitoring:** every active room is checked continuously. A failed
  server or network-folder access immediately changes the status from green to
  red and shows a clear error. Round trips of 2.5 seconds or more are marked as
  a slow connection. **Refresh now** performs an immediate health, participant,
  edit, chat, and lock refresh without leaving the room.
- **Permanent room chat:** messages are stored in the room and reappear after
  leaving, restarting Slicer, or joining from another computer. Messages can
  carry a Slicer crosshair/slice location and jump collaborators directly there.
- **Live spatial presence:** online users publish active label, editor effect,
  crosshair and slice offsets. Users can jump to a collaborator or follow that
  person's view. Remote voxel changes receive a short-lived colored outline.
- **Label ownership and locks:** the user whose first synchronized operation
  creates a label owns it. Locks may expire, access can be requested, ownership
  can be transferred, and room administrators can resolve abandoned locks.
  Other users' edits to a locked label are reverted locally and rejected at the
  transport boundary.
- **Roles and review:** rooms support viewer, editor, reviewer, and administrator
  roles. Labels progress through draft, in-progress, review, changes-requested,
  and approved states; approval locks the reviewed label.
- **Version timeline:** every voxel operation remains auditable. A selected
  revision can be restored as a new append-only revision, so no history is
  silently rewritten.
- **Snapshots and compaction:** full segment snapshots are periodically appended
  using the normal backwards-compatible operation format. Older loose operation
  files move into ZIP archives, keeping join time and folder enumeration bounded
  while retaining historical restore data.
- **Conflict review:** overlapping concurrent voxel operations produce permanent
  conflict records. Users can accept the latest state, reapply their version,
  use the other revision, or merge the two masks by union.
- **Automatic project backups:** in shared-folder mode, a complete `.mrb` Slicer
  project bundle is saved at the selected interval (five minutes by default).
  Backups have SHA-256 sidecars, a configurable retention limit, pinning,
  verification, and restore controls.
- **Diagnostics:** the module reports transport latency, read/write health, free
  space, operation/snapshot/archive counts, pending changes, and backup count.
  A sanitized diagnostic JSON report intentionally omits users, room names,
  server URLs, and shared-folder paths.
- **Room material templates:** administrators can publish required label IDs,
  names, colors, and Slicer/DICOM terminology entries. The template creates
  missing labels without replacing existing voxel data.
- **Portable invitations:** secret-free `.liveseg` files populate room and
  connection settings and verify that the selected source volume matches.
- **Clean leave/rejoin:** leaving detaches Segment Editor and removes every local
  room replica. Rejoining always rebuilds exactly one clean replica from the
  permanent operation history.

## Optional collaboration server

The connection selector also offers **Collaboration server**. This transport is
optional and uses the same ordered-operation protocol. A localhost URL is only
reachable on that one computer; collaborators on different computers need a
shared network address.

For institutional deployment, use per-user bearer tokens, a TLS reverse proxy,
and the HTTPS enforcement option described in `docs/SERVER_DEPLOYMENT.md`. The
legacy shared API key does not verify individual identities.

## Verification

- Ruff and Python compilation pass.
- 21 automated transport, API, chat-anchor, snapshot/compaction, history,
  conflict, role, review, lock, template, invitation, diagnostics, backup,
  authentication, and delta tests pass.
- Two simultaneously running Slicer 5.12.3 processes synchronize a 27-voxel
  edit and an 8-voxel return edit in both directions through a shared folder.
- A joining client starts without a Segmentation node, receives the room node
  automatically, and the built-in Segment Editor edits that exact node.
- The module contains no bundled inference session or custom segmentation UI.
- Slicer lifecycle tests confirm that leave clears the shared-segmentation field,
  Segment Editor, and MRML room node; rejoin creates exactly one replica.
- A real complete-project backup was written and verified as a valid `.mrb` bundle.
- The full advanced Slicer smoke test passed rich presence, spatial chat,
  templates, review/access requests, snapshot compaction, conflict detection,
  diagnostics, backup verification, connection recovery, and clean rejoin.

Use only an institutionally approved shared folder with suitable access
controls. Segmentation changes may themselves represent sensitive research or
medical information. This is research software, not a certified medical device.

## Support and contributions

Report reproducible defects through the public
[issue tracker](https://github.com/heinjenny95/SlicerLiveSegmentation/issues),
use [Discussions](https://github.com/heinjenny95/SlicerLiveSegmentation/discussions)
for usage questions, and read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing
code changes. Security-sensitive reports should follow [SECURITY.md](SECURITY.md).
