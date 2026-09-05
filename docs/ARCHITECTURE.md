# Architecture

## Components

1. `LiveSegmentation` is an independent scripted 3D Slicer module. It selects a
   standard source volume, creates or loads the room-managed MRML Segmentation
   node, and opens Slicer's built-in Segment Editor.
2. The default shared-folder transport stores room metadata, ordered operations,
   and expiring presence records using atomic file publication.
3. An optional FastAPI transport exposes the same live-room operations through
   HTTP and persists them in SQLite.
4. An optional in-process `ThreadingHTTPServer` exposes the shared-folder client
   on a trusted LAN. A hybrid client prefers this relay and uses the same
   persistent shared room as fallback. The relay uses only Python's standard
   library and a temporary access code.
5. Drawing and inference tools are outside this extension. They interoperate by
   modifying the room-managed MRML Segmentation node.

## Data flow

```text
Segment Editor or another separately installed tool
                         |
                         v
              MRML Segmentation node
                         |
                  changed voxels
                         v
          compressed delta operation
                         |
             +-----------+-----------+
             |                       |
      shared/network folder    optional HTTP server
             |                       |
       optional LAN relay ------------+
             |       preferred path
             +------ ordered log -----+
                         |
                         v
              collaborating clients
```

Each operation contains a stable client-operation ID, segment ID, name, color,
volume geometry, voxel bounding box, a packed changed-voxel mask, and packed
binary values. Bit fields are compressed with zlib and encoded as Base64.

Each client retains confirmed label state in sparse 64×64×64 `uint8` chunks.
Chunks are allocated only when they contain foreground voxels and are removed
when they become empty. Incoming patches read and mutate only the affected
chunks. This makes persistent baseline memory proportional to occupied label
chunks rather than `volume voxels × label count`; the source image and Slicer's
own active binary labelmap remain under Slicer's normal memory management.

The first participant may seed a new room from an explicitly selected local
segmentation. A participant joining an existing room receives a fresh local
replica, and the ordered log recreates the same segment IDs and voxel state.
Slicer segmentation events (`SegmentAdded`, `SegmentModified`,
`SourceRepresentationModified`, and `SegmentRemoved`) drive change detection;
no manual save or generic MRML `Modified()` call is required.

## Concurrency

- Each room has one strictly increasing operation sequence.
- Shared-folder sequencing uses a short atomically created lock directory;
  abandoned locks expire.
- Repeating the same client-operation ID is idempotent.
- Independent voxel edits are combined.
- For conflicting values on one voxel, the later ordered operation wins.
- A newer unconfirmed local overlay remains visible when an older remote
  operation arrives and is then submitted as a later operation.
- Operations include the last global sequence known to their author. Concurrent
  operations touching the same segment and voxel region create a persistent
  conflict record in addition to deterministic ordering.

## Spatial collaboration

Presence contains active segment, active Segment Editor effect, stable user
color, crosshair RAS position, and Red/Yellow/Green slice offsets. Following a
user changes local view state only. Incoming changes create a separate temporary
display-only Segmentation node, so highlight geometry never enters the shared
operation observer.

Remote operations also append to a persistent local activity dock. Spatial
comments reuse the permanent chat log with a message-kind marker, stable comment
ID, segment metadata, crosshair and slice offsets; resolution is another
append-only chat event.

## Revisions, undo, quality, and metrics

Historical state reconstruction remains append-only. A comparison request
builds a separate display-only Segmentation node containing the union of voxels
added and removed since the chosen sequence. Collaborative undo reads the target
operation plus its immediate prior label state, then publishes an inverse patch.
It changes only target voxels that still equal the target operation's written
value, preserving later changes on the same region.

Quality checks copy current masks on the GUI thread and analyze them on a daemon
worker for empty labels, connected components, small components, and pairwise
overlap. Session metrics retain a bounded in-memory sample window per lane and
export only aggregate timing/count/byte statistics. Unacknowledged outgoing
operations are atomically journaled under Slicer's local application-data
directory, keyed by user, room, dataset signature, transport, and a hash of the
connection identity; no saved UNC path is probed during startup.

## Dataset protection, presence, and connection health

