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

## Separately installed tools

1. Keep the live room connected.
2. Open any separately installed segmentation tool and configure it to edit the
   same Segmentation node.
3. Apply one edit and verify that it appears in the other Slicer instance.

## Conflicts and protection

1. Edit two non-overlapping areas simultaneously and verify both remain.
2. Assign different values to the same voxel and verify both clients converge on
   the later ordered operation.
3. Attempt to join the room with a different source volume and verify rejection.
4. Lock A's selected label and verify B cannot keep or publish an edit to it.
5. Unlock the label and verify B can edit it again.

## Connection, chat, backups, and lifecycle

1. Interrupt shared-folder or server access and verify the green online state is
   replaced by a red connection error and a visible dialog.
2. Restore access and click **Refresh now**. Verify the room remains joined and
   returns to green without using Slicer's developer **Reload** action.
3. Exchange chat messages, leave and rejoin, and verify the full chat remains.
4. In shared-folder mode verify versioned `.mrb` bundles appear under the room's
   `backups` directory and older bundles remain.
5. Leave the room and verify the shared-segmentation selector, Segment Editor
   label table, and room node are cleared.
6. Rejoin repeatedly and verify exactly one room segmentation is present.

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
generic `Modified()` call.
