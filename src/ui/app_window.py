import sys
import os
import json
import bisect
import statistics
from typing import Optional

from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool, QUrl, Qt, QTimer
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


class WorkerSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
            self.signals.done.emit(res)
        except Exception as e:
            self.signals.error.emit(str(e))


def _format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:d}:{sec:02d}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Drone Recovery")
        self.resize(1200, 800)

        self.thread_pool = QThreadPool.globalInstance()

        # State
        self.sessions_by_day: dict[str, list[SessionMeta]] = {}
        self.current_track: list[TrackPoint] | None = None

        # Playback timeline (seconds from start)
        self.timeline_s: list[float] = []
        self.total_duration_s: float = 0.0

        # Playback state
        self.playing: bool = False
        self.user_scrubbing: bool = False
        self.playback_elapsed_s: float = 0.0     # "playhead" time in seconds
        self.playback_progress: float = 0.0      # float index progress (i + frac)

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

        row2 = QWidget()
        row2_layout = QHBoxLayout(row2)
        row2_layout.setContentsMargins(0, 0, 0, 0)

        self.time_label = QLabel("0:00 / 0:00")
        row2_layout.addWidget(self.time_label)

        self.follow_checkbox = QCheckBox("Follow")
        self.follow_checkbox.setEnabled(False)
        row2_layout.addWidget(self.follow_checkbox)

        row2_layout.addStretch(1)
        left_layout.addWidget(row2)

        # ---- Export ----
        self.btn_export_kml = QPushButton("Export KML (Google Earth)")
        self.btn_export_kml.setEnabled(False)
        left_layout.addWidget(self.btn_export_kml)

        # --- UI (map panel) ---
        self.web = QWebEngineView()

        # Allow file:// HTML to load remote resources (Leaflet + tiles + hotline)
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

        # Playback timer (drives animation)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(33)  # ~30 FPS
        self.play_timer.timeout.connect(self.on_play_tick)

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

        self.sessions_by_day = {}
        self.current_track = None
        self.pause_playback()
        self.timeline_s = []
        self.total_duration_s = 0.0
        self.playback_elapsed_s = 0.0
        self.playback_progress = 0.0
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

        # Default to Progress when a new session is loaded
        self.metric_combo.setEnabled(True)
        self.metric_combo.blockSignals(True)
        self.metric_combo.setCurrentText("Progress")
        self.metric_combo.blockSignals(False)

        # Build playback timeline (time-based if timestamps are usable, else fallback)
        self.timeline_s = self.build_timeline_seconds(track)
        self.total_duration_s = self.timeline_s[-1] if self.timeline_s else 0.0

        # Reset playback to start
        self.pause_playback()
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

        self.btn_export_kml.setEnabled(True)

        # Plot map + set playback position to start (drone icon + empty trail)
        self.plot_current_track()
        self.push_playback_progress()

        self.update_time_label()
        self.status.setText(f"Loaded {len(track)} points. Ready to play.")

    # ---------- Timeline building ----------
    def build_timeline_seconds(self, track: list[TrackPoint]) -> list[float]:
        """
        Returns monotonic elapsed seconds for each point.
        Prefers timestamps if present; falls back to 1s-per-point if not.
        """
        # Estimate dt from timestamp diffs if possible
        diffs = []
        prev_t = None
        for p in track:
            if p.t is None:
                continue
            if prev_t is not None:
                d = (p.t - prev_t).total_seconds()
                if 0 < d < 300:  # ignore huge gaps
                    diffs.append(d)
            prev_t = p.t

        dt_est = statistics.median(diffs) if diffs else 1.0

        # First valid timestamp
        base = None
        for p in track:
            if p.t is not None:
                base = p.t
                break

        if base is None:
            # No timestamps at all: assume 1s per point
            return [i * 1.0 for i in range(len(track))]

        timeline = []
        last = 0.0
        for p in track:
            if p.t is None:
                last = last + dt_est
            else:
                delta = (p.t - base).total_seconds()
                # enforce monotonic
                if delta < last:
                    delta = last + dt_est
                last = delta
            timeline.append(last)

        # if something weird happens, fallback safely
        if not timeline or timeline[-1] < 0:
            return [i * 1.0 for i in range(len(track))]

        return timeline

    # ---------- Gradient plotting ----------
    def on_metric_changed(self, _idx: int):
        if self.current_track:
            # Recolor the whole path but keep playhead position
            self.plot_current_track()
            self.push_playback_progress()

    def plot_current_track(self):
        if not self.current_track:
            return

        metric = self.metric_combo.currentText()
        data_meta = self.build_hotline_payload(self.current_track, metric)

        if data_meta is None:
            self.status.setText(f"No {metric} data available; falling back to Progress.")
            self.metric_combo.blockSignals(True)
            self.metric_combo.setCurrentText("Progress")
            self.metric_combo.blockSignals(False)
            data_meta = self.build_hotline_payload(self.current_track, "Progress")

        data, meta = data_meta # type:ignore
        js = f"if (window.plotHotline) {{ plotHotline({json.dumps(data)}, {json.dumps(meta)}); }}"
        self.run_js(js)

    def build_hotline_payload(self, track: list[TrackPoint], metric: str):
        n = len(track)
        if n < 2:
            return None

        if metric == "Progress":
            values = [(i / (n - 1)) if n > 1 else 0.0 for i in range(n)]
            vmin, vmax = 0.0, 1.0
            meta = {
                "title": "Progress",
                "min": vmin,
                "max": vmax,
                "label_min": "Start",
                "label_max": "End",
            }

        elif metric == "RSSI":
            raw = [p.rssi_db for p in track]
            present = [v for v in raw if v is not None]
            if not present:
                return None
            vmin, vmax = min(present), max(present)
            meta = {
                "title": "RSSI (dB)",
                "min": vmin,
                "max": vmax,
                "label_min": f"{vmin:.0f} dB",
                "label_max": f"{vmax:.0f} dB",
            }
            values = raw

        elif metric == "Speed":
            raw = [p.speed_kmh for p in track]
            present = [v for v in raw if v is not None]
            if not present:
                return None
            vmin, vmax = min(present), max(present)
            meta = {
                "title": "Speed (km/h)",
                "min": vmin,
                "max": vmax,
                "label_min": f"{vmin:.1f} km/h",
                "label_max": f"{vmax:.1f} km/h",
            }
            values = raw

        elif metric == "Altitude":
            raw = [p.alt_m for p in track]
            present = [v for v in raw if v is not None]
            if not present:
                return None
            vmin, vmax = min(present), max(present)
            meta = {
                "title": "Altitude (m)",
                "min": vmin,
                "max": vmax,
                "label_min": f"{vmin:.1f} m",
                "label_max": f"{vmax:.1f} m",
            }
            values = raw

        else:
            return None

        if abs(meta["max"] - meta["min"]) < 1e-12:
            meta["max"] = meta["min"] + 1e-6

        # Fill missing values (so every point has z)
        filled = []
        last_val = None
        for v in values:
            if v is None:
                filled.append(last_val)
            else:
                filled.append(float(v))
                last_val = float(v)

        first_non = next((v for v in filled if v is not None), None)
        fallback = first_non if first_non is not None else float(meta["min"])
        filled = [fallback if v is None else v for v in filled]

        data = [[p.lat, p.lon, float(z)] for p, z in zip(track, filled)]
        return data, meta

    # ---------- Playback ----------
    def on_follow_toggled(self, checked: bool):
        self.run_js(f"if (window.setFollowMode) {{ setFollowMode({str(bool(checked)).lower()}); }}")

    def on_speed_changed(self, _idx: int):
        # no immediate action needed; next tick uses the new multiplier
        pass

    def get_speed_multiplier(self) -> float:
        t = self.speed_combo.currentText().strip().lower().replace("x", "")
        try:
            return float(t)
        except Exception:
            return 1.0

    def update_time_label(self):
        total = self.total_duration_s if self.total_duration_s > 0 else 0.0
        cur = max(0.0, min(self.playback_elapsed_s, total))
        self.time_label.setText(f"{_format_time(cur)} / {_format_time(total)}")

    def push_playback_progress(self):
        """Send current playhead progress to JS (marker + trail)."""
        if not self.current_track:
            return
        js = f"if (window.setPlaybackProgress) {{ setPlaybackProgress({float(self.playback_progress):.6f}); }}"
        self.run_js(js)

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
        if self.playback_progress >= (len(self.current_track) - 1):
            # if we're at end, restart
            self.seek_to_index(0)

        self.playing = True
        self.btn_play_pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        if not self.play_timer.isActive():
            self.play_timer.start()

    def pause_playback(self):
        self.playing = False
        self.btn_play_pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        if self.play_timer.isActive():
            self.play_timer.stop()

    def stop_playback(self):
        self.pause_playback()
        self.seek_to_index(0)

    def seek_to_index(self, idx: int):
        if not self.current_track:
            return
        idx = max(0, min(int(idx), len(self.current_track) - 1))
        # Move playhead to the time at that index
        if self.timeline_s and idx < len(self.timeline_s):
            self.playback_elapsed_s = float(self.timeline_s[idx])
        else:
            self.playback_elapsed_s = float(idx)

        self.playback_progress = float(idx)

        # Update UI + map
        self.play_slider.blockSignals(True)
        self.play_slider.setValue(idx)
        self.play_slider.blockSignals(False)

        self.update_time_label()
        self.push_playback_progress()

    def on_slider_pressed(self):
        self.user_scrubbing = True
        self.pause_playback()

    def on_slider_moved(self, value: int):
        # live scrub
        self.seek_to_index(value)

    def on_slider_released(self):
        self.user_scrubbing = False

    def on_slider_value_changed(self, value: int):
        # Handles keyboard adjustments (left/right arrow) too
        if self.user_scrubbing:
            return
        # If user clicked the slider groove, valueChanged fires without sliderMoved
        self.seek_to_index(value)

    def on_play_tick(self):
        if not self.playing or not self.current_track:
            return

        dt = self.play_timer.interval() / 1000.0
        speed = self.get_speed_multiplier()

        if not self.timeline_s or len(self.timeline_s) != len(self.current_track):
            # fallback
            self.timeline_s = [float(i) for i in range(len(self.current_track))]
            self.total_duration_s = self.timeline_s[-1] if self.timeline_s else 0.0

        self.playback_elapsed_s += dt * speed

        # clamp to end
        if self.playback_elapsed_s >= self.total_duration_s:
            self.playback_elapsed_s = self.total_duration_s
            self.playback_progress = float(len(self.current_track) - 1)
            self.push_playback_progress()
            self.update_time_label()

            # snap slider to end
            self.play_slider.blockSignals(True)
            self.play_slider.setValue(len(self.current_track) - 1)
            self.play_slider.blockSignals(False)

            self.pause_playback()
            self.status.setText("Playback finished.")
            return

        # Find segment for current time
        # i is the last index where timeline[i] <= elapsed
        i = bisect.bisect_right(self.timeline_s, self.playback_elapsed_s) - 1
        i = max(0, min(i, len(self.timeline_s) - 2))

        t0 = self.timeline_s[i]
        t1 = self.timeline_s[i + 1]
        frac = 0.0 if (t1 <= t0) else (self.playback_elapsed_s - t0) / (t1 - t0)
        frac = max(0.0, min(1.0, frac))

        self.playback_progress = float(i) + float(frac)

        # Update map
        self.push_playback_progress()

        # Update slider (integer)
        if not self.user_scrubbing:
            self.play_slider.blockSignals(True)
            self.play_slider.setValue(i)
            self.play_slider.blockSignals(False)

        self.update_time_label()

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
            altitude_mode="relativeToGround",  # change to "absolute" if your Alt is MSL
        )
        QMessageBox.information(self, "Export complete", f"Saved KML:\n{out_path}\n\nOpen in Google Earth for 3D view.")


def run():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()