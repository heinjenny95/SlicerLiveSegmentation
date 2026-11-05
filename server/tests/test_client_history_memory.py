"""The live timeline keeps bounded summaries without retaining voxel payloads."""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.controller_harness import collaboration as c


class Item:
    text_reads = 0

    def __init__(self, values):
        self.values = values
        self.hidden = False
        self.sequence = None

    def text(self, column):
        type(self).text_reads += 1
        return self.values[column]

    def setData(self, column, role, sequence):
        self.sequence = sequence

    def setHidden(self, hidden):
        self.hidden = hidden


class Tree:
    columnCount = 5

    def __init__(self):
        self.items = []

    @property
    def topLevelItemCount(self):
        return len(self.items)

    def insertTopLevelItem(self, index, item):
        # Native Qt ignores setHidden while an item is not yet in its tree.
        item.hidden = False
        self.items.insert(index, item)

    def topLevelItem(self, index):
        return self.items[index]

    def takeTopLevelItem(self, index):
        return self.items.pop(index)

    def clear(self):
        self.items.clear()


def timeline(monkeypatch, limit=32):
    monkeypatch.setitem(sys.modules, "qt", SimpleNamespace(QTreeWidgetItem=Item))
    monkeypatch.setattr(c, "HISTORY_ROW_LIMIT", limit, raising=False)
    Item.text_reads = 0
    controller = c.LiveCollaborationController.__new__(c.LiveCollaborationController)
    controller.user_name = "me"
    controller.history_records = []
    controller.history_sequences = set()
    controller.history_tree = Tree()
    controller.history_filter_edit = SimpleNamespace(text="")
    controller.undo_shared_button = SimpleNamespace(enabled=True)
    controller._queue_action = MagicMock()
    controller.on_timer = MagicMock()
    controller._show_error = MagicMock()
    return controller


def record(sequence, author="peer"):
    return {"sequence": sequence, "segment_id": "A", "segment_name": "Organ",
            "author": author, "client_operation_id": str(sequence),
            "created_at": "2026-11-05T12:00:00+00:00", "operation_kind": "patch",
            "changed_voxels": 1, "payload": "encoded voxels"}


def test_history_keeps_summaries_without_encoded_or_decoded_masks(monkeypatch):
    controller = timeline(monkeypatch)
    original = record(1)
    original.update(_packed=object(), _decoded=object())
    controller._append_history_operations([original])
    saved = controller.history_records[0]
    assert not {"payload", "_packed", "_decoded"}.intersection(saved)
    assert saved["client_operation_id"] == "1"
    assert saved["changed_voxels"] == 1
    assert original["payload"] == "encoded voxels", "Timeline mutated the authoritative operation"


def test_long_replay_bounds_timeline_records_rows_and_sequence_set(monkeypatch):
    controller = timeline(monkeypatch)
    for sequence in range(1, 2001):
        controller._append_history_operations([record(sequence)])
    assert len(controller.history_records) == 32
    assert controller.history_tree.topLevelItemCount == 32
    assert controller.history_sequences == set(range(1969, 2001))
    assert [item.sequence for item in controller.history_tree.items] == list(range(2000, 1968, -1))


def test_peer_replay_filters_new_rows_without_rescanning_the_whole_tree(monkeypatch):
    controller = timeline(monkeypatch)
    controller.history_filter_edit.text = "organ"
    for sequence in range(1, 1001):
        controller._append_history_operations([record(sequence)])
    assert Item.text_reads <= 5*1000, "Every operation reread all prior Qt row text"
    assert not any(item.hidden for item in controller.history_tree.items)
    controller.filter_history_tree("nobody")
    assert all(item.hidden for item in controller.history_tree.items)


def test_active_history_filter_hides_new_rows_after_native_qt_insertion(monkeypatch):
    controller = timeline(monkeypatch)
    controller.history_filter_edit.text = "nobody"
    controller._append_history_operations([record(1)])
    assert controller.history_tree.items[0].hidden


def test_own_last_shared_undo_survives_peer_timeline_eviction(monkeypatch):
    controller = timeline(monkeypatch, limit=8)
    controller._append_history_operations([record(1, "me")])
    for sequence in range(2, 100):
        controller._append_history_operations([record(sequence)])
    assert all(item["sequence"] != 1 for item in controller.history_records)
    controller.undo_last_shared_edit()
    controller._queue_action.assert_called_once_with("undo_operation", sequence=1)
    assert not controller._show_error.called


def test_advanced_reload_preserves_own_undo_and_bounds_merged_history(monkeypatch):
    controller = timeline(monkeypatch, limit=8)
    controller._append_history_operations([record(1, "me")])
    for sequence in range(2, 25):
        controller._append_history_operations([record(sequence)])
    for name in ("conflict_tree", "review_queue_tree", "resolve_latest_button", "resolve_mine_button",
                 "resolve_other_button", "resolve_union_button"):
        setattr(controller, name, MagicMock())
    controller._update_lock_controls = MagicMock()
    controller._update_advanced_state({"history": [record(i) for i in range(2, 40)]})
    assert [item["sequence"] for item in controller.history_records] == list(range(32, 40))
    assert all("payload" not in item for item in controller.history_records)
    controller.undo_last_shared_edit()
    controller._queue_action.assert_called_once_with("undo_operation", sequence=1)


def test_activity_dock_retains_only_a_bounded_recent_text_window(monkeypatch):
    controller = timeline(monkeypatch)
    editor = MagicMock()
    lines = []
    block_limit = [0]

    def append(text):
        lines.append(text)
        if block_limit[0]:
            del lines[:-block_limit[0]]

    editor.setMaximumBlockCount.side_effect = lambda count: block_limit.__setitem__(0, count)
    editor.appendPlainText.side_effect = append
    monkeypatch.setitem(sys.modules, "qt", SimpleNamespace(
        QDockWidget=lambda *args: MagicMock(), QPlainTextEdit=lambda: editor,
        Qt=SimpleNamespace(RightDockWidgetArea=1),
    ))
    monkeypatch.setitem(sys.modules, "slicer", SimpleNamespace(util=SimpleNamespace(mainWindow=MagicMock())))
    controller._scroll_chat_to_end = lambda unused: None
    controller._setup_activity_dock()
    for index in range(100):
        controller._append_activity("Event " + str(index))
    assert len(lines) == 32
    assert lines[-1].endswith("Event 99")
