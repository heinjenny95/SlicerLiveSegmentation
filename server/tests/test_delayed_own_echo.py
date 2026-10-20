"""Delayed acknowledgements must not replay intermediate local drawing states."""
from unittest.mock import Mock

import numpy as np
import pytest

from tests.controller_harness import SHAPE, Peer, RoomLog, install_fake_slicer
from tests.controller_harness import collaboration as c


@pytest.mark.parametrize("push_acknowledged", [False, True])
@pytest.mark.parametrize("erase_encoded", [False, True])
def test_old_own_add_echo_does_not_resurrect_a_later_local_erase(monkeypatch, push_acknowledged, erase_encoded):
    scene, _ = install_fake_slicer(monkeypatch)
    peer = Peer("author", scene)
    log = RoomLog()
    seed = log.commit({"client_operation_id": "seed", "segment_id": "A",
                       "segment_name": "A", "metadata_update": True,
                       **c.encode_mask_delta(np.zeros(SHAPE, np.uint8), np.zeros(SHAPE, np.uint8), replace=True)}, "author")
    peer.apply(seed)
    peer.paint("A", (3, 3, 3), 1)
    peer.prepare()
    peer.paint("A", (3, 3, 3), 0)
    if erase_encoded:
        peer.prepare()
    pending = list(peer.controller.outgoing)
    assert len(pending) == (2 if erase_encoded else 1)
    if push_acknowledged:
        peer.finish_push(accepted=pending)
    writer = Mock(wraps=peer.controller.owner.update_segment_binary_labelmap_crop)
    peer.controller.owner.update_segment_binary_labelmap_crop = writer
    peer.apply(log.commit(pending[0], "author"))
    assert peer.mask("A")[3, 3, 3] == 0, "Own echo resurrected an already erased voxel"
    writer.assert_not_called()
    if erase_encoded:
        peer.apply(log.commit(pending[1], "author"))
    assert not np.any(peer.mask("A"))
    assert not peer.controller.awaiting_echo


def test_same_display_name_is_not_treated_as_a_known_local_echo(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    peer = Peer("author", scene)
    log = RoomLog()
    empty = np.zeros(SHAPE, np.uint8)
    peer.apply(log.commit({"client_operation_id": "seed", "segment_id": "A",
                           **c.encode_mask_delta(empty, empty, replace=True)}, "author"))
    foreign = empty.copy()
    foreign[2, 2, 2] = 1
    peer.apply(log.commit({"client_operation_id": "different-device", "segment_id": "A",
                           **c.encode_mask_delta(empty, foreign)}, "author"))
    assert peer.mask("A")[2, 2, 2] == 1


def test_known_visible_own_echo_does_not_preallocate_native_extent(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    peer, log = Peer("author", scene), RoomLog()
    peer.apply(log.commit({"segment_id": "A", "client_operation_id": "seed",
                           **c.encode_metadata_update(SHAPE)}, "author"))
    peer.paint("A", (3, 3, 3), 1)
    peer.prepare()
    pending = list(peer.controller.outgoing)
    peer.controller.owner.prepare_segment_labelmap_extent = Mock(return_value=iter(()))
    peer.apply(log.commit(pending[0], "author"))
    peer.controller.owner.prepare_segment_labelmap_extent.assert_not_called()
