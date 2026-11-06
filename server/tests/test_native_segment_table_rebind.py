"""Repair native row controls only after the current name editor closes."""

import sys
from types import SimpleNamespace

import pytest

from tests.controller_harness import collaboration


@pytest.mark.parametrize("same_node", [False, True])
def test_order_rebind_waits_for_name_editor_and_preserves_binding(monkeypatch, same_node):
    callbacks, timers, rebound = [], [], []
    properties = {}
    viewport = object()
    hidden = [False]
    name_editor = SimpleNamespace(parentWidget=lambda: viewport, isHidden=lambda: hidden[0],
                                  destroyed=SimpleNamespace(connect=callbacks.append))
    scroll = SimpleNamespace(value=42)
    scroll.setValue = lambda value: setattr(scroll, "value", value)
    table = SimpleNamespace(viewport=lambda: viewport, findChildren=lambda kind: [name_editor],
                            verticalScrollBar=lambda: scroll, property=properties.get,
                            setProperty=lambda name, value: properties.__setitem__(name, value))
    node = object()
    bound = [node]
    selection = ["B"]

    def bind(new_node):
        bound[0] = new_node
        rebound.append(new_node)
        scroll.value = 0
        selection.clear()

    view = SimpleNamespace(segmentationNode=lambda: bound[0], findChild=lambda *args: table,
                           selectedSegmentIDs=lambda: list(selection), setSegmentationNode=bind,
                           setSelectedSegmentIDs=lambda ids: selection.extend(ids), blockSignals=lambda value: False)
    window = SimpleNamespace(findChildren=lambda kind: [view])
    monkeypatch.setitem(sys.modules, "slicer", SimpleNamespace(util=SimpleNamespace(mainWindow=lambda: window)))
    monkeypatch.setitem(sys.modules, "qt", SimpleNamespace(
        QTimer=SimpleNamespace(singleShot=lambda delay, callback: timers.append(callback))))

    refresh = collaboration.LiveCollaborationController._refresh_segment_order_views
    refresh(node)
    refresh(node)
    assert rebound == []  # A live draft must survive duplicate repair requests.
    assert len(callbacks) == 1

    hidden[0] = True
    if not same_node:
        bound[0] = object()
    callbacks[0]()  # Native Enter or Escape destroyed the old editor.
    assert not rebound
    assert len(timers) == 1
    timers.pop()()
    assert rebound == ([None, node] if same_node else [])
    assert scroll.value == 42 and selection == ["B"]
