# Shared label order

Use the same current test build on every participant before testing this feature.
After saving and restarting Slicer through the updated starter, join the same
room. In nnInteractive IR, drag a **label name** up or down in the embedded
Segment Editor. Outside a room this is local; in a connected room Live
Segmentation shares the native order with everyone. A dedicated metadata-only
operation carries stable IDs, not labelmap arrays. Label names, colors, locks,
voxel ownership and each peer's selected editing target are unchanged.

Concurrent reorders resolve in the room's stored sequence order (last accepted
order wins for its stated IDs). New labels unknown to that drag are retained;
deleted IDs in a stale order never recreate labels. Read-only viewers cannot
reorder the room. Label-content locks do not block order changes. Checkpoints,
compaction, historical state and later joins retain the shared order.

For HTTP/HTTPS installations, update the server code as well as the clients.
The optional `segment_order` field is stored in a nullable SQLite column added
by the normal startup migration. Shared Folder, SSH helper and Direct LAN use
the same ordered room log. This does not change the base plugin version or
require deleting an existing room. Older clients do not synchronize order;
do not mix test builds for a shared-order session.

Native segmentation saves preserve list order. When exporting integer labelmaps,
consult the export manifest's label-value mapping, which can reflect list order.

## Manual acceptance

1. A and B select different labels. A drags a name; both lists must agree while
   B's selected label, name/color and painted voxels remain unchanged.
2. Both drag different labels at nearly the same time. Once pending operations
   settle, both lists must agree. Sorting must not generate voxel-edit activity.
3. Add/delete a label while another participant sorts. Retain new labels; never
   revive a deleted one. Confirm ordinary paint still targets the selected ID.
4. Leave, rejoin, and join from a third Slicer after a room checkpoint. Verify
   identical order and voxel contents. Do this in a separate test room first.

Automated native coverage: `scripts/slicer_label_order_probe.py` drives real Qt
mouse events and two independent MRML/editor parameter nodes with a private
folder room, then tests a third late join after compaction. Transport/controller
tests cover concurrent orders, validation, read-only viewers and encrypted SSH.
