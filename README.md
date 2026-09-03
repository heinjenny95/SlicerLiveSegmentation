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

1. Extract `SlicerLiveSegmentation-module-0.14.0.zip` completely.
2. Double-click `Install-LiveSegmentation.cmd` in the extracted folder.
3. Close all running Slicer windows.
4. Open the new desktop shortcut **Live Segmentation**.

The installer also creates **Live Segmentation Safe Start**. It launches Slicer
with `--disable-settings --ignore-slicerrc` and therefore bypasses stale scene,
module, startup-script, or network state from the normal Slicer profile. Use it
when an existing Slicer profile cannot reach its main window; it does not delete
or overwrite the normal profile.

The installer copies only this module to
`Documents\SlicerExtensions\LiveSegmentation-0.14.0`. Other Slicer extensions and
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

Version 0.11.2 adds the first structural room operation: deleting a complete
label removes it for every participant and from reconstructed/rejoined room
state. All participants in a shared-folder room must update to 0.11.2; joining
an older room upgrades its protocol metadata so earlier clients fail clearly
instead of silently retaining deleted labels.

Version 0.11.3 makes network failure safe for Slicer itself. Room names,
shared-folder paths, and server targets are session-only; a launch starts with
blank room/shared-folder fields and the neutral local-server default. Joining,
leaving, and health checks never wait for an SMB
or UNC path on the GUI thread. A connection attempt that does not answer within
four seconds is cancelled locally; an active shared-folder session with no
usable response for three seconds is reset locally, including removal of its
replica from the Slicer scene. The user can immediately select another location
or close Slicer even if Windows continues waiting on the old network path in a
background daemon thread.

Version 0.11.4 accommodates the cold-start latency of institutional SMB shares.
The non-blocking connection watchdog now allows 15 seconds and shows elapsed
time while connecting. Active shared-folder sessions allow 10 seconds for a
live-sync or read/write-health response, preventing a single brief network stall
from being mistaken for an outage while keeping the UI responsive and locally
cancellable throughout.

Version 0.11.5 separates a slow active session from a confirmed outage. Ten
seconds without a completed request now produces an orange connection warning
and an immediate health probe without removing the shared segmentation. The
hard local reset occurs only after 30 seconds of complete silence or an explicit
failed read/write health check. Time spent applying a large received voxel patch
inside Slicer is refreshed after local processing and no longer counts as
network silence.

Version 0.11.5 also clears legacy room, transport, shared-folder, and server
targets as soon as Slicer imports the module, before widget setup. The Windows
installer adds a **Live Segmentation Safe Start** shortcut that ignores saved
settings and the user startup script for that launch, so a damaged or stale
normal Slicer profile cannot prevent access to the plugin.

Version 0.11.6 adds a safe recent-folder dropdown. The active shared-folder
field still starts empty, but its dropdown offers up to eight locations that
previously completed a successful connection. Loading or opening the dropdown
only reads local text settings and never probes those network paths. Selecting
an entry copies it into the field; network access starts only after **Join live
room**. Failed locations are not remembered, and **Clear list** removes the
local history.

Version 0.12.0 adds an optional one-click direct-LAN relay with automatic
shared-folder fallback, live stage-by-stage latency measurements, a
non-destructive connection benchmark, persistent spatial comment threads, an
always-visible activity dock, collaborative undo, local crash recovery,
review queues, segmentation QA checks, revision comparison overlays,
double-clickable `.livesegroom` invitations, and anonymized session-metrics
export. The pre-existing room-template feature is unchanged.

Version 0.12.1 makes the module panel responsive on narrow and lower-resolution
displays. Wide action rows wrap into compact grids, while long lists continue to
use Slicer's normal scrolling. The measured minimum module-content width is 288
pixels instead of 741 pixels in version 0.12.0.

Version 0.13.0 adds a non-mutating two-computer **Check connection** preflight
before joining. It validates endpoint/folder reachability, atomic shared-folder
permissions, plugin and wire-protocol compatibility, source-volume identity,
clock skew, authentication, transport security, and recent visibility of the
other computer. The Remote HTTPS server mode works across ordinary internet
connections without an institutional intranet, shared drive, or VPN. A bundled
Docker Compose + Caddy deployment provides automatic TLS and per-user tokens;
plain HTTP is rejected for non-loopback server addresses unless the user
explicitly enables the local-test override.

Version 0.13.1 fixes false participant disappearance and severe delays observed
on a real multi-computer NAS session. High-frequency presence and rebuildable
hot-cache files no longer use replace-over-existing, which can stall for more
than a minute on some SMB servers. Sequence allocation now cross-checks the
immutable operation journal so a stale SMB cache cannot assign the same
sequence number to two edits. Polling falls back to immutable operation and
chat files whenever a state cache is stale. Presence leases survive transient
heartbeat delays, and a late cleanup from an older connection cannot remove a
newer session of the same user.

Version 0.14.0 gives every newly created collaborative label a room-global
identifier instead of publishing Slicer's scene-local `Segment_1`-style ID.
This prevents two users who create labels concurrently from crossing voxel data,
names, or colors. Label renames and color changes are explicit ordered room
operations and update the standard Segment Editor immediately; delayed paint
packets can no longer revert newer label properties. The live activity dock now
includes the current user's acknowledged changes. Direct LAN and Remote HTTPS
address controls appear directly below the connection selector, with the LAN
host creating its private local room store, URL, and session code automatically.

The safer global-label protocol is intentionally incompatible with earlier room
formats. Every collaborator must install 0.14.0 and create a new room; an older
room is rejected clearly instead of risking a wrong label assignment.

