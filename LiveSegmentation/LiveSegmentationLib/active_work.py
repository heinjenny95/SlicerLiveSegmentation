"""Local work-time state machine. No image access or external tool dependency."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict

SAMPLE_SECONDS = 5.0
MAX_OBSERVATION_GAP_SECONDS = 10.0


class ActiveWorkClock:
    """Monotonic durations with a fixed UTC anchor; idle time is an estimate.

    Manual review explicitly includes silent visual inspection. Only purposeful
    local input renews the automatic activity window, never a remote edit.
    """

    def __init__(self, session_id, sink, *, monotonic=None, epoch=None, idle_seconds=60,
                 max_gap_seconds=MAX_OBSERVATION_GAP_SECONDS):
        self.session_id = str(session_id)
        self.sink = sink
        self.anchor = time.monotonic() if monotonic is None else float(monotonic)
        self.epoch = time.time() if epoch is None else float(epoch)
        self.now = self.last_input = self.anchor
        self.idle_seconds = float(idle_seconds)
        if not 15 <= self.idle_seconds <= 900:
            raise ValueError("Auto-pause must be between 15 and 900 seconds")
        self.max_gap_seconds = float(max_gap_seconds)
        self.foreground = self.relevant = True
        self.chat = self.busy = self.syncing = False
        self.busy_since = None
        self.paused = self.review = self.manual_wait = False
        self.stopped = False
        self.totals = defaultdict(float)
        self._buffer = None
        self._run_id = uuid.uuid4().hex
        self._sequence = 0

    def category_at(self, when):
        if self.paused:
            return "manual_pause"
        if self.manual_wait:
            return "technical_wait"
        if not self.foreground:
            return "away"
        if not self.relevant:
            return "other_context"
        if self.chat:
            return "communication"
        if self.syncing:
            return "technical_wait"
        if self.review:
            return "review"
        if self.busy and (self.busy_since is None or self.last_input <= self.busy_since
                          or when >= self.last_input + 2.0):
            return "technical_wait"
        if when >= self.last_input + self.idle_seconds:
            return "idle"
        return "work"

    @property
    def category(self):
        return self.category_at(self.now)

    @property
    def active_seconds(self):
        return self.totals["work"] + self.totals["review"]

    def _flush(self):
        if self._buffer is None:
            return
        category, low, high, busy, threshold = self._buffer
        self._buffer = None
        if high <= low:
            return
        self._sequence += 1
        self.sink({"sample_id": f"{self._run_id}-{self._sequence:012d}",
                   "session_id": self.session_id, "measurement_version": 1,
                   "category": category, "start_epoch": self.epoch + low - self.anchor,
                   "end_epoch": self.epoch + high - self.anchor,
                   "duration_seconds": high - low, "idle_threshold_seconds": threshold,
                   "nninteractive_busy": bool(busy)})

    def _accumulate(self, category, low, high):
        while high > low + 1e-9:
            if self._buffer and (self._buffer[0] != category or self._buffer[3] != self.busy
                                 or self._buffer[4] != self.idle_seconds):
                self._flush()
            if self._buffer is None:
                self._buffer = [category, low, low, self.busy, self.idle_seconds]
            boundary = high if category == "unknown" else min(high, self._buffer[1] + SAMPLE_SECONDS)
            self.totals[category] += boundary - low
            self._buffer[2] = boundary
            low = boundary
            if category == "unknown" or self._buffer[2] - self._buffer[1] >= SAMPLE_SECONDS - 1e-9:
                self._flush()

    def tick(self, now=None, **context):
        if self.stopped:
            return
        now = time.monotonic() if now is None else float(now)
        if now < self.now:
            raise ValueError("Work timing requires a monotonic clock")
        if now - self.now > self.max_gap_seconds:
            self._flush()
            self._accumulate("unknown", self.now, now)
            self.last_input = now - self.idle_seconds
        else:
            cursor = self.now
            while cursor < now - 1e-9:
                end = now
                for boundary in (self.last_input + self.idle_seconds, self.last_input + 2.0):
                    if cursor + 1e-9 < boundary < end:
                        end = boundary
                self._accumulate(self.category_at(cursor + 1e-9), cursor, end)
                cursor = end
        self.now = now
        was_busy, was_syncing = self.busy, self.syncing
        for key in ("foreground", "relevant", "chat", "busy", "syncing"):
            if key in context:
                setattr(self, key, bool(context[key]))
        if self.busy and not was_busy:
            self.busy_since = now
        elif was_busy and not self.busy:
            self.busy_since = None
        if (was_busy and not self.busy) or (was_syncing and not self.syncing):
            # A new result needs inspection; allow the configured activity grace.
            self.last_input = now

    def note_input(self, now=None):
        self.tick(now)
        self.last_input = self.now

    def set_paused(self, value, now=None):
        self.tick(now)
        self._flush()
        self.paused = bool(value)
        self.review = self.manual_wait = False
        if not self.paused:
            self.last_input = self.now

    def set_review(self, value, now=None):
        self.tick(now)
        self._flush()
        self.review = bool(value)
        if self.review:
            self.paused = self.manual_wait = False
        self.last_input = self.now

    def set_manual_wait(self, value, now=None):
        self.tick(now)
        self._flush()
        self.manual_wait = bool(value)
        if self.manual_wait:
            self.paused = self.review = False
        self.last_input = self.now

    def set_idle_seconds(self, value, now=None):
        value = float(value)
        if not 15 <= value <= 900:
            raise ValueError("Auto-pause must be between 15 and 900 seconds")
        self.tick(now)
        self._flush()
        self.idle_seconds = value

    def finish(self, now=None):
        if self.stopped:
            return
        self.tick(now)
        self._flush()
        self.sink({"sample_id": self._run_id + "-end", "session_id": self.session_id,
                   "measurement_version": 1, "category": "session_end",
                   "start_epoch": self.epoch + self.now - self.anchor,
                   "end_epoch": self.epoch + self.now - self.anchor, "duration_seconds": 0,
                   "idle_threshold_seconds": self.idle_seconds,
                   "nninteractive_busy": False, "expected_samples": self._sequence})
        self.stopped = True


def nninteractive_busy(modules):
    """Inspect ONLY an already-created optional widget; never load/initialize it.

    The IR background heartbeat is not an inference wait and must not be charged.
    """
    namespace = vars(modules)
    widget = namespace.get("SlicerNNInteractiveIRWidget") or namespace.get("slicernninteractiveirwidget")
    return bool(widget is not None and not getattr(widget, "_cleaned_up", False)
                and getattr(widget, "_prediction_in_flight", False))


def module_matches_project(modules, module_name, segmentation_id):
    """Read selectors/cached IDs only; the IR target getter can CREATE nodes."""
    namespace = vars(modules)
    if not segmentation_id:
        return False
    try:
        if module_name == "SegmentEditor":
            widget = namespace.get("SegmentEditorWidget")
            editor = getattr(widget, "editor", None)
            node = editor.segmentationNode() if editor is not None else None
            return bool(node and node.GetID() == segmentation_id)
        if module_name == "SlicerNNInteractiveIR":
            widget = namespace.get("SlicerNNInteractiveIRWidget")
            if getattr(widget, "_cleaned_up", False):
                return False
            selector = getattr(getattr(widget, "ui", None), "targetSegmentationSelector", None)
            node = selector.currentNode() if selector is not None else None
            node_id = node.GetID() if node else getattr(widget, "_explicit_target_segmentation_node_id", None) or getattr(widget, "_last_selected_segmentation_node_id", None)
            return node_id == segmentation_id
    except (AttributeError, RuntimeError):
        return False  # Optional module reload/deletion must not break the timer.
    return module_name == "LiveSegmentation"


class WorkTimeUI:
    """Compact global controls + input observer. All GUI access stays here."""

    def __init__(self, controller, toolbar):
        import qt
        import slicer

        self.controller = controller
        self.clock = self.local_log = None
        self.pending = []
        self.error = ""
        self._roots = []
        self._roots_at = 0.0
        self._observing = False
        self.timer = qt.QTimer(toolbar)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)
        self.button = qt.QToolButton(toolbar)
        self.button.setObjectName("LiveSegmentationWorkTime")
        self.button.setPopupMode(qt.QToolButton.InstantPopup)
        self.menu = qt.QMenu(self.button)
        self.wait_action = self.menu.addAction("Technical wait (manual)")
        self.wait_action.setCheckable(True)
        self.wait_action.triggered.connect(lambda checked=False: self._manual_wait(checked))
        self.backup_action = self.menu.addAction("Open local timing backup")
        self.backup_action.triggered.connect(self.open_backup)
        spin_widget = qt.QWidget(self.menu)
        spin_layout = qt.QHBoxLayout(spin_widget)
        spin_layout.addWidget(qt.QLabel("Auto-pause after (s)"))
        self.idle_spin = qt.QSpinBox()
        self.idle_spin.setRange(15, 900)
        self.idle_spin.setValue(60)
        self.idle_spin.valueChanged.connect(self._change_idle)
        spin_layout.addWidget(self.idle_spin)
        widget_action = qt.QWidgetAction(self.menu)
        widget_action.setDefaultWidget(spin_widget)
        self.menu.addAction(widget_action)
        self.button.setMenu(self.menu)
        toolbar.addSeparator()
        toolbar.addWidget(self.button)
        self.pause_action = toolbar.addAction("Pause")
        self.pause_action.setObjectName("LiveSegmentationWorkPause")
        self.pause_action.triggered.connect(self.toggle_pause)
        self.review_action = toolbar.addAction("Review")
        self.review_action.setObjectName("LiveSegmentationWorkReview")
        self.review_action.setCheckable(True)
        self.review_action.setToolTip("Visual review counts as work even without input. Turn off or Pause when taking a break.")
        self.review_action.triggered.connect(lambda checked=False: self.set_review(checked))
        adapter = self
        input_types = frozenset((qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease,
                                 qt.QEvent.Wheel, qt.QEvent.KeyPress, qt.QEvent.MouseMove))

        class InputFilter(qt.QObject):
            def eventFilter(self, watched, event):
                # Model/layout/paint events are not user work. During a large
                # initial replay Qt emits millions of them; reject those before
                # entering the accounting adapter/import/ancestry path.
                if event.type() in input_types:
                    adapter.observe_input(watched, event)
                return False  # Never consume a painting/key event.

        self.filter = InputFilter(slicer.util.mainWindow())
        self.refresh()

    def start(self):
        import hashlib
        from pathlib import Path

        import qt
        import slicer

        try:
            from .work_log import RoomWorkLog
        except ImportError:
            from work_log import RoomWorkLog
        client = self.controller.client
        session_id = getattr(client, "presence_session_id", None) or uuid.uuid4().hex
        location = str(getattr(client, "shared_folder", None) or getattr(client, "server_url", None)
                       or getattr(client, "location", ""))
        key = hashlib.sha256(f"{location}\0{self.controller.room_id}\0{self.controller.user_name}".encode()).hexdigest()[:20]
        root = Path(qt.QStandardPaths.writableLocation(qt.QStandardPaths.GenericDataLocation))
        root = root / "LiveSegmentation" / "work-time-recovery" / key / uuid.uuid4().hex
        self.local_log = RoomWorkLog(root, create_room=True)
        self.pending = []
        self.error = ""
        self.clock = ActiveWorkClock(session_id, self._sample, idle_seconds=self.idle_spin.value)
        self._observing = True
        slicer.app.installEventFilter(self.filter)
        self.timer.start()
        self.tick()

    def _sample(self, item):
        # Called only by the GUI-owned clock. submit() performs no file I/O.
        self.pending.append(item)
        self.local_log.submit("work.interval", self.controller.user_name,
                              item["session_id"], item, event_id="local-" + item["sample_id"])

    def _under(self, widget, roots):
        while widget is not None:
            if any(widget is root or widget == root for root in roots if root is not None):
                return True
            try:
                widget = widget.parent()
            except (AttributeError, RuntimeError):
                return False
        return False

    def _chat_roots(self):
        return [getattr(self.controller, name, None) for name in
                ("chat_dock", "chat_history", "chat_input", "chat_dock_input", "chat_edit")]

    def _refresh_roots(self):
        import slicer

        manager = slicer.app.layoutManager()
        roots = [self.controller.owner.content_widget]
        if manager:
            roots.extend(manager.sliceWidget(name) for name in manager.sliceViewNames())
            roots.extend(manager.threeDWidget(index) for index in range(manager.threeDViewCount))
        for name in ("SegmentEditorWidget", "SlicerNNInteractiveIRWidget"):
            widget = vars(slicer.modules).get(name)
            if widget is not None:
                roots.append(widget.parent)
        self._roots = roots

    def observe_input(self, watched, event):
        import qt

        if not self._observing or self.clock is None or self.clock.stopped:
            return
        kind = event.type()
        meaningful = kind in (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease,
                              qt.QEvent.Wheel, qt.QEvent.KeyPress)
        if kind == qt.QEvent.MouseMove:
            meaningful = bool(event.buttons())  # Dragging, not bare pointer motion.
        if not meaningful or self._under(watched, self._chat_roots()):
            return
        if kind == qt.QEvent.KeyPress and event.key() in (
                qt.Qt.Key_Control, qt.Qt.Key_Shift, qt.Qt.Key_Alt, qt.Qt.Key_Meta):
            return
        if self._under(watched, self._roots):
            self.clock.note_input()  # No keys, coordinates, text or images are retained.

    def tick(self):

        if self.clock is None or self.clock.stopped:
            return
        now = time.monotonic()
        if now - self._roots_at >= 1.0:
            self._roots_at = now
            self._refresh_roots()
        self.clock.tick(now, **self._context())
        self.refresh()

    def _context(self):
        import slicer

        selected_module = slicer.util.selectedModule()
        relevant = module_matches_project(slicer.modules, selected_module,
                                           self.controller.segmentation_node_id)
        if selected_module not in {"LiveSegmentation", "SegmentEditor", "SlicerNNInteractiveIR"}:
            relevant = self.clock.review  # Explicit review also works in volume/data views.
        return {"foreground": slicer.app.activeWindow() is not None,
                "relevant": relevant,
                "chat": self._under(slicer.app.focusWidget(), self._chat_roots()),
                "busy": nninteractive_busy(slicer.modules),
                "syncing": not self.controller.initial_sync_complete}

    def toggle_pause(self, checked=False):
        del checked
        if self.clock and not self.clock.stopped:
            resume = self.clock.paused or self.clock.manual_wait or self.clock.category in {
                "idle", "away", "other_context", "communication"}
            self.clock.set_paused(not resume)
            self.refresh()

    def set_review(self, checked):
        if self.clock and not self.clock.stopped:
            self.clock.set_review(checked)
            self.refresh()

    def _manual_wait(self, checked):
        if self.clock and not self.clock.stopped:
            self.clock.set_manual_wait(checked)
            self.refresh()

    def _change_idle(self, value):
        if self.clock and not self.clock.stopped:
            self.clock.set_idle_seconds(value)
            self.refresh()

    def acknowledge(self, ids):
        accepted = set(ids)
        self.pending = [item for item in self.pending if item["sample_id"] not in accepted]

    def finish(self):
        import slicer

        if self.clock and not self.clock.stopped:
            self.clock.finish()
        self.timer.stop()
        if self._observing:
            slicer.app.removeEventFilter(self.filter)
            self._observing = False
        self.refresh()
        return (self.local_log, list(self.pending)) if self.local_log else None

    def cleanup(self):
        self.finish()
        self.filter.deleteLater()

    def open_backup(self, checked=False):
        del checked
        import qt

        if self.local_log:
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(str(self.local_log.room_path)))

    def refresh(self):
        running = self.clock is not None and not self.clock.stopped
        seconds = int(self.clock.active_seconds) if self.clock else 0
        state = self.clock.category if running else "offline"
        labels = {"work": "Work", "review": "Review", "manual_pause": "Paused",
                  "idle": "Auto-paused", "technical_wait": "Wait", "away": "Away",
                  "other_context": "Other", "communication": "Chat", "offline": "Work"}
        self.button.setText(f"{labels.get(state, state)} {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
        self.pause_action.setEnabled(running)
        self.review_action.setEnabled(running)
        self.wait_action.setEnabled(running)
        self.backup_action.setEnabled(self.local_log is not None and bool(self.local_log.saved_at))
        can_resume = running and (self.clock.paused or self.clock.manual_wait or state in {
            "idle", "away", "other_context", "communication"})
        self.pause_action.setText("Resume" if can_resume else "Pause")
        for action, checked in ((self.review_action, running and self.clock.review),
                                (self.wait_action, running and self.clock.manual_wait)):
            action.blockSignals(True)
            action.setChecked(bool(checked))
            action.blockSignals(False)
        color = "#188038" if state in {"work", "review"} else "#b26a00" if running else "#666666"
        self.button.setStyleSheet(f"QToolButton {{ color: {color}; }}")
        tooltip = ("Local work + visual review. Auto-pause is an estimate; Review explicitly counts silent inspection.\n"
                   "Pause for breaks. Menu: idle threshold, manual technical wait, local timing backup.")
        if self.local_log:
            tooltip += f"\nLocal backup: {self.local_log.room_path}\nPending transfer: {len(self.pending)} interval(s)."
        if self.error:
            tooltip += "\n" + self.error
        self.button.setToolTip(tooltip)