The first participant creates the room with a signature of volume dimensions,
type, geometry, and distributed content samples. A client with a different
signature cannot join.

About once per second, the client publishes the display name and active segment.
Presence expires automatically and is not part of the permanent segmentation
history.

Every active client performs periodic transport health checks and measures the
full synchronization round trip. A failed read/write marks the room offline in
the UI; a slow successful cycle is reported separately. Manual refresh uses the
same idempotent synchronization path and never resets the room session.

## Chat and label locks

Chat messages use a separate permanent ordered log with stable client-message
IDs. Label ownership is derived from the first accepted operation for each
segment ID. A persistent lock record can be changed only by that owner. Lock
enforcement occurs both in the Slicer controller and at the transport boundary.
Chat anchors add segment and view/crosshair coordinates. Locks may expire, their
owners can transfer them, and administrators can resolve abandoned ownership.

## Roles, review, and material templates

The room creator is the initial administrator. Users may be viewers, editors,
reviewers, or administrators. Review records use draft, in-progress,
ready-for-review, changes-requested, and approved states. Approval creates a
non-expiring lock. Templates store stable segment IDs, names, colors, and
optional DICOM-compatible terminology strings independently from voxel data.

## Project backups and local lifecycle

Shared-folder clients coordinate an interval lease and save full `.mrb` Slicer
project bundles under the room's `backups` directory. Bundles are staged locally
and atomically published; prior versions are never overwritten.

Segmentation checkpoints are appended as contiguous groups of normal operations.
For each segment, the first operation is a standard `snapshot` that clears the
old state and sets the first occupied chunk; additional occupied chunks are
ordinary `patch` operations. An empty label uses a zero-changed-voxel one-voxel
snapshot marker. The group therefore reconstructs a complete segment while no
temporary payload spans distant components or source-volume-sized zero fields.
Existing operation-only clients remain compatible because they already
understand the ordered snapshot-plus-patch semantics. Once a complete group
exists, older loose operation JSON files move into a ZIP archive. Timeline and
restoration read active and archived records, while new joins replay only a
recent full snapshot group and newer patches.

The MRML Segmentation node is a disposable local room replica. Leave detaches it
from Segment Editor and removes it from the scene. Session tokens make delayed
worker results harmless, and rejoin always reconstructs one new replica from the
ordered room history. Until the join watermark is reached, the pull lane remains
armed so a short SMB cache-visibility window during checkpoint publication cannot
leave a new replica at sequence zero.

## Persistence

### Cooperative GUI boundary (0.14.7)

The transport workers return compact, decompressed bit-packed masks. A bounded
queue limits queued operations and expanded packed bytes. The GUI expands only
64-cubed regions and applies them in room order with an 8 ms soft work budget;
5 ms continuations allow input and rendering between batches. Snapshot clearing
uses occupied baseline chunks, including old regions outside the new snapshot.
The original operation is acknowledged and added to history only after all its
regions finish. Capacity growth is staged incrementally with revision checks.

Outgoing mask capture is cooperative and revision-checked. Private arrays and
copy-on-write baseline forks are compared/compressed on a worker into bounded
128-cubed operations. Metadata-only edits bypass voxel capture. The local
recovery journal has an independent coalescing writer; a blocked file write never
holds its submission lock. No MRML objects are accessed by these encode workers.
See [performance validation and limits](PERFORMANCE.md).

### Durable room data

The shared-folder layout contains `room.json`, active operation and archived
history records, snapshot manifests, permanent spatial chat, conflict, role,
review, access-request, template, audit and lock records, versioned `.mrb`
backups, and one presence file per user under `LiveSegmentation/rooms`. Presence
normally uses atomic replacement; on SMB shares that reject replacement of an
existing file it falls back to direct overwrite. Readers ignore temporary or
temporarily incomplete presence files. Room metadata and segmentation
operations always retain atomic publication. The optional server stores room,
operation, chat, and label-lock records in SQLite. The extended server schema
also stores roles, review state, access requests, conflict decisions, material
templates, and audit events. Optional per-user bearer tokens bind identity to a
fixed user; HTTPS can be required behind a TLS reverse proxy.
