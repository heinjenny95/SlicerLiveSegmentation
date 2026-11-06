"""A lock poll must not emit 600 native Modified events for unchanged tags."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests import controller_harness as h


def test_repeated_lock_poll_does_not_rewrite_unchanged_native_tags(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("Jenny", scene)
    peer.node.GetSegmentation().AddSegment(h.FakeSegment(), "A")
    segment = peer.node.GetSegmentation().GetSegment("A")
    segment.GetTag = lambda key: segment.tags.get(key)
    original = segment.SetTag
    segment.SetTag = Mock(side_effect=original)
    peer.controller.segment_owners["A"] = "Jenny"
    peer.controller._set_segment_collaboration_tags("A")
    assert segment.SetTag.call_count == 3
    for _ in range(100):
        peer.controller._set_segment_collaboration_tags("A")
    assert segment.SetTag.call_count == 3
    peer.controller.segment_locks_state["A"] = {
        "locked": True, "lock_holder": "Pauline", "lock_mode": "all"}
    peer.controller._set_segment_collaboration_tags("A")
    assert segment.tags["LiveSegmentation.LockedBy"] == "Pauline"
    assert segment.tags["LiveSegmentation.LockMode"] == "all"
    assert segment.SetTag.call_count == 5


def test_lock_poll_lists_once_instead_of_probing_every_label(tmp_path, monkeypatch):
    client = h.collaboration.SharedFolderRoomClient(tmp_path, "Jenny")
    room = client.join("many-labels", "a" * 64)
    creators = {f"label-{index}": "Jenny" for index in range(200)}
    monkeypatch.setattr(client, "_segment_creators", lambda path: creators)
    checks = []
    original = Path.is_file

    def counted(path):
        if path.parent.name == "locks":
            checks.append(path.name)
        return original(path)

    monkeypatch.setattr(Path, "is_file", counted)
    result = client.segment_locks(room["id"])
    assert len(result) == 200
    assert all(not row["locked"] for row in result)
    assert not checks, "An idle poll still probed one nonexistent lock per label"


def test_initial_labels_are_inserted_in_room_order_not_moved_after_creation(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("Jenny", scene)
    controller = peer.controller
    controller._confirmed_segment_order = ["A", "B", "C"]
    added = []
    segmentation = peer.node.GetSegmentation()
    original = segmentation.AddSegment

    def add(segment, sid, insert_before=""):
        added.append((sid, insert_before, segment.GetName()))
        return original(segment, sid, insert_before)

    monkeypatch.setattr(segmentation, "AddSegment", add)
    for sid in ("C", "A", "B"):
        controller._ensure_segment(peer.node, {"segment_id": sid, "segment_name": "name-"+sid})
    assert added == [("C", "", "name-C"), ("A", "C", "name-A"), ("B", "C", "name-B")]
    assert segmentation.GetSegmentIDs() == ["A", "B", "C"]


def test_initial_native_batch_is_balanced_and_does_not_own_foreign_batches(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("Jenny", scene)
    scene.BatchProcessState = 1
    scene.StartState = Mock()
    scene.EndState = Mock()
    scene.IsBatchProcessing = lambda: False
    peer.controller.initial_sync_complete = False
    peer.controller.initial_sequence = 200
    peer.controller._begin_initial_scene_batch()
    peer.controller._begin_initial_scene_batch()
    scene.StartState.assert_called_once_with(1)
    peer.controller._end_initial_scene_batch()
    peer.controller._end_initial_scene_batch()
    scene.EndState.assert_called_once_with(1)
    scene.IsBatchProcessing = lambda: True
    peer.controller._begin_initial_scene_batch()
    peer.controller._end_initial_scene_batch()
    assert scene.EndState.call_count == 1


def test_initial_replay_refreshes_label_list_at_readiness(monkeypatch):
    scene, status_messages = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("Jenny", scene)
    controller = peer.controller
    controller.initial_sync_complete = False
    controller.initial_sequence = 2
    controller._refresh_label_combo = Mock()
    room = h.RoomLog()
    for label in ("A", "B", "C"):
        operation = room.commit({
            "segment_id": label,
            "segment_name": label,
            **h.collaboration.encode_mask_crop_snapshot(None, None, h.SHAPE),
        }, "Pauline")
        peer.apply(operation)
        if label == "A":
            controller._refresh_label_combo.assert_not_called()
            assert not status_messages
        elif label == "B":
            controller._refresh_label_combo.assert_called_once()
            assert not status_messages
        else:
            assert controller._refresh_label_combo.call_count == 2
            assert status_messages == ["Live: Pauline updated C"]
    assert controller.initial_sync_complete
    assert peer.node.GetSegmentation().GetSegmentIDs() == ["A", "B", "C"]


@pytest.mark.parametrize("initial", [True, False])
def test_incoming_continuation_yields_without_sleep_only_during_join(monkeypatch, initial):
    import sys

    scene, _ = h.install_fake_slicer(monkeypatch)
    controller = h.Peer("Jenny", scene).controller
    controller.initial_sync_complete = not initial
    controller.initial_sequence = 200
    controller._incoming_operations.append({"sequence": 1})
    monkeypatch.setattr(h.collaboration, "GUI_WORK_BUDGET_SECONDS", 0)
    schedule = Mock()
    monkeypatch.setattr(sys.modules["qt"].QTimer, "singleShot", schedule)
    controller._drain_incoming_operations()
    assert schedule.call_args.args[0] == (0 if initial else h.collaboration.GUI_CONTINUATION_MILLISECONDS)
    assert len(controller._incoming_operations) == 1


def test_folder_push_does_not_compete_with_its_own_sequence_lock(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    controller = h.Peer("Jenny", scene).controller
    client = Mock()
    client.push_operation.return_value = {"sequence": 1}
    monkeypatch.setattr(h.collaboration, "_uses_shared_folder", lambda client: True)
    monkeypatch.setattr(h.collaboration, "_parallel_map", lambda *a, **k: pytest.fail("parallel folder writers"))
    outgoing = [{"segment_id": sid, "client_operation_id": sid} for sid in ("order", "A", "B")]
    controller._edit_push_lane(controller._session_token, client, "room", outgoing, [], "")
    result = controller._worker_results.get_nowait()
    assert not result.get("error")
    assert result["outgoing_ids"] == ["order", "A", "B"]
    assert [call.args[1]["segment_id"] for call in client.push_operation.call_args_list] == ["order", "A", "B"]
