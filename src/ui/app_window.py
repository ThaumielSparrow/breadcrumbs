import sys
import os
import json

from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool, QUrl, Qt
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Drone Recovery")
        self.resize(1200, 800)

        self.thread_pool = QThreadPool.globalInstance()

        # State
        self.sessions_by_day: dict[str, list[SessionMeta]] = {}
        self.current_track: list[TrackPoint] | None = None

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
            # Some builds expose attributes directly (older API style)
            try:
                s.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True) # type:ignore
                s.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True) # type:ignore
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

        # Signals
        self.btn_scan.clicked.connect(self.scan_test_logs)
        self.day_combo.currentIndexChanged.connect(self.on_day_changed)
        self.session_list.itemClicked.connect(self.on_session_clicked)
        self.metric_combo.currentIndexChanged.connect(self.on_metric_changed)
        self.btn_export_kml.clicked.connect(self.export_kml)

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
            self.day_combo.setEnabled(False)
            self.session_list.setEnabled(False)
            self.metric_combo.setEnabled(False)
            self.btn_export_kml.setEnabled(False)
            self.sessions_by_day = {}
            self.current_track = None
            self.run_js("if (window.clearTrack) { clearTrack(); }")
            return

        self.status.setText(f"Indexing logs in {logs_dir} ...")
        worker = Worker(scan_logs_dir, logs_dir)
        worker.signals.done.connect(self.on_index_built)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

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

        # Reset state
        self.metric_combo.setEnabled(False)
        self.btn_export_kml.setEnabled(False)
        self.current_track = None
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
        self.status.setText(f"{day}: {len(metas)} session(s). Select one to plot.")
        self.run_js("if (window.clearTrack) { clearTrack(); }")

    def on_session_clicked(self, item: QListWidgetItem):
        meta: SessionMeta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return

        self.status.setText(f"Loading track: {os.path.basename(meta.file_path)} ...")
        self.metric_combo.setEnabled(False)
        self.btn_export_kml.setEnabled(False)
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
        self.metric_combo.setEnabled(True)
        self.btn_export_kml.setEnabled(True)

        # Default to Progress on each newly loaded track
        self.metric_combo.blockSignals(True)
        self.metric_combo.setCurrentText("Progress")
        self.metric_combo.blockSignals(False)

        self.plot_current_track()

    # ---------- Gradient plotting ----------
    def on_metric_changed(self, _idx: int):
        if self.current_track:
            self.plot_current_track()

    def plot_current_track(self):
        if not self.current_track:
            return

        metric = self.metric_combo.currentText()
        data_meta = self.build_hotline_payload(self.current_track, metric)

        # If chosen metric isn't available, fall back to Progress
        if data_meta is None:
            self.status.setText(f"No {metric} data available for this session; falling back to Progress.")
            self.metric_combo.blockSignals(True)
            self.metric_combo.setCurrentText("Progress")
            self.metric_combo.blockSignals(False)
            data_meta = self.build_hotline_payload(self.current_track, "Progress")

        data, meta = data_meta # type:ignore
        js = f"if (window.plotHotline) {{ plotHotline({json.dumps(data)}, {json.dumps(meta)}); }}"
        self.run_js(js)

        last = self.current_track[-1]
        alt_str = f", Alt {last.alt_m:.1f}m" if last.alt_m is not None else ""
        self.status.setText(
            f"Plotted {len(self.current_track)} points. "
            f"Last: {last.lat:.6f}, {last.lon:.6f}{alt_str} • Color by: {self.metric_combo.currentText()}"
        )

    def build_hotline_payload(self, track: list[TrackPoint], metric: str):
        n = len(track)
        if n < 2:
            return None

        # Build values list based on metric
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

        # Guard: avoid min == max (some renderers do divide-by-zero internally)
        if abs(vmax - vmin) < 1e-12:
            vmax = vmin + 1e-6
            meta["max"] = vmax

        # Fill missing values so every point has a numeric z
        filled: list[float | None] = []
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

        # data format expected by leaflet-hotline: [lat, lon, z]
        data = [[p.lat, p.lon, float(z)] for p, z in zip(track, filled)] # type:ignore
        return data, meta

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