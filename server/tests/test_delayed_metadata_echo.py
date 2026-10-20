"""A delayed own rename must not replace a newer local name/color."""
import pytest

from tests.controller_harness import SHAPE, Peer, RoomLog, install_fake_slicer
from tests.controller_harness import collaboration as c


@pytest.mark.parametrize("latest_encoded", [False, True])
def test_delayed_own_metadata_preserves_latest_local_name_and_color(monkeypatch, latest_encoded):
    scene, _ = install_fake_slicer(monkeypatch)
    peer, log = Peer("author", scene), RoomLog()
    seed = {"segment_id": "A", "segment_name": "A", "metadata_update": True,
            "client_operation_id": "seed", **c.encode_metadata_update(SHAPE)}
    peer.apply(log.commit(seed, "author"))
    first = {**seed, "client_operation_id": "rename-1", "segment_name": "Mandible", "color_hex": "#FF0000"}
    latest = {**seed, "client_operation_id": "rename-2", "segment_name": "Mandible left", "color_hex": "#0000FF"}
    peer.controller.outgoing = [first, latest] if latest_encoded else [first]
    if not latest_encoded:
        peer.controller.metadata_updates.add((peer.node.GetID(), "A"))
    segment = peer.node.GetSegmentation().GetSegment("A")
    segment.SetName(latest["segment_name"])
    segment.SetColor(0, 0, 1)
    peer.apply(log.commit(first, "author"))
    assert segment.GetName() == latest["segment_name"]
    assert tuple(segment.GetColor()) == (0, 0, 1)
