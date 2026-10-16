"""Do not eagerly preprocess large sources in a hidden Segment Editor."""
import ast
from pathlib import Path
from types import SimpleNamespace


def functions(slicer=None):
    path = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentation.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LiveSegmentationWidget")
    names = {"_configure_standard_segment_editor", "prepare_shared_segmentation", "_on_module_selected"}
    nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = {"slicer": slicer}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def test_unchanged_editor_targets_are_not_rebound():
    calls = []
    editor = SimpleNamespace(segmentationNode=lambda: "seg", sourceVolumeNode=lambda: "source",
                             setSegmentationNode=lambda x: calls.append(("seg", x)),
                             setSourceVolumeNode=lambda x: calls.append(("source", x)))
    functions()["_configure_standard_segment_editor"](SimpleNamespace(_live_session_active=False), editor, "seg", "source")
    assert calls == []


def test_real_target_change_still_binds_source_and_segmentation():
    calls = []
    editor = SimpleNamespace(segmentationNode=lambda: "old", sourceVolumeNode=lambda: "old-source",
                             setSegmentationNode=lambda x: calls.append(("seg", x)),
                             setSourceVolumeNode=lambda x: calls.append(("source", x)))
    functions()["_configure_standard_segment_editor"](SimpleNamespace(_live_session_active=False), editor, "seg", "source")
    assert calls == [("seg", "seg"), ("source", "source")]


def test_join_does_not_initialize_a_hidden_editor():
    calls = []
    node = SimpleNamespace(SetAttribute=lambda *a: None)
    owner = SimpleNamespace(get_segmentation_node=lambda: None,
                            clear_live_segmentation=lambda: None,
                            _create_segmentation_node=lambda *a: node,
                            segmentation_selector=SimpleNamespace(setCurrentNode=lambda *a: None),
                            _set_reference_geometry_if_empty=lambda *a: None,
                            _standard_segment_editor_widget=lambda: calls.append("created"),
                            _configure_standard_segment_editor=lambda *a: calls.append("configured"))
    api = SimpleNamespace(util=SimpleNamespace(selectedModule=lambda: "LiveSegmentation"))
    assert functions(api)["prepare_shared_segmentation"](owner, {"id": "room", "created": False}) is node
    assert calls == []


def test_manual_navigation_attaches_only_during_a_live_room():
    calls = []
    owner = SimpleNamespace(_live_session_active=True,
                            get_segmentation_node=lambda: "live-seg", get_volume_node=lambda: "volume",
                            _standard_segment_editor_widget=lambda: "editor",
                            _configure_standard_segment_editor=lambda *a: calls.append(a))
    handler = functions()["_on_module_selected"]
    handler(owner, "SlicerNNInteractiveIR")
    assert not calls
    handler(owner, "SegmentEditor")
    assert calls == [("editor", "live-seg", "volume")]
    owner._live_session_active = False
    handler(owner, "SegmentEditor")
    assert len(calls) == 1
