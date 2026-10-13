"""Remember destinations, not authentication or an active network connection."""
from types import SimpleNamespace

from tests.controller_harness import collaboration as c


def test_location_history_uses_explicit_local_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(__import__("sys").modules, "qt", SimpleNamespace(
        QSettings=lambda *args: calls.append(args),
    ))
    # Add the format constant to the fake constructor.
    __import__("sys").modules["qt"].QSettings.IniFormat = 1
    c.location_history_settings(tmp_path)
    assert calls == [(str(tmp_path / "LiveSegmentation" / "locations.ini"), 1)]


def test_history_never_persists_url_credentials_or_tokens():
    assert c.safe_location_history([
        "ssh://alice:secret@specimen.example.org/mnt/data?password=bad#token",
        "https://user:secret@example.org/?token=bad",
        r"\\nas\shared",
    ]) == ["ssh://specimen.example.org/mnt/data", "https://example.org/", r"\\nas\shared"]


def test_default_history_does_not_depend_on_slicer_application_name(monkeypatch, tmp_path):
    import sys
    calls = []
    def settings(*args):
        calls.append(args)
    settings.IniFormat = 1
    paths = SimpleNamespace(GenericDataLocation=1, AppLocalDataLocation=2,
                            writableLocation=lambda kind: str(tmp_path if kind == 1 else tmp_path / "Slicer-tmp"))
    monkeypatch.setitem(sys.modules, "qt", SimpleNamespace(QSettings=settings, QStandardPaths=paths))
    c.location_history_settings()
    assert calls[0][0] == str(tmp_path / "SlicerLiveSegmentation" / "LiveSegmentation" / "locations.ini")
