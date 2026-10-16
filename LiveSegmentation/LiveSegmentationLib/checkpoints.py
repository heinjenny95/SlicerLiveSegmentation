"""Private, cooperatively assembled room checkpoints; never render a prefix."""
from __future__ import annotations


class CheckpointGroup:
    def __init__(self, first, shape, baseline_factory, tile_iterator):
        self.group_id = str(first.get("snapshot_group_id") or "")
        self.count = int(first.get("snapshot_group_count") or 0)
        if not self.group_id or not 0 < self.count <= 100000 or int(first.get("snapshot_group_index", -1)) != 0:
            raise ValueError("Incomplete or invalid checkpoint header; local labels retained")
        self.first_sequence = int(first["sequence"])
        self.last_sequence = self.first_sequence - 1
        self.index = 0
        self.shape = tuple(shape)
        self.baselines = {}
        self.metadata = {}
        self.deletions = {}
        self.records = []
        self.baseline_factory = baseline_factory
        self.tile_iterator = tile_iterator

    @property
    def complete(self):
        return self.index == self.count

    def ingest(self, operation):
        if (not operation.get("system_snapshot")
                or operation.get("snapshot_group_id") != self.group_id
                or int(operation.get("snapshot_group_count", 0)) != self.count
                or int(operation.get("snapshot_group_index", -1)) != self.index
                or int(operation["sequence"]) != self.last_sequence + 1):
            raise ValueError("Checkpoint order changed or a part is missing; local labels retained")
        segment_id = str(operation["segment_id"])
        if operation.get("_undecodable"):
            raise ValueError("Damaged checkpoint part; local labels retained")
        if operation.get("segment_deleted"):
            self.deletions[segment_id] = operation
            self.baselines.pop(segment_id, None)
        else:
            baseline = self.baselines.setdefault(segment_id, self.baseline_factory(self.shape))
            if operation.get("operation_kind") == "snapshot":
                baseline.clear()
            for tile in self.tile_iterator(operation, baseline):
                if tile.get("_completion_only"):
                    continue
                changed, values = tile["_decoded"]
                baseline.set_region(tile["voxel_bbox"], changed, values)
                yield None
            self.metadata[segment_id] = operation
            self.deletions.pop(segment_id, None)
        self.records.append({key: value for key, value in operation.items() if not key.startswith("_")})
        self.index += 1
        self.last_sequence = int(operation["sequence"])


def same_baseline_steps(first, second):
    """Compare only occupied chunks; yield before the next bounded comparison."""
    if first is None or not hasattr(first, "chunks") or first.shape != second.shape:
        return False
    if first.chunks.keys() != second.chunks.keys():
        return False
    import numpy as np

    for coordinate, chunk in second.chunks.items():
        if not np.array_equal(first.chunks[coordinate], chunk):
            return False
        yield None
    return True