Drive letters may differ between computers as long as both paths refer to the
same shared directory. The extension creates a `LiveSegmentation/rooms`
subdirectory containing room metadata, ordered voxel operations, and expiring
presence records. It does not copy the source image into that directory.

The client checks a signature derived from volume geometry and distributed
content samples. A different source volume cannot join the same room.
Independent voxel edits are combined. If two operations assign different values
to the same voxel, the operation ordered later by the shared transport wins.

## Collaboration controls

- **Connection monitoring:** every active room is checked continuously. Ten
  seconds without a response produces a visible warning and health check;
  30 seconds of complete silence or an explicit failed validation resets locally.
  the UI remains responsive and never silently reconnects at the next launch.
  Round trips of 2.5 seconds or more are marked as a slow connection. **Sync
  now** immediately requests participants, edits, chat, locks, and a health
  check without rescanning history or backups.
- **Direct LAN with fallback:** one participant can start a lightweight LAN
  relay from the module; that host creates a private local room store and the
  displayed LAN URL and session code automatically. It does not create or
  require a shared folder. Export a `.livesegroom` invitation so collaborators
  prefer that direct HTTP path and automatically use the same shared-folder
  room when the relay is temporarily unavailable. The access-code relay is
  unencrypted and is intended only for a trusted institutional LAN or VPN.
- **Latency and benchmark panel:** publish, receive, apply/render, chat, and
  edit-roundtrip timings update during the session. **Benchmark** performs
  repeated non-destructive health and live-feed reads and rates the connection.
- **Permanent room chat:** messages are stored in the room and reappear after
  leaving, restarting Slicer, or joining from another computer. Messages can
  carry a Slicer crosshair/slice location and jump collaborators directly there.
  The sender sees a message immediately while it is persisted independently in
  the background. A persistent Slicer dock keeps chat visible while Segment
  Editor or another module is active.
- **Spatial comment threads:** a comment can be attached to the selected label,
  crosshair, and slice offsets, revisited from a queue, and resolved without
  deleting the permanent discussion record.
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
- **Live activity and version timeline:** label creation, editing, and deletion entries
  appear as soon as their ordered operation arrives (for example,
  `07:34 Jenny created label “Label 1”`). A selected revision can be restored as
  a new append-only revision, so no history is silently rewritten.
- **Collaborative undo and comparison:** **Undo my last shared edit** publishes
  an inverse operation instead of rewinding shared history and preserves voxels
  changed later by another participant. **Compare with current** creates a
  display-only Added/Removed overlay for a selected historical sequence.
- **Review queue and QA:** review states are sorted into a visible queue.
  On-demand quality checks report empty labels, disconnected or very small
  components, and overlaps between labels without changing voxel data.
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
- **Crash recovery and research metrics:** unacknowledged edits are journaled in
  local application storage and can be replayed idempotently after rejoining the
  same user/room/dataset/connection. An anonymized JSON export contains only
  timing distributions, byte/count totals, fallback counts, and aggregate QA
  counts—never user names, room names, URLs, or paths.
- **Room material templates:** administrators can publish required label IDs,
  names, colors, and Slicer/DICOM terminology entries. The template creates
  missing labels without replacing existing voxel data.
- **Portable invitations:** `.livesegroom` files populate room and connection
  settings, carry the temporary direct-LAN session code when applicable, and
  verify that the selected source volume matches. The Windows installer
  registers them for double-click opening; legacy `.liveseg` files still import.
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

## Remote internet collaboration

The connection selector offers **Remote HTTPS server**. This transport uses the
same ordered-operation protocol and works for collaborators outside KIT or any
other institutional network. A localhost URL is only reachable on that one
computer. Internet collaborators enter the same public HTTPS URL and their own
per-user access token; they do not need a shared folder.

The source release includes a reproducible public deployment under
`deploy/public`: Caddy provides and renews TLS, the application server remains
private inside Docker, and the token generator creates separate credentials.
See `docs/SERVER_DEPLOYMENT.md`. The legacy shared API key does not verify
individual identities and is not suitable for public exposure.

Before joining through any transport, both computers should run **Check
connection**. The check does not create or join the room. Run it on computer A,
then B, then once more on A to confirm that each machine sees the other's
two-minute preflight beacon. See `docs/TWO_COMPUTER_PREFLIGHT.md`.

## Verification

- Ruff and Python compilation pass.
- 66 automated transport, API, preflight, HTTPS-policy, direct-LAN/fallback, crash-recovery, QA,
  chat-anchor, chunked snapshot/compaction, history,
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
- A separate two-Slicer round trip through an institutional SMB share published the
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
- A real Slicer 5.12.3 deletion smoke test confirmed that local label removal
  publishes a tombstone, a peer deletion removes the complete MRML segment,
  historical reconstruction excludes it, and it remains absent after rejoin.
- A real Slicer 5.12.3 direct-LAN smoke test joined through the one-click relay,
  published a 27-voxel edit in about 0.2 seconds, and passed spatial comments,
  QA, benchmark, review queue, revision overlay, collaborative undo, and
  end-to-end latency-metrics checks.

Use only an institutionally approved shared folder with suitable access
controls. Segmentation changes may themselves represent sensitive research or
medical information. This is research software, not a certified medical device.

## Support and contributions

Report reproducible defects through the public
[issue tracker](https://github.com/heinjenny95/SlicerLiveSegmentation/issues),
use [Discussions](https://github.com/heinjenny95/SlicerLiveSegmentation/discussions)
for usage questions, and read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing
code changes. Security-sensitive reports should follow [SECURITY.md](SECURITY.md).
