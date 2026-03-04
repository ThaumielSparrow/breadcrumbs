import sys
import os
import json

from PySide6.QtCore import QThreadPool, QUrl, Qt, QTimer
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
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from core.sessions import scan_logs_dir, SessionMeta
from core.load import load_track, TrackPoint
from core.export_kml import export_track_to_kml
from .worker import Worker, format_time
from .plotting import build_timeline_seconds, build_hotline_payload


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Drone Recovery")
        self.resize(1200, 800)

        self.thread_pool = QThreadPool.globalInstance()

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

        # --- UI (left panel) ---
        left = QWidget()
        left_layout = QVBoxLayout(left)

        self.status = QLabel("Ready")
        left_layout.addWidget(self.status)

        self.btn_scan = QPushButton("Load test logs (src/test)")
        left_layout.addWidget(self.btn_scan)

        self.day_combo = QComboBox()
        self.day_combo.setEnabled(False)
        left_layout.addWidget(self.day_combo)

        self.session_list = QListWidget()
        self.session_list.setEnabled(False)
        left_layout.addWidget(self.session_list)

        left_layout.addWidget(QLabel("Color by:"))
        self.metric_combo = QComboBox()
        self.metric_combo.addItems(["Progress", "RSSI", "Speed", "Altitude"])
        self.metric_combo.setEnabled(False)
        left_layout.addWidget(self.metric_combo)

        # ---- Playback controls ----
        left_layout.addWidget(QLabel("Playback:"))

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_to_start = QToolButton()
        self.btn_to_start.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward))
        self.btn_to_start.setToolTip("Go to start")
        self.btn_to_start.setEnabled(False)
        row_layout.addWidget(self.btn_to_start)

        self.btn_play_pause = QToolButton()
        self.btn_play_pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_play_pause.setToolTip("Play / Pause")
        self.btn_play_pause.setEnabled(False)
        row_layout.addWidget(self.btn_play_pause)

        self.btn_stop = QToolButton()
        self.btn_stop.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.btn_stop.setToolTip("Stop (reset to start)")
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
        self.btn_export_kml = QPushButton("Export KML (Google Earth)")
        self.btn_export_kml.setEnabled(False)
        left_layout.addWidget(self.btn_export_kml)

        # --- UI (map panel) ---
        self.web = QWebEngineView()

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
        self.btn_scan.clicked.connect(self.scan_test_logs)
        self.day_combo.currentIndexChanged.connect(self.on_day_changed)
        self.session_list.itemClicked.connect(self.on_session_clicked)
        self.metric_combo.currentIndexChanged.connect(self.on_metric_changed)
        self.btn_export_kml.clicked.connect(self.export_kml)

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

        # Auto-load on startup
        self.scan_test_logs()

    # ---------- JS helpers ----------
    def on_map_loaded(self, ok: bool):
        self.map_ready = bool(ok)
        if self.map_ready and self._pending_js:
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

    # ---------- Data loading ----------
    def scan_test_logs(self):
        logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "test"))
        if not os.path.isdir(logs_dir):
            self.status.setText(f"Missing test logs dir: {logs_dir}")
            self.disable_everything()
            self.run_js("if (window.clearTrack) { clearTrack(); }")
            return

        self.status.setText(f"Indexing logs in {logs_dir} ...")
        worker = Worker(scan_logs_dir, logs_dir)
        worker.signals.done.connect(self.on_index_built)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    def disable_everything(self):
        self.day_combo.setEnabled(False)
        self.session_list.setEnabled(False)
        self.metric_combo.setEnabled(False)
        self.btn_export_kml.setEnabled(False)

        self.btn_to_start.setEnabled(False)
        self.btn_play_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.speed_combo.setEnabled(False)
        self.play_slider.setEnabled(False)
        self.follow_checkbox.setEnabled(False)
        self.smooth_checkbox.setEnabled(False)
        self.show_full_checkbox.setEnabled(False)

        self.sessions_by_day = {}
        self.current_track = None

        self.pause_playback()

        self.timeline_s = []
        self.total_duration_s = 0.0
        self.playback_elapsed_s = 0.0
        self.playback_progress = 0.0
        self._auto_hid_full_path = False

        self.update_time_label()

    def on_worker_error(self, msg: str):
        QMessageBox.critical(self, "Error", msg)

    def on_index_built(self, sessions_by_day: dict):
        self.sessions_by_day = sessions_by_day

        days = sorted(self.sessions_by_day.keys(), reverse=True)
        self.day_combo.setEnabled(bool(days))
        self.session_list.setEnabled(bool(days))

        self.day_combo.blockSignals(True)
        self.day_combo.clear()
        self.day_combo.addItems(days)
        self.day_combo.blockSignals(False)

        self.metric_combo.setEnabled(False)
        self.btn_export_kml.setEnabled(False)
        self.current_track = None

        self.pause_playback()
        self.play_slider.setEnabled(False)
        self.btn_to_start.setEnabled(False)
        self.btn_play_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.speed_combo.setEnabled(False)
        self.follow_checkbox.setEnabled(False)
        self.smooth_checkbox.setEnabled(False)
        self.show_full_checkbox.setEnabled(False)
        self._auto_hid_full_path = False

        self.run_js("if (window.clearTrack) { clearTrack(); }")

        if not days:
            self.status.setText("No CSV logs found in src/test.")
            self.session_list.clear()
            return

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

        self.metric_combo.setEnabled(False)
        self.btn_export_kml.setEnabled(False)
        self.current_track = None

        self.pause_playback()
        self.play_slider.setEnabled(False)
        self.btn_to_start.setEnabled(False)
        self.btn_play_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.speed_combo.setEnabled(False)
        self.follow_checkbox.setEnabled(False)
        self.smooth_checkbox.setEnabled(False)
        self.show_full_checkbox.setEnabled(False)
        self._auto_hid_full_path = False

        self.status.setText(f"{day}: {len(metas)} session(s). Select one to plot.")
        self.run_js("if (window.clearTrack) { clearTrack(); }")

    def on_session_clicked(self, item: QListWidgetItem):
        meta: SessionMeta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return

        self.status.setText(f"Loading track: {os.path.basename(meta.file_path)} ...")

        self.metric_combo.setEnabled(False)
        self.btn_export_kml.setEnabled(False)

        self.pause_playback()
        self.play_slider.setEnabled(False)
        self.btn_to_start.setEnabled(False)
        self.btn_play_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.speed_combo.setEnabled(False)
        self.follow_checkbox.setEnabled(False)
        self.smooth_checkbox.setEnabled(False)
        self.show_full_checkbox.setEnabled(False)
        self._auto_hid_full_path = False

        self.current_track = None

        worker = Worker(load_track, meta.file_path, 10000)
        worker.signals.done.connect(self.on_track_loaded)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    def on_track_loaded(self, track: list[TrackPoint]):
        if not track:
            self.status.setText("No GPS points found in session.")
            self.run_js("if (window.clearTrack) { clearTrack(); }")
            return

        self.current_track = track

        # Default metric
        self.metric_combo.setEnabled(True)
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
        self.play_slider.setEnabled(True)
        self.play_slider.setMinimum(0)
        self.play_slider.setMaximum(max(0, len(track) - 1))
        self.play_slider.setValue(0)
        self.play_slider.blockSignals(False)

        # Enable playback UI
        self.btn_to_start.setEnabled(True)
        self.btn_play_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.speed_combo.setEnabled(True)

        self.follow_checkbox.setEnabled(True)
        self.follow_checkbox.setChecked(False)

        self.smooth_checkbox.setEnabled(True)
        self.smooth_checkbox.setChecked(True)

        self.show_full_checkbox.setEnabled(True)
        self.show_full_checkbox.setChecked(True)

        self.btn_export_kml.setEnabled(True)

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


def run():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()