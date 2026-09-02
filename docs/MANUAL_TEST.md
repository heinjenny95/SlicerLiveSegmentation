# Manual two-user acceptance test

## Preparation

1. Create an empty folder in a shared location writable by both test users.
2. Open two Slicer instances and load the same non-sensitive source volume.
3. Open `Live Segmentation` and select the source volume in both instances. Do
   not create or select a Segmentation node.
4. Select `Shared/network folder`, use the same shared folder and room name, and
   enter different user names.
5. Join the room in both instances.
6. Verify that each instance automatically shows a room-managed node under
   `Shared segmentation`.

## Standard Segment Editor

1. In instance A click `Open Segment Editor`.
2. Verify that Segment Editor displays the selected source volume and the
   automatically selected room Segmentation node.
3. Add a segment and paint a small area.
4. Verify that the segment and voxel edit appear in instance B without manual
   save or pull.
5. In B add a non-overlapping area to the same segment and verify both areas in A.
6. Erase part of the segment in B and verify the removal in A.

## Rapid same-label strokes

1. Select a single label in instance A and paint three disconnected components
   in quick succession without waiting for the peer between strokes.
2. Verify instance B receives all three components, in order, without pressing
   **Sync now**.
3. Repeat while the first component is still being transmitted over a slower
   network share. Verify no component is lost or assigned to a different label.
4. Select a different label and repeat. Confirm the Segment Editor selection,
   live history, and the received label identity all agree.

## Separately installed tools

1. Keep the live room connected.
2. Open any separately installed segmentation tool and configure it to edit the
   same Segmentation node.
3. Apply one edit and verify that it appears in the other Slicer instance.

## Parallel shared-folder load

1. Create at least four labels and distribute them between the two users.
2. Paint different labels simultaneously while sending chat messages and
   changing a lock on another label.
3. Verify edits inside each label retain their local order while edits to
   different labels, chat, presence, and locks continue without waiting for one
   another.
4. Join a third disposable client after at least 25 operations. Verify the
   operation files are loaded in sequence order and the reconstructed masks are
   identical to both active clients.
5. Open history, conflicts, reviews, roles, and snapshots together with
   **Refresh advanced data**. Verify the independent datasets load without
   pausing live edits or chat.

## Conflicts and protection

1. Edit two non-overlapping areas simultaneously and verify both remain.
2. Assign different values to the same voxel and verify both clients converge on
   the later ordered operation.
3. Attempt to join the room with a different source volume and verify rejection.
4. Lock A's selected label and verify B cannot keep or publish an edit to it.
5. Unlock the label and verify B can edit it again.
6. Use **Label to manage** to switch labels. Confirm the lock status and Segment
   Editor selection follow the visible choice.

## Connection, chat, backups, and lifecycle

1. Interrupt shared-folder or server access and verify the green online state is
   replaced by a red connection error and a visible dialog.
2. Restore access and click **Sync now**. Verify the room remains joined and
   returns to green without using Slicer's developer **Reload** action.
3. Send a chat message and verify it appears immediately for its sender and in
   the other instance without pressing **Sync now**. Leave and rejoin and verify
   the full chat remains.
4. Verify both participants appear online within two seconds in both instances.
5. While both users paint, send chat and lock/unlock a label. Verify none of
   these actions disconnects either participant or waits for the voxel sync.
6. In shared-folder mode change backup enablement, interval, and retention while
   connected. Use **Back up now** and verify a versioned `.mrb` appears under the
   room's `backups` directory while older bundles remain.
7. Leave the room and verify the shared-segmentation selector, Segment Editor
   label table, and room node are cleared.
8. Rejoin repeatedly and verify exactly one room segmentation is present.

## Advanced collaboration

1. Move Alice's crosshair and slice views. On Thomas, select Alice and test
   **Jump to user** and **Follow view**. Confirm no voxel data changes.
2. Paint on Alice and confirm Thomas sees a short-lived colored outline exactly
   over the changed area.
3. Send an anchored chat message and verify **Jump to message location** restores
   the saved view after reconnecting.
4. Create overlapping edits from the same starting revision. Verify a conflict
   reports label and voxel count; test latest, mine, other, and union resolution
   on disposable synthetic data.
5. Set Thomas to viewer and confirm edits are reverted and rejected. Set reviewer
   and confirm approval locks the label.
6. Request access to a locked label, transfer ownership, use a temporary lock,
   and confirm an administrator can resolve an abandoned lock.
7. Publish a material template with names, colors, and terminology. Join from a
   clean scene and verify missing labels appear without replacing voxel data.
8. Export and import a `.liveseg` invitation. Verify it contains no API key and
   rejects a different source volume.
9. Create a checkpoint and confirm older loose operations move into a ZIP
   archive. A fresh client must reconstruct the same segmentation, and the
   timeline must restore a pre-checkpoint revision.
10. Run diagnostics online and offline. Verify the exported report omits users,
    room names, server URLs, and shared-folder paths.
11. Pin, verify, and restore a disposable MRB backup. Confirm retention removes
    only the oldest unpinned backups.

## Automated Slicer probe

`scripts/slicer_live_segmentation_smoke_test.py` runs in two simultaneous Slicer
processes. The consumer starts without a Segmentation node and must receive the
room node plus the producer's 27 voxels. It then adds 8 voxels to the same
segment; both processes must converge on 35 voxels without any artificial
generic `Modified()` call. With `LIVE_SEGMENTATION_SMOKE_RAPID_COMPONENTS=1`,
the producer also adds three disconnected components to the same existing label
at 120 ms intervals. The third notification intentionally precedes its voxel
write; the peer must still receive all 24 new voxels.
