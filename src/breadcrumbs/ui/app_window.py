import sys
import os
import json
from enum import Enum, auto

from PySide6.QtCore import QThreadPool, QUrl, Qt, QTimer, QSettings
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QFileDialog,
    QMessageBox,
    QSlider,
    QToolButton,
    QStyle,
    QCheckBox,
    QGroupBox,
    QFormLayout,
)
from PySide6.QtGui import QDesktopServices, QShortcut, QKeySequence
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage

from breadcrumbs.core.sessions import scan_logs_dir, SessionMeta
from breadcrumbs.core.load import load_track, TrackPoint
from breadcrumbs.core.export_kml import export_track_to_kml
from breadcrumbs.core.export_gpx import export_track_to_gpx
from breadcrumbs.core.drive import find_mounted_edgetx_once
from breadcrumbs.core.stats import compute_flight_stats, FlightStats
from breadcrumbs.ui.cache import start_cache_server, shutdown_cache_server
from breadcrumbs.ui.worker import Worker, format_time
from breadcrumbs.ui.plotting import (
    build_timeline_seconds,
    build_hotline_payload,
    build_dropout_segments,
)


class UiState(Enum):
    IDLE = auto()      # no folder scanned (or scan failed)
    INDEXING = auto()  # worker scanning a folder
    INDEXED = auto()   # sessions available, no track loaded
    LOADING = auto()   # worker loading a track
    LOADED = auto()    # track in memory, playback ready


# Single source of truth for widget-enabled state per UiState. Every transition
# must go through MainWindow._set_ui_state — do not call setEnabled directly.
_PLAYBACK_WIDGETS = (
    "btn_to_start", "btn_play_pause", "btn_stop", "speed_combo", "play_slider",
    "follow_checkbox", "smooth_checkbox", "show_full_checkbox",
)
_INDEX_WIDGETS = ("day_combo", "session_list")
_TRACK_WIDGETS = ("metric_combo", "btn_export_kml", "btn_export_gpx")

_UI_STATE_TABLE: dict[UiState, dict[str, bool]] = {
    state: {
        **{w: False for w in _INDEX_WIDGETS + _TRACK_WIDGETS + _PLAYBACK_WIDGETS},
        "btn_open_folder": True,
        "btn_load_radio": True,
        "map_style_combo": True,
    }
    for state in UiState
}
for w in _INDEX_WIDGETS:
    _UI_STATE_TABLE[UiState.INDEXED][w] = True
    _UI_STATE_TABLE[UiState.LOADING][w] = True
    _UI_STATE_TABLE[UiState.LOADED][w] = True
for w in _TRACK_WIDGETS + _PLAYBACK_WIDGETS:
    _UI_STATE_TABLE[UiState.LOADED][w] = True


