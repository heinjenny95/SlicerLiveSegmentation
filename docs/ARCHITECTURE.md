# Architecture

## Components

1. `LiveSegmentation` is an independent scripted 3D Slicer module. It selects a
   standard source volume, creates or loads the room-managed MRML Segmentation
   node, and opens Slicer's built-in Segment Editor.
2. The default shared-folder transport stores room metadata, ordered operations,
   and expiring presence records using atomic file publication.
3. An optional FastAPI transport exposes the same live-room operations through
   HTTP and persists them in SQLite.
4. Drawing and inference tools are outside this extension. They interoperate by
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
             +------ ordered log ----+
                         |
                         v
              collaborating clients
```

Each operation contains a stable client-operation ID, segment ID, name, color,
volume geometry, voxel bounding box, a packed changed-voxel mask, and packed
binary values. Bit fields are compressed with zlib and encoded as Base64.

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

Segmentation checkpoints are appended as contiguous groups of normal `snapshot`
operations. Snapshot payloads contain only each segment's effective non-zero
extent (or a zero-changed-voxel one-voxel marker for an empty label), while still
reconstructing a complete segment state. This keeps them readable by
operation-only clients without transferring a source-volume-sized zero field. Once a complete
group exists, older loose operation JSON files move into a ZIP archive. Timeline
and restoration read active and archived records, while new joins replay only a
recent full snapshot group and newer patches.

The MRML Segmentation node is a disposable local room replica. Leave detaches it
from Segment Editor and removes it from the scene. Session tokens make delayed
worker results harmless, and rejoin always reconstructs one new replica from the
ordered room history.

## Persistence

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
