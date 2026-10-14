"""Optional module navigation must never mutate collaboration state."""
import ast
from pathlib import Path
from types import SimpleNamespace


def methods(slicer):
    path = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentation.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    widget = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LiveSegmentationWidget")
    names = {"_refresh_optional_navigation", "_open_nninteractive_ir"}
    selected = [node for node in widget.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(selected) == 2, "Optional nnInteractive IR navigation is missing"
    namespace = {"slicer": slicer}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class Action:
    def setEnabled(self, value):
        self.enabled = value

    def setVisible(self, value):
        self.visible = value


def test_ir_action_available_only_with_separate_module():
    api = SimpleNamespace(modules=SimpleNamespace(slicernninteractiveir=object()))
    action = Action()
    methods(api)["_refresh_optional_navigation"](SimpleNamespace(ir_navigation_action=action))
    assert action.enabled and action.visible


def test_ir_action_hidden_when_module_absent():
    action = Action()
    methods(SimpleNamespace(modules=SimpleNamespace()))["_refresh_optional_navigation"](
        SimpleNamespace(ir_navigation_action=action)
    )
    assert not action.enabled and not action.visible


def test_ir_action_only_selects_module_not_room_or_label():
    selected = []
    api = SimpleNamespace(modules=SimpleNamespace(slicernninteractiveir=object()),
                          util=SimpleNamespace(selectModule=selected.append))
    state = SimpleNamespace(connected=True, session="original", selected_label="label-a")
    widget = SimpleNamespace(live_collaboration=state)
    methods(api)["_open_nninteractive_ir"](widget)
    assert selected == ["SlicerNNInteractiveIR"]
    assert vars(state) == {"connected": True, "session": "original", "selected_label": "label-a"}


def test_missing_optional_module_does_not_open_or_install_anything():
    selected, refreshed = [], []
    api = SimpleNamespace(modules=SimpleNamespace(), util=SimpleNamespace(selectModule=selected.append))
    widget = SimpleNamespace(_refresh_optional_navigation=lambda: refreshed.append(True))
    methods(api)["_open_nninteractive_ir"](widget)
    assert not selected and refreshed == [True]
