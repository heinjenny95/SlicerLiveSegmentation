"""Synchronization traffic must not claim that the observer manually edited."""
import numpy as np
import pytest

from tests import controller_harness as h


@pytest.mark.parametrize("metadata", [True, False])
def test_unchanged_metadata_or_snapshot_is_not_a_manual_edit(monkeypatch, metadata):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("local", scene)
    mask = np.zeros(h.SHAPE, np.uint8)
    mask[2, 2, 2] = 1
    initial = h.collaboration.encode_mask_delta(np.zeros_like(mask), mask, replace=True)
    common = {"segment_id": "A", "segment_name": "A", "color_hex": "#FF0000", "author": "local"}
    peer.apply({**initial, **common, "sequence": 1, "client_operation_id": "initial"})
    peer.activity.clear()
    encoded = h.collaboration.encode_metadata_update(h.SHAPE) if metadata else initial
    peer.apply({**encoded, **common, "metadata_update": metadata, "sequence": 2,
                "client_operation_id": "same-state"})
    assert not peer.activity, "Unchanged protocol state was reported as a user edit"
    np.testing.assert_array_equal(peer.mask("A"), mask)
    assert peer.controller.last_sequence == 2


def test_real_remote_change_still_has_activity(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("local", scene)
    old = np.zeros(h.SHAPE, np.uint8)
    new = old.copy()
    new[3, 3, 3] = 1
    peer.apply({**h.collaboration.encode_mask_delta(old, new), "segment_id": "A", "segment_name": "A",
                "author": "other", "sequence": 1, "client_operation_id": "real"})
    assert len(peer.activity) == 1 and "other" in peer.activity[0]


def test_repeated_paint_and_own_confirmations_are_diagnostic_only(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("local", scene)
    common = {"segment_id": "A", "segment_name": "Mandible", "color_hex": "#FF0000"}
    peer.apply({**common, **h.collaboration.encode_metadata_update(h.SHAPE),
                "metadata_update": True, "author": "other", "sequence": 1})
    peer.activity.clear()
    before = np.zeros(h.SHAPE, np.uint8)
    for sequence in range(2, 8):
        after = before.copy()
        after[sequence, 1, 1] = 1
        peer.apply({**common, **h.collaboration.encode_mask_delta(before, after),
                    "author": "other", "sequence": sequence})
        before = after
    confirmation = {**common, **h.collaboration.encode_metadata_update(h.SHAPE),
                    "metadata_update": True, "author": "local", "sequence": 8,
                    "client_operation_id": "own-confirmation"}
    peer.controller.outgoing.append(confirmation)
    peer.apply(confirmation)
    assert not peer.activity
    np.testing.assert_array_equal(peer.mask("A"), before)
    assert peer.controller.last_sequence == 8


def test_coarse_label_lifecycle_and_order_without_sequence_numbers(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("local", scene)
    common = {"segment_id": "A", "segment_name": "Mandible", "author": "other"}
    peer.apply({**common, **h.collaboration.encode_metadata_update(h.SHAPE),
                "metadata_update": True, "sequence": 1})
    peer.apply({**common, **h.collaboration.encode_metadata_update(h.SHAPE),
                "segment_name": "Mandible left", "metadata_update": True, "sequence": 2})
    peer.controller._apply_operations([{"segment_order": ["A"], "author": "other", "sequence": 3}])
    peer.apply({**common, "segment_deleted": True, "sequence": 4})
    assert any("created label" in text for text in peer.activity)
    assert any("renamed" in text for text in peer.activity)
    assert any("deleted label" in text for text in peer.activity)
    assert all("sequence" not in text for text in peer.activity)