class CustomWebEnginePage(QWebEnginePage):
    def acceptNavigationRequest(self, url, _type, isMainFrame):
        # If the user clicked a link, open it in the system default browser
        if _type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False  # Prevent the QWebEngineView from following the link
        
        # Otherwise, let the web engine load the page normally (e.g., your map HTML)
        return super().acceptNavigationRequest(url, _type, isMainFrame)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Drone Recovery")
        self.resize(1200, 800)

        self.thread_pool = QThreadPool.globalInstance()

        # Local tile proxy cache
        self.cache_server, self.cache_port = start_cache_server()

        # State
        self.sessions_by_day: dict[str, list[SessionMeta]] = {}
        self.current_track: list[TrackPoint] | None = None

        # Timeline (seconds from start) sent to JS for smooth playback
        self.timeline_s: list[float] = []
        self.total_duration_s: float = 0.0

        # Playback/UI state
        self.playing: bool = False
        self.user_scrubbing: bool = False
        self.playback_elapsed_s: float = 0.0
        self.playback_progress: float = 0.0
        self._auto_hid_full_path: bool = False

        # JS queue to avoid calling functions before page is ready
        self.map_ready = False
        self._pending_js: list[str] = []

        # Background radio detection (1Hz). _radio_check_inflight prevents
        # piling up workers if a probe takes longer than the poll interval.
        self._radio_check_inflight: bool = False
        self._radio_mount: str | None = None
        self._radio_default_text = "Load from radio"

        # --- UI (left panel) ---
        left = QWidget()
        left_layout = QVBoxLayout(left)

        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        left_layout.addWidget(self.status)

        load_row = QWidget()
        load_row_layout = QHBoxLayout(load_row)
        load_row_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_open_folder = QPushButton("Open folder…")
        self.btn_load_radio = QPushButton("Load from radio")
        load_row_layout.addWidget(self.btn_open_folder)
        load_row_layout.addWidget(self.btn_load_radio)
        left_layout.addWidget(load_row)

        self.day_combo = QComboBox()
        self.day_combo.setEnabled(False)
        left_layout.addWidget(self.day_combo)

        self.session_list = QListWidget()
        self.session_list.setEnabled(False)
        left_layout.addWidget(self.session_list)

        # ---- Flight stats ----
        self.stats_group = QGroupBox("Flight stats")
        stats_form = QFormLayout(self.stats_group)
        stats_form.setContentsMargins(8, 6, 8, 6)
        stats_form.setVerticalSpacing(2)
        self.stat_duration = QLabel("—")
        self.stat_distance = QLabel("—")
        self.stat_max_range = QLabel("—")
        self.stat_max_alt = QLabel("—")
        self.stat_max_speed = QLabel("—")
        self.stat_rssi = QLabel("—")
        stats_form.addRow("Duration:", self.stat_duration)
        stats_form.addRow("Distance:", self.stat_distance)
        stats_form.addRow("Max range:", self.stat_max_range)
        stats_form.addRow("Max alt:", self.stat_max_alt)
        stats_form.addRow("Max speed:", self.stat_max_speed)
        stats_form.addRow("RSSI:", self.stat_rssi)
        left_layout.addWidget(self.stats_group)

        left_layout.addWidget(QLabel("Color by:"))
        self.metric_combo = QComboBox()
        self.metric_combo.addItems(["Progress", "RSSI", "Speed", "Altitude"])
        self.metric_combo.setEnabled(False)
        left_layout.addWidget(self.metric_combo)

        left_layout.addWidget(QLabel("Map Style:"))
        self.map_style_combo = QComboBox()
        self.map_style_combo.addItems(["Normal", "Satellite"]) # Terrain option available but unused
        left_layout.addWidget(self.map_style_combo)

        # ---- Playback controls ----
        left_layout.addWidget(QLabel("Playback:"))

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_to_start = QToolButton()
        self.btn_to_start.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward))
        self.btn_to_start.setToolTip("Go to start (Home)")
        self.btn_to_start.setEnabled(False)
        row_layout.addWidget(self.btn_to_start)

        self.btn_play_pause = QToolButton()
        self.btn_play_pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_play_pause.setToolTip("Play / Pause (Space)")
        self.btn_play_pause.setEnabled(False)
        row_layout.addWidget(self.btn_play_pause)

        self.btn_stop = QToolButton()
        self.btn_stop.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.btn_stop.setToolTip("Stop / reset to start (Esc)")
        self.btn_stop.setEnabled(False)
        row_layout.addWidget(self.btn_stop)

        row_layout.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5x", "1x", "2x", "5x", "10x", "20x"])
        self.speed_combo.setCurrentText("5x")
        self.speed_combo.setEnabled(False)
        row_layout.addWidget(self.speed_combo)

        left_layout.addWidget(row)

        self.play_slider = QSlider(Qt.Orientation.Horizontal)
        self.play_slider.setEnabled(False)
        self.play_slider.setMinimum(0)
        self.play_slider.setMaximum(0)
        self.play_slider.setValue(0)
        left_layout.addWidget(self.play_slider)

        # Time label row
        row2 = QWidget()
        row2_layout = QHBoxLayout(row2)
        row2_layout.setContentsMargins(0, 0, 0, 0)

        self.time_label = QLabel("0:00 / 0:00")
        row2_layout.addWidget(self.time_label)
        row2_layout.addStretch(1)
        left_layout.addWidget(row2)

        # Toggle row: Follow, Smooth, Show full path
        row3 = QWidget()
        row3_layout = QHBoxLayout(row3)
        row3_layout.setContentsMargins(0, 0, 0, 0)

        self.follow_checkbox = QCheckBox("Follow")
        self.follow_checkbox.setEnabled(False)
        row3_layout.addWidget(self.follow_checkbox)

        self.smooth_checkbox = QCheckBox("Smooth")
        self.smooth_checkbox.setChecked(True)
        self.smooth_checkbox.setEnabled(False)
        row3_layout.addWidget(self.smooth_checkbox)

        self.show_full_checkbox = QCheckBox("Show full path")
        self.show_full_checkbox.setChecked(True)
        self.show_full_checkbox.setEnabled(False)
        row3_layout.addWidget(self.show_full_checkbox)

        row3_layout.addStretch(1)
        left_layout.addWidget(row3)

        # ---- Export ----
        export_row = QWidget()
        export_row_layout = QHBoxLayout(export_row)
        export_row_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_export_kml = QPushButton("Export KML")
        self.btn_export_kml.setToolTip("Save as KML for Google Earth")
        self.btn_export_kml.setEnabled(False)
        self.btn_export_gpx = QPushButton("Export GPX")
        self.btn_export_gpx.setToolTip("Save as GPX (Strava, mapping tools)")
        self.btn_export_gpx.setEnabled(False)
        export_row_layout.addWidget(self.btn_export_kml)
        export_row_layout.addWidget(self.btn_export_gpx)
        left_layout.addWidget(export_row)

        # --- UI (map panel) ---
        self.web = QWebEngineView()

        # Remove internal webcache
        self.web.page().profile().clearHttpCache()

        # Intercept link click and send to default OS browser
        self.web_page = CustomWebEnginePage(self.web)
        self.web.setPage(self.web_page)

        # Allow file:// HTML to load remote resources
        s = self.web.settings()
        try:
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        except Exception:
            pass

        self.map_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "map_view.html"))
        self.web.loadFinished.connect(self.on_map_loaded)
        self.web.load(QUrl.fromLocalFile(self.map_path))

        # --- Layout ---
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.addWidget(left, 3)
        layout.addWidget(self.web, 7)
        self.setCentralWidget(root)

        # Poll timer (UI sync while JS animates smoothly)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(100)  # 10 Hz UI update
        self.play_timer.timeout.connect(self.on_poll_tick)

        # Signals
        self.btn_open_folder.clicked.connect(self.open_folder_dialog)
        self.btn_load_radio.clicked.connect(self.load_from_radio)
        self.day_combo.currentIndexChanged.connect(self.on_day_changed)
        self.session_list.itemClicked.connect(self.on_session_clicked)
        self.metric_combo.currentIndexChanged.connect(self.on_metric_changed)
        self.btn_export_kml.clicked.connect(self.export_kml)
        self.btn_export_gpx.clicked.connect(self.export_gpx)
        self.map_style_combo.currentIndexChanged.connect(self.on_map_style_changed)


        # Playback signals
        self.btn_to_start.clicked.connect(self.go_to_start)
        self.btn_play_pause.clicked.connect(self.toggle_play_pause)
        self.btn_stop.clicked.connect(self.stop_playback)

        self.play_slider.sliderPressed.connect(self.on_slider_pressed)
        self.play_slider.sliderMoved.connect(self.on_slider_moved)
        self.play_slider.sliderReleased.connect(self.on_slider_released)
        self.play_slider.valueChanged.connect(self.on_slider_value_changed)

        self.speed_combo.currentIndexChanged.connect(self.on_speed_changed)
        self.follow_checkbox.toggled.connect(self.on_follow_toggled)
        self.smooth_checkbox.toggled.connect(self.on_smooth_toggled)
        self.show_full_checkbox.toggled.connect(self.on_show_full_toggled)

        # Keyboard shortcuts (window-scoped — guards live in target methods)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.toggle_play_pause)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.stop_playback)
        QShortcut(QKeySequence(Qt.Key.Key_Home), self, activated=self.go_to_start)
        QShortcut(QKeySequence(Qt.Key.Key_End), self,
                  activated=lambda: self.seek_to_index(self.play_slider.maximum()))
        QShortcut(QKeySequence(Qt.Key.Key_Left), self,
                  activated=lambda: self._seek_index_relative(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self,
                  activated=lambda: self._seek_index_relative(1))
        QShortcut(QKeySequence("Shift+Left"), self,
                  activated=lambda: self._seek_time_relative(-10.0))
        QShortcut(QKeySequence("Shift+Right"), self,
                  activated=lambda: self._seek_time_relative(10.0))

        # Drag-and-drop a folder or .csv onto the window
        self.setAcceptDrops(True)

        # Restore persisted state
        self.settings = QSettings("breadcrumbs", "breadcrumbs")
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        for combo, key in (
            (self.metric_combo, "metric"),
            (self.speed_combo, "speed"),
            (self.map_style_combo, "map_style"),
        ):
            val = self.settings.value(key, "", type=str)
            if val:
                combo.blockSignals(True)
                combo.setCurrentText(val)
                combo.blockSignals(False)
        self.last_folder = self.settings.value("last_folder", "", type=str) or None

        # Initial state + restore last folder if it still exists
        self._set_ui_state(UiState.IDLE)
        if self.last_folder and os.path.isdir(self.last_folder):
            self._scan_folder(self.last_folder)
        else:
            self.status.setText("Open a folder of EdgeTX logs to begin, or plug in a radio.")

        # Background radio detection — light up the "Load from radio" button
        # when a radio appears on disk. Probes off-thread to keep UI smooth.
        self.radio_timer = QTimer(self)
        self.radio_timer.setInterval(1000)
        self.radio_timer.timeout.connect(self._radio_poll_tick)
        self.radio_timer.start()

    # ---------- UI state machine ----------
    def _set_ui_state(self, state: UiState) -> None:
        self._ui_state = state
        for attr, enabled in _UI_STATE_TABLE[state].items():
            getattr(self, attr).setEnabled(enabled)

    # ---------- JS helpers ----------
    def on_map_loaded(self, ok: bool):
        self.map_ready = bool(ok)
        if self.map_ready:
            # initialize proxy port before running pending JS
            self.web.page().runJavaScript(f"if (window.initMap) {{ window.initMap({self.cache_port}); }}") # type:ignore

            if self._pending_js:
                for code in self._pending_js:
                    self.web.page().runJavaScript(code)
                self._pending_js.clear()

    def run_js(self, code: str) -> None:
        if self.map_ready:
            self.web.page().runJavaScript(code)
        else:
            self._pending_js.append(code)

    # ---------- Toggle handlers ----------
    def on_follow_toggled(self, checked: bool):
        self.run_js(f"if (window.setFollowMode) {{ setFollowMode({str(bool(checked)).lower()}); }}")

    def on_smooth_toggled(self, checked: bool):
        self.run_js(f"if (window.setSmoothPlaybackEnabled) {{ setSmoothPlaybackEnabled({str(bool(checked)).lower()}); }}")

    def on_show_full_toggled(self, checked: bool):
        self.run_js(f"if (window.setFullPathVisible) {{ setFullPathVisible({str(bool(checked)).lower()}); }}")
    
    def on_map_style_changed(self, _idx: int):
        style = self.map_style_combo.currentText()
        self.run_js(f"if (window.setBaseMap) {{ setBaseMap('{style}'); }}")

    # ---------- Data loading ----------
    def open_folder_dialog(self):
        start = self.last_folder or os.path.expanduser("~")
        # Native dialog is the familiar UX for end users. The Qt dialog is an
        # escape hatch for machines where filesystem-level hooks (Zscaler,
        # corporate antivirus, shell extensions) make the native dialog slow
        # — set BREADCRUMBS_QT_DIALOG=1 to opt in.
        use_qt_dialog = os.environ.get("BREADCRUMBS_QT_DIALOG", "") == "1"
        dlg = QFileDialog(self, "Open EdgeTX logs folder", start)
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        dlg.setOption(QFileDialog.Option.ShowDirsOnly, False)
        if use_qt_dialog:
            dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if dlg.exec():
            paths = dlg.selectedFiles()
            if paths:
                self._scan_folder(paths[0])

    def load_from_radio(self):
        self.status.setText("Looking for radio…")
        worker = Worker(find_mounted_edgetx_once)
        worker.signals.done.connect(self._on_radio_found)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    def _on_radio_found(self, mount):
        if not mount:
            self.status.setText("No radio found.")
            QMessageBox.information(
                self, "No radio found",
                "Plug your EdgeTX radio into USB in mass-storage mode and try again.",
            )
            return
        logs_dir = os.path.join(mount, "LOGS")
        if not os.path.isdir(logs_dir):
            self.status.setText(f"Radio mounted at {mount} but no LOGS/ folder.")
            QMessageBox.warning(
                self, "No LOGS folder",
                f"Radio mounted at {mount} but no LOGS/ directory was found.",
            )
            return
        self._scan_folder(logs_dir)

    def _scan_folder(self, logs_dir: str):
        if not os.path.isdir(logs_dir):
            self.status.setText(f"Folder not found: {logs_dir}")
            self.disable_everything()
            self.run_js("if (window.clearTrack) { clearTrack(); }")
            return

        self.last_folder = logs_dir
        self.status.setText(f"Indexing logs in {logs_dir} …")
        self._set_ui_state(UiState.INDEXING)
        worker = Worker(scan_logs_dir, logs_dir)
        # Opt-in progress reporting (see WorkerSignals.progress).
        worker.kwargs["progress"] = worker.signals.progress.emit
        worker.signals.done.connect(self.on_index_built)
        worker.signals.progress.connect(self.on_scan_progress)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    def on_scan_progress(self, done: int, total: int):
        self.status.setText(f"Indexing… {done}/{total} files")

    # ---------- Drag-and-drop ----------
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    p = u.toLocalFile()
                    if os.path.isdir(p) or p.lower().endswith(".csv"):
                        e.acceptProposedAction()
                        return
        e.ignore()

    def dropEvent(self, e):
        if not e.mimeData().hasUrls():
            return
        for u in e.mimeData().urls():
            if not u.isLocalFile():
                continue
            p = u.toLocalFile()
            if os.path.isdir(p):
                self._scan_folder(p)
                return
            if p.lower().endswith(".csv"):
                self._scan_folder(os.path.dirname(p))
                return

    # ---------- Background radio polling ----------
    def _radio_poll_tick(self):
        if self._radio_check_inflight:
            return
        self._radio_check_inflight = True
        worker = Worker(find_mounted_edgetx_once)
        worker.signals.done.connect(self._on_radio_poll_done)
        worker.signals.error.connect(lambda _msg: setattr(self, "_radio_check_inflight", False))
        self.thread_pool.start(worker)

    def _on_radio_poll_done(self, mount):
        self._radio_check_inflight = False
        if mount == self._radio_mount:
            return
        self._radio_mount = mount
        if mount:
            self.btn_load_radio.setText("Load from radio ●")
            self.btn_load_radio.setToolTip(f"Radio detected at {mount}")
        else:
            self.btn_load_radio.setText(self._radio_default_text)
            self.btn_load_radio.setToolTip("")

    # ---------- Lifecycle ----------
    def closeEvent(self, e):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        if self.last_folder:
            s.setValue("last_folder", self.last_folder)
        s.setValue("metric", self.metric_combo.currentText())
        s.setValue("speed", self.speed_combo.currentText())
        s.setValue("map_style", self.map_style_combo.currentText())

        if self.radio_timer.isActive():
            self.radio_timer.stop()
        shutdown_cache_server(self.cache_server)
        super().closeEvent(e)

    def disable_everything(self):
        self._set_ui_state(UiState.IDLE)

        self.sessions_by_day = {}
        self.current_track = None

        self.pause_playback()

        self.timeline_s = []
        self.total_duration_s = 0.0
        self.playback_elapsed_s = 0.0
        self.playback_progress = 0.0
        self._auto_hid_full_path = False

        self._clear_stats()
        self.update_time_label()

    def on_worker_error(self, msg: str):
        QMessageBox.critical(self, "Error", msg)

    def on_index_built(self, sessions_by_day: dict):
        self.sessions_by_day = sessions_by_day

        days = sorted(self.sessions_by_day.keys(), reverse=True)

        self.day_combo.blockSignals(True)
        self.day_combo.clear()
        self.day_combo.addItems(days)
        self.day_combo.blockSignals(False)

        self.current_track = None
        self._auto_hid_full_path = False
        self.pause_playback()
        self.run_js("if (window.clearTrack) { clearTrack(); }")

        if not days:
            self._set_ui_state(UiState.IDLE)
            self.status.setText("No CSV logs found.")
            self.session_list.clear()
            return

        self._set_ui_state(UiState.INDEXED)
        total = sum(len(v) for v in sessions_by_day.values())
        self.status.setText(f"Indexed {total} session(s). Select a day/session.")
        self.populate_sessions(days[0])

    def on_day_changed(self, _idx: int):
        day = self.day_combo.currentText()
        if day:
            self.populate_sessions(day)

    def populate_sessions(self, day: str):
        self.session_list.clear()
        metas = self.sessions_by_day.get(day, [])

        for meta in metas:
            item = QListWidgetItem(meta.label())
            item.setData(Qt.ItemDataRole.UserRole, meta)
            self.session_list.addItem(item)

        self.current_track = None
        self._auto_hid_full_path = False
        self.pause_playback()
        self._set_ui_state(UiState.INDEXED)

        self.status.setText(f"{day}: {len(metas)} session(s). Select one to plot.")
        self.run_js("if (window.clearTrack) { clearTrack(); }")

    def on_session_clicked(self, item: QListWidgetItem):
        meta: SessionMeta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return

        self.status.setText(f"Loading track: {os.path.basename(meta.file_path)} ...")

        self.current_track = None
        self._auto_hid_full_path = False
        self.pause_playback()
        self._set_ui_state(UiState.LOADING)

        worker = Worker(load_track, meta.file_path, 10000)
        worker.signals.done.connect(self.on_track_loaded)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    def on_track_loaded(self, track: list[TrackPoint]):
        if not track:
            self.status.setText("No GPS points found in session.")
            self.run_js("if (window.clearTrack) { clearTrack(); }")
            self._set_ui_state(UiState.INDEXED)
            return

        self.current_track = track

        # Default metric
        self.metric_combo.blockSignals(True)
        self.metric_combo.setCurrentText("Progress")
        self.metric_combo.blockSignals(False)

        # Timeline
        self.timeline_s = build_timeline_seconds(track)
        self.total_duration_s = self.timeline_s[-1] if self.timeline_s else 0.0
        self.playback_elapsed_s = 0.0
        self.playback_progress = 0.0

        # Slider uses index positions
        self.play_slider.blockSignals(True)
        self.play_slider.setMinimum(0)
        self.play_slider.setMaximum(max(0, len(track) - 1))
        self.play_slider.setValue(0)
        self.play_slider.blockSignals(False)

        # Checkbox initial values
        self.follow_checkbox.setChecked(False)
        self.smooth_checkbox.setChecked(True)
        self.show_full_checkbox.setChecked(True)

        self._set_ui_state(UiState.LOADED)

        self._refresh_stats(track)

        # Plot + send timeline to JS
        self.plot_current_track()
        self.send_timeline_to_js()

        # Apply toggles
        self.on_smooth_toggled(self.smooth_checkbox.isChecked())
        self.on_show_full_toggled(self.show_full_checkbox.isChecked())
        self.on_follow_toggled(self.follow_checkbox.isChecked())

        # Reset JS playback to start
        self.run_js("if (window.stopPlayback) { stopPlayback(); }")

        self.update_time_label()
        self.status.setText(f"Loaded {len(track)} points. Ready to play.")

    # ---------- Timeline building ----------

    def send_timeline_to_js(self):
        if not self.timeline_s:
            return
        self.run_js(f"if (window.setTimelineSeconds) {{ setTimelineSeconds({json.dumps(self.timeline_s)}); }}")

    # ---------- Gradient plotting ----------
    def on_metric_changed(self, _idx: int):
        if not self.current_track:
            return

        cur_time = float(self.playback_elapsed_s)
        was_playing = self.playing

        self.pause_playback()

        self.plot_current_track()
        self.send_timeline_to_js()

        self.on_smooth_toggled(self.smooth_checkbox.isChecked())
        self.on_show_full_toggled(self.show_full_checkbox.isChecked())
        self.on_follow_toggled(self.follow_checkbox.isChecked())
        self.on_speed_changed(self.speed_combo.currentIndex())

        self.seek_to_time(cur_time)

        if was_playing:
            self.start_playback()

    def plot_current_track(self):
        if not self.current_track:
            return

        metric = self.metric_combo.currentText()
        data_meta = build_hotline_payload(self.current_track, metric)

        if data_meta is None:
            self.status.setText(f"No {metric} data available; falling back to Progress.")
            self.metric_combo.blockSignals(True)
            self.metric_combo.setCurrentText("Progress")
            self.metric_combo.blockSignals(False)
            data_meta = build_hotline_payload(self.current_track, "Progress")

        data, meta = data_meta # type:ignore
        js = f"if (window.plotHotline) {{ plotHotline({json.dumps(data)}, {json.dumps(meta)}); }}"
        self.run_js(js)

        # plotHotline clears prior dropouts via clearTrack; re-plot them on top.
        segments = build_dropout_segments(self.current_track)
        if segments:
            self.run_js(
                f"if (window.plotDropouts) {{ plotDropouts({json.dumps(segments)}); }}"
            )



    # ---------- Playback control (JS-driven) ----------
    def on_speed_changed(self, _idx: int):
        mult = self.get_speed_multiplier()
        self.run_js(f"if (window.setPlaybackSpeed) {{ setPlaybackSpeed({mult}); }}")

    def get_speed_multiplier(self) -> float:
        t = self.speed_combo.currentText().strip().lower().replace("x", "")
        try:
            return float(t)
        except Exception:
            return 1.0

    def update_time_label(self):
        total = self.total_duration_s if self.total_duration_s > 0 else 0.0
        cur = max(0.0, min(self.playback_elapsed_s, total))
        self.time_label.setText(f"{format_time(cur)} / {format_time(total)}")

    def set_playing_ui(self, playing: bool):
        self.playing = playing
        if playing:
            self.btn_play_pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        else:
            self.btn_play_pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

    def go_to_start(self):
        self.pause_playback()
        self.seek_to_index(0)

    def toggle_play_pause(self):
        if not self.current_track:
            return
        if self.playing:
            self.pause_playback()
        else:
            self.start_playback()

    def start_playback(self):
        if not self.current_track:
            return

        # Auto-hide full path when playback starts
        if self.show_full_checkbox.isChecked():
            self._auto_hid_full_path = True
            self.show_full_checkbox.setChecked(False)
        else:
            self._auto_hid_full_path = False

        self.on_speed_changed(self.speed_combo.currentIndex())

        self.run_js("if (window.playPlayback) { playPlayback(); }")
        self.set_playing_ui(True)

        if not self.play_timer.isActive():
            self.play_timer.start()

    def pause_playback(self):
        self.run_js("if (window.pausePlayback) { pausePlayback(); }")
        self.set_playing_ui(False)
        if self.play_timer.isActive():
            self.play_timer.stop()

    def stop_playback(self):
        self.run_js("if (window.stopPlayback) { stopPlayback(); }")
        self.set_playing_ui(False)
        if self.play_timer.isActive():
            self.play_timer.stop()

        self.playback_elapsed_s = 0.0
        self.playback_progress = 0.0
        self.update_time_label()

        self.play_slider.blockSignals(True)
        self.play_slider.setValue(0)
        self.play_slider.blockSignals(False)

        if self._auto_hid_full_path:
            self.show_full_checkbox.setChecked(True)
            self._auto_hid_full_path = False

    def seek_to_time(self, seconds: float):
        seconds = max(0.0, float(seconds))
        self.playback_elapsed_s = seconds
        self.update_time_label()
        self.run_js(f"if (window.seekPlaybackTime) {{ seekPlaybackTime({seconds}); }}")

    def seek_to_index(self, idx: int):
        if not self.current_track:
            return
        idx = max(0, min(int(idx), len(self.current_track) - 1))

        if self.timeline_s and idx < len(self.timeline_s):
            self.playback_elapsed_s = float(self.timeline_s[idx])
        else:
            self.playback_elapsed_s = float(idx)

        self.playback_progress = float(idx)
        self.update_time_label()

        self.play_slider.blockSignals(True)
        self.play_slider.setValue(idx)
        self.play_slider.blockSignals(False)

        self.run_js(f"if (window.seekPlaybackIndex) {{ seekPlaybackIndex({idx}); }}")

    def _seek_index_relative(self, delta: int) -> None:
        if not self.current_track:
            return
        self.pause_playback()
        self.seek_to_index(self.play_slider.value() + delta)

    def _seek_time_relative(self, delta_s: float) -> None:
        if not self.current_track:
            return
        self.pause_playback()
        new_t = max(0.0, min(self.playback_elapsed_s + delta_s, self.total_duration_s))
        self.seek_to_time(new_t)

    # ---------- Slider events ----------
    def on_slider_pressed(self):
        self.user_scrubbing = True
        self.pause_playback()

    def on_slider_moved(self, value: int):
        self.seek_to_index(value)

    def on_slider_released(self):
        self.user_scrubbing = False

    def on_slider_value_changed(self, value: int):
        if self.user_scrubbing:
            return
        self.pause_playback()
        self.seek_to_index(value)

    # ---------- Poll JS playback status ----------
    def on_poll_tick(self):
        if not self.map_ready:
            return
        self.web.page().runJavaScript(
            "JSON.stringify(window.getPlaybackStatus ? getPlaybackStatus() : {});",
            self.on_js_playback_status,
        )

    def on_js_playback_status(self, res):
        # res is a JSON string from JavaScript, parse it
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:
                return
        
        if not isinstance(res, dict):
            return

        try:
            t = float(res.get("time", 0.0))
        except Exception:
            t = 0.0
        try:
            dur = float(res.get("duration", self.total_duration_s or 0.0))
        except Exception:
            dur = self.total_duration_s or 0.0
        try:
            progress = float(res.get("progress", 0.0))
        except Exception:
            progress = 0.0
        js_playing = bool(res.get("playing", False))

        self.playback_elapsed_s = t
        self.total_duration_s = dur
        self.playback_progress = progress

        if not self.user_scrubbing:
            # Update slider from JS playback status (with signals blocked)
            i = round(progress)
            i = max(0, min(i, self.play_slider.maximum()))
            self.play_slider.blockSignals(True)
            self.play_slider.setValue(i)
            self.play_slider.blockSignals(False)
            self.play_slider.repaint()  # Force visual update

        self.update_time_label()

        # If JS stopped (end reached), update UI state
        if self.playing and not js_playing:
            self.set_playing_ui(False)
            if self.play_timer.isActive():
                self.play_timer.stop()
            self.status.setText("Playback finished.")

    # ---------- Flight stats ----------
    def _clear_stats(self) -> None:
        for w in (
            self.stat_duration, self.stat_distance, self.stat_max_range,
            self.stat_max_alt, self.stat_max_speed, self.stat_rssi,
        ):
            w.setText("—")

    def _refresh_stats(self, track: list[TrackPoint]) -> None:
        s: FlightStats = compute_flight_stats(track)
        self.stat_duration.setText(format_time(s.duration_s) if s.duration_s else "—")

        if s.distance_m >= 1000:
            self.stat_distance.setText(f"{s.distance_m / 1000:.2f} km")
        else:
            self.stat_distance.setText(f"{s.distance_m:.0f} m")

        if s.max_dist_from_launch_m >= 1000:
            self.stat_max_range.setText(f"{s.max_dist_from_launch_m / 1000:.2f} km")
        else:
            self.stat_max_range.setText(f"{s.max_dist_from_launch_m:.0f} m")

        self.stat_max_alt.setText(f"{s.max_alt_m:.0f} m" if s.max_alt_m is not None else "—")
        self.stat_max_speed.setText(
            f"{s.max_speed_kmh:.1f} km/h" if s.max_speed_kmh is not None else "—"
        )

        if s.avg_rssi_db is not None and s.min_rssi_db is not None:
            self.stat_rssi.setText(f"avg {s.avg_rssi_db:.0f} / min {s.min_rssi_db:.0f} dB")
        else:
            self.stat_rssi.setText("—")

    # ---------- Export ----------
    def export_kml(self):
        if not self.current_track:
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save KML", "flight_session.kml", "KML (*.kml)"
        )
        if not out_path:
            return

        export_track_to_kml(
            self.current_track,
            out_path,
            name="EdgeTX Flight Session",
            altitude_mode="relativeToGround",
        )
        QMessageBox.information(self, "Export complete", f"Saved KML:\n{out_path}\n\nOpen in Google Earth for 3D view.")

    def export_gpx(self):
        if not self.current_track:
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save GPX", "flight_session.gpx", "GPX (*.gpx)"
        )
        if not out_path:
            return

        export_track_to_gpx(
            self.current_track,
            out_path,
            name="EdgeTX Flight Session",
        )
        QMessageBox.information(self, "Export complete", f"Saved GPX:\n{out_path}")


def run():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()