import json
import sys
from types import SimpleNamespace

from LiveSegmentationLib import view_preferences
from LiveSegmentationLib.view_preferences import PersonalVisibility, personal_view_settings


def test_safe_starter_preferences_do_not_use_discarded_default_profile(tmp_path, monkeypatch):
    calls = []
    def settings(*args):
        calls.append(args)
        return "persistent"
    settings.IniFormat = "ini"
    monkeypatch.setitem(sys.modules, "qt", SimpleNamespace(QSettings=settings,
        QStandardPaths=SimpleNamespace(GenericDataLocation=1, writableLocation=lambda _: str(tmp_path))))
    assert personal_view_settings() == "persistent"
    assert calls == [(str(tmp_path/"SlicerLiveSegmentation"/"personal-label-view.ini"), "ini")]


class Display:
    def __init__(self, states):
        self.states = states
        self.global_3d = False  # Fast preview temporarily hides the native surface.
    def AddObserver(self, *args): return 1
    def RemoveObserver(self, *args): pass
    def GetSegmentVisibility(self, sid): return self.states[sid][0]
    def GetSegmentVisibility2DFill(self, sid): return self.states[sid][1]
    def GetSegmentVisibility2DOutline(self, sid): return self.states[sid][2]
    def GetSegmentVisibility3D(self, sid): return self.states[sid][3]
    def SetSegmentVisibility(self, sid, value): self.states[sid][0] = bool(value)
    def SetSegmentVisibility2DFill(self, sid, value): self.states[sid][1] = bool(value)
    def SetSegmentVisibility2DOutline(self, sid, value): self.states[sid][2] = bool(value)
    def SetSegmentVisibility3D(self, sid, value): self.states[sid][3] = bool(value)


class Settings:
    def __init__(self): self.values = {}
    def value(self, key, default): return self.values.get(key, default)
    def setValue(self, key, value): self.values[key] = value
    def sync(self): pass


def node(states):
    display = Display(states)
    segmentation = SimpleNamespace(GetSegmentIDs=lambda: list(states),
                                   SegmentAdded=100, AddObserver=lambda *a: 1, RemoveObserver=lambda *a: None)
    return SimpleNamespace(GetSegmentation=lambda: segmentation, GetDisplayNode=lambda: display)


def test_restart_restores_two_hundred_stable_ids_without_affecting_other_users():
    settings, pending = Settings(), []
    states = {str(i): [True]*4 for i in range(200)}
    first = PersonalVisibility(node(states), settings, "room:alice", pending.append)
    for i in range(199):
        states[str(i)] = [False, True, False, False]
    first.changed()
    first.changed()
    assert len(pending) == 1, "Visibility changes must be coalesced, not 200 synchronous settings writes"
    first.close()
    second_states = {str(i): [True]*4 for i in reversed(range(200))}
    second = PersonalVisibility(node(second_states), settings, "room:alice", pending.append)
    assert second_states == states
    assert second.node.GetDisplayNode().global_3d is False
    other = {str(i): [True]*4 for i in range(200)}
    PersonalVisibility(node(other), settings, "room:bob", pending.append)
    assert all(all(state) for state in other.values())
    assert "alice" not in next(iter(settings.values)), "Scope stored as a digest, not personal data"


def test_late_join_labels_restore_before_the_next_save_and_new_labels_stay_visible():
    settings, pending = Settings(), []
    old = node({"old": [False]*4})
    first = PersonalVisibility(old, settings, "room:alice", pending.append)
    first.close()
    states = {}
    rejoined = PersonalVisibility(node(states), settings, "room:alice", pending.append)
    states.update(old=[True]*4, new=[True]*4)
    rejoined.changed()
    pending[-1]()
    assert states["old"] == [False]*4
    assert states["new"] == [True]*4
    rejoined.close()
    assert json.loads(next(iter(settings.values.values())))["old"] == [False]*4


def test_disabling_visibility_detaches_pending_saves_and_blocks_later_binds(monkeypatch):
    settings, pending = Settings(), []
    modules = SimpleNamespace()
    scene = SimpleNamespace(NodeAboutToBeRemovedEvent=1, StartCloseEvent=2, AddObserver=lambda *a: 1)
    slicer = SimpleNamespace(modules=modules, mrmlScene=scene,
                             app=SimpleNamespace(aboutToQuit=SimpleNamespace(connect=lambda callback: None)))
    monkeypatch.setitem(sys.modules, "slicer", slicer)
    monkeypatch.setitem(sys.modules, "vtk", SimpleNamespace(calldata_type=lambda _: lambda f: f, VTK_OBJECT=1))
    monkeypatch.setitem(sys.modules, "qt", SimpleNamespace(QTimer=SimpleNamespace(singleShot=lambda ms, f: pending.append(f))))
    monkeypatch.setattr(view_preferences, "personal_view_settings", lambda: settings)

    def scene_node(states, identifier):
        result = node(states)
        attrs = {}
        result.GetScene = lambda: scene
        result.GetID = lambda: identifier
        result.SetAttribute = attrs.__setitem__
        result.GetAttribute = attrs.get
        return result

    assert view_preferences.personal_visibility_enabled()
    hidden = {"old": [False]*4}
    first = scene_node(hidden, "first")
    manager = view_preferences.bind_personal_visibility(first, scope="room:alice")
    manager.flush()
    stored = dict(settings.values)
    key, = stored
    hidden["old"] = [True]*4
    manager.changed()

    notifications = []
    unobserve = view_preferences.observe_personal_visibility_enabled(
        lambda enabled: notifications.append((enabled, dict(modules._personal_segment_views))))
    view_preferences.set_personal_visibility_enabled(False)
    assert notifications == [(False, {})]
    assert manager.node is None
    pending.pop()()
    assert hidden["old"] == [True]*4
    assert settings.values[key] == stored[key]

    new_states = {"old": [True]*4}
    rejoined = scene_node(new_states, "second")
    assert view_preferences.bind_personal_visibility(rejoined, scope="room:alice") is None
    assert view_preferences.bind_personal_visibility(rejoined) is None
    assert new_states["old"] == [True]*4

    unobserve()
    slicer.modules = SimpleNamespace()  # Next application instance reads the dedicated setting.
    assert not view_preferences.personal_visibility_enabled()
    view_preferences.set_personal_visibility_enabled(True)
    assert notifications == [(False, {})]
    resumed = view_preferences.bind_personal_visibility(rejoined)
    assert resumed.scope == "room:alice"
    assert new_states["old"] == [False]*4
