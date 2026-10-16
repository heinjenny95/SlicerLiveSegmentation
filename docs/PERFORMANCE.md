# Responsiveness validation

## Standard editor first-open source preparation (unreleased)

The earlier lazy-Threshold fix removes preparation of an unused effect, but the
source intensity range is also requested by the native editor's masking UI.
VTK 9.6.2's image override visits every voxel individually even when an ordinary
source has no blanking/ghost information. This caused a measured 47.25-second
GUI gap on a decoded 9.46-GB uint8 test volume.

`LiveSegmentationLib/native_range.py` primes the same VTK image cache by calling
the exported `vtkDataSet::ComputeScalarRange` base implementation explicitly.
It uses VTK's optimized array reduction and normal cache invalidation. It does
not alter voxel values, image/node identity, extent, orientation, spacing,
display settings, pipeline connections, label IDs, or the active segmentation.
All calls occur on the GUI thread, before native source callbacks; no full
source copy, worker-side MRML access, process-memory patching or DLL replacement
is involved. Array and image revision checks avoid redundant work.

This method is **protected C++ API**, not a supported public Python interface.
The bridge is deliberately gated to the tested **Windows x64 / Slicer 5.12.3
revision 34627 / VTK 9.6.2** ABI, the installed application's own VTK library,
an exact typed function signature and a mandatory tiny-image equivalence and
revision-invalidation self-test. No guessed field offsets or arbitrary shared
libraries are used. An unsupported profile fails closed to ordinary Slicer.

Only standard-memory-layout, nonempty, single-component integer `vtkImageData`
with matching point counts, no cell scalars and no point/cell ghost arrays is
accelerated. Float/NaN/Inf, vector, subclass and ghost/blanking cases retain the
native path; they are not silently approximated. A large-source fallback emits
a single diagnostic rather than changing the image or disconnecting the room.
For a public cross-platform release, validate additional ABIs explicitly or
integrate the fast path upstream in VTK instead of broadening the gate by guess.

Run `scripts/slicer_editor_open_probe.py` with `LIVE_EDITOR_OUTPUT` and optionally
`LIVE_EDITOR_DATASET` (using the separately installed BigTIFF reader). Set
`LIVE_EDITOR_ASSERT_FAST=1` to require a maximum first-open GUI gap below one
second. Loading/decompressing the file is measured separately. Native checks
also cover exact Threshold, actual Paint/Erase, image/revision preservation,
manual source selection, parameter replacement, eight integer types with
nonzero extents, safe rejection of unsupported inputs, repeat binding and cleanup.
Recorded local native results (one run each; loading excluded):

| Source | Old first-open GUI gap | New editor binding | New maximum GUI gap |
| --- | ---: | ---: | ---: |
| 1 GiB synthetic uint8 | 4.609 s | 0.063 s | 0.188 s |
| 9.464 GB decoded uint8 BigTIFF, 3697 x 1600 x 1600 | 47.250 s | 0.562 s | 0.578 s |

The real file took 263.266 s to load/decode separately; this change does not
accelerate TIFF decoding. These are measured local tests, not a promise about
all hardware, datasets, native effects or AI initialization.

## What version 0.14.7 changes

Network/disk waits are not performed on Slicer's GUI thread. Receiving a result
is also bounded: bit-packed decompression happens on a worker, followed by small
ordered 64-cubed MRML updates with an 8 ms soft work budget. Qt receives control
between batches. The count/byte-limited queue applies backpressure when drawing
cannot keep up. No intermediate tile advances the committed room sequence.

Local array capture yields between approximately 1 MiB slabs. A revision check
rejects captures changed while copying; comparisons and compression operate on
private NumPy data on a background thread. Bounded 128-cubed operations replace
large union-shaped temporary buffers. Baseline chunks use copy-on-write so
background encoding and incoming edits cannot modify each other's snapshots.

Aligned labelmaps are updated directly in the changed region. Larger extents
are staged incrementally and committed only if the source revision is still
current. Updates preserve segment IDs, label values, colors, and visibility.
Derived meshes are invalidated only for affected labels. Checkpoint compression
and recovery-journal writes are also off the GUI thread.

## Reproducible local probe

Run `scripts/slicer_responsiveness_smoke_test.py` using Slicer's `--python-script`
option and this release's `--additional-module-path`. Set
`LIVE_RESPONSIVENESS_OUTPUT` to an absolute JSON output path. On Windows the
validated run used `QT_OPENGL=desktop`; the default ANGLE backend on the test
machine could not create an OpenGL context. The probe enables the standard
Segment Editor and a rendered red slice view. It does not initialize an AI model.

The synthetic source is 384 x 384 x 384 voxels (56,623,104 voxels). Two distant
foreground points first force a large native labelmap extent. An independent
transport client then publishes 24 small brush operations and a dense remote
mask. A final local stroke checks the outbound path. A 10 ms Qt timer measures
event-loop gaps; all measured scenarios must stay below 250 ms to pass.

Recorded source-release run on Windows / Slicer 5.12.3:

| Scenario | Time to completion | Largest Qt heartbeat gap |
| --- | ---: | ---: |
| 24 remote brush operations, published 10 ms apart | 0.609 s total | 47 ms |
| Remote dense mask, 96-cubed / 884,736 voxels | 0.219 s | 47 ms |
| Local 64-voxel edit into the large native layer | 0.312 s | 31 ms |
| Storage read artificially delayed by 2 s | 2 s injected wait | 16 ms |

These are one-run synthetic observations, not statistical bounds or a network
SLA. Brush completion includes the publication interval and all 24 operations,
not the latency of one stroke. The stalled-read test verifies independence of
transport waiting from the GUI; it does not claim that missing network data can
be displayed before it arrives. The receiver is a real Slicer process, while
the burst publisher is a separate background transport client. A separate
two-Slicer test checks simultaneous overlapping labels and convergence.

## Limits and fair expectations

- First joining an old room still needs history/checkpoint transfer and replay.
  It should make incremental progress, not pretend to be instantly complete.
- End-to-end delay includes capture, encode, network/SMB publication, receiving,
  and rendering. A saturated host, NAS cache, VPN, or disconnected share can
  increase it beyond the local values. Running a storage host and heavy AI on
  one memory-starved computer also affects its network-serving performance.
- The 8 ms budget is checked between native calls. It cannot interrupt a native
  Slicer/VTK call already running. Non-linear or otherwise incompatible geometry
  retains the general Slicer resampling fallback for correctness.
- Full `.mrb` project saving is an explicit opt-in and may block the saving
  computer. Do not enable frequent full project bundles for very large sources
  when measuring interactive latency. Ordered voxel history remains automatic.
- Local AI initialization, other extensions, GPU drivers, mesh generation,
  and operating-system memory pressure remain outside the collaboration
  scheduler's control. A universal promise that Slicer never freezes would be
  misleading. This release specifically reduces collaboration-triggered work.
- Large incoming masks become visible progressively. The committed sequence
  advances only when the entire operation has been processed, so a temporary
  partial display is not a completed synchronization acknowledgement.

For a two-computer validation, install the same release on both computers, use
the same source and room, and compare the plugin's local-encode, GUI-incoming,
edit-push, edit-pull, and round-trip measurements. Use separate labels first,
then intentionally overlapping strokes; verify final ownership as well as speed.
