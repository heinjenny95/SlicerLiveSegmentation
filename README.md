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

1. Extract `SlicerLiveSegmentation-module-0.11.0.zip` completely.
2. Double-click `Install-LiveSegmentation.cmd` in the extracted folder.
3. Close all running Slicer windows.
4. Open the new desktop shortcut **Live Segmentation**.

The installer copies only this module to
`Documents\SlicerExtensions\LiveSegmentation-0.11.0`. Other Slicer extensions and
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
  a slow connection. **Sync now** immediately requests participants, edits,
  chat, locks, and a health check without rescanning history or backups.
- **Permanent room chat:** messages are stored in the room and reappear after
  leaving, restarting Slicer, or joining from another computer. Messages can
  carry a Slicer crosshair/slice location and jump collaborators directly there.
  The sender sees a message immediately while it is persisted independently in
  the background. A persistent Slicer dock keeps chat visible while Segment
  Editor or another module is active.
- **Live spatial presence:** online users publish active label, editor effect,
  crosshair and slice offsets. Users can jump to a collaborator or follow that
  person's view. Remote voxel changes receive a short-lived colored outline.
- **Label ownership and locks:** the user whose first synchronized operation
  creates a label owns it. Locks may expire, access can be requested, ownership
  can be transferred, and room administrators can resolve abandoned locks. An
  explicit **Label to manage** selector controls which label these actions affect.
  Other users' edits to a locked label are reverted locally and rejected at the
  transport boundary.
- **Roles and review:** rooms support viewer, editor, reviewer, and administrator
  roles. Labels progress through draft, in-progress, review, changes-requested,
  and approved states; approval locks the reviewed label.
- **Live activity and version timeline:** label creation and editing entries
  appear as soon as their ordered operation arrives (for example,
  `07:34 Jenny created label “Label 1”`). A selected revision can be restored as
  a new append-only revision, so no history is silently rewritten.
- **Snapshots and compaction:** complete segment states are periodically appended
  as one cropped clear snapshot plus zero-sparse 64³ chunk patches using the
  normal backwards-compatible operation format. Widely separated components do
  not create a temporary dense bounding box. Older loose operation
  files move into ZIP archives, keeping join time and folder enumeration bounded
  while retaining historical restore data.
- **Conflict review:** overlapping concurrent voxel operations produce permanent
  conflict records. Users can accept the latest state, reapply their version,
  use the other revision, or merge the two masks by union.
- **Automatic project backups:** in shared-folder mode, a complete `.mrb` Slicer
  project bundle is saved at the selected interval (five minutes by default).
  Backups have SHA-256 sidecars, a configurable retention limit, pinning,
  verification, restore controls, and an immediate **Back up now** action. The
  settings remain editable while the room is active.
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
- **Low-latency shared-folder I/O:** edit upload/download, chat send/receive,
  presence, lock set/read, health, and maintenance use independent worker lanes.
  Compact recent-operation, chat, and label-owner indexes avoid relisting large
  network directories during every poll. Ordering remains serialized only where
  the same label or the global voxel sequence requires it.
- **Region-based Slicer updates:** local masks are read directly from their
  effective internal labelmap extents and incoming edits mutate only the changed
  voxel box. Small brush strokes no longer export, copy, or re-import the entire
  source-volume geometry.
- **Sparse large-volume baselines:** confirmed label state is retained in
  independently allocated 64×64×64 chunks. Empty source-volume space consumes no
  baseline memory, erased chunks are released, and diagnostics expose allocated
  chunk bytes. This prevents every label from retaining a dense copy of a large
  microscopy volume.
- **One-read live edit feed:** the complete newest voxel operations are embedded
  in a bounded atomic hot feed. Receivers no longer wait for a state-file read
  followed by a second network-file read. Append-only archives, retry indexes,
  and unchanged label-owner metadata are maintained outside the visible path.
- **Targeted Slicer observation:** when Slicer identifies the modified segment,
  only that label is compared. Editing one label no longer scans every label in
  the shared segmentation; generic third-party editing remains supported through
  a conservative fallback.
- **Event-driven slice synchronization:** shared-folder changes wake the receiver
  immediately, while a lightweight revision probe catches Segment Editor effects
  that update the selected binary labelmap before emitting their final high-level
  event. The 100 ms edit poll remains only as a compatibility fallback.
- **Foreground-first SMB scheduling:** live hot-feed publication and peer reads
  receive a short uncontended window before append-only archives, indexes, audit,
  and conflict analysis use the network share. Repeated edits of the same label
  are no longer held behind this maintenance work.

## Large-dataset considerations

Live Segmentation's confirmed baselines and checkpoints are sparse and chunked,
so the extension itself no longer retains a full source-volume-sized byte mask
for every label. Total memory still includes the source image, Slicer's active
binary labelmap representations, display data, and undo history. In particular,
Slicer may store one label's binary representation over the bounding extent
between its outermost components. A label that densely spans an entire 1024³
volume can therefore still require substantial RAM even though the collaboration
baseline remains sparse. Non-linear parent-transform geometries also use Slicer's
general full-volume resampling fallback for correctness.

For exceptionally large projects, keep unrelated distant structures in separate
labels when scientifically appropriate, monitor Slicer process memory, and first
test the intended volume, label count, undo settings, and network share on a
representative workstation.

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
- 36 automated transport, API, chat-anchor, chunked snapshot/compaction, history,
  conflict, role, review, lock, template, invitation, diagnostics, backup,
  authentication, and delta tests pass.
- Two simultaneously running Slicer 5.12.3 processes synchronize a 27-voxel
  edit and an 8-voxel return edit in both directions through a shared folder.
- Two Slicer processes synchronized a 27-voxel edit on a 512×512×512 reference
  volume; the current sparse-baseline run published in about 0.28 seconds and
  restored 27/27 voxels after a clean rejoin.
- Two simultaneous Slicer processes on a 256×256×256 volume converged on 35/35
  voxels after a bidirectional edit and received all 24/24 voxels from three
  rapid disconnected additions to the same label.
- A separate two-Slicer round trip through the real LSDF share published the
  sender edit in 0.343 seconds and applied it remotely in 0.448 seconds.
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
