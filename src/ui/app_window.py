import sys
import os
import json

from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QComboBox,
    QFileDialog, QMessageBox
)

from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from core.drive import find_mounted_edgetx_once
from core.sessions import scan_logs_dir, SessionMeta
from core.load import load_track, TrackPoint
from core.export_kml import export_track_to_kml

_LOAD_TEST_DIR = True

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

        self.mount_root: str | None = None
        self.sessions_by_day: dict[str, list[SessionMeta]] = {}
        self.current_track: list[TrackPoint] | None = None
        self.map_ready = False
        self.pending_coords = None

        # Left panel
        left = QWidget()
        left_layout = QVBoxLayout(left)

        self.status = QLabel("Not connected")
        left_layout.addWidget(self.status)

        self.btn_scan = QPushButton("Scan for radio")
        left_layout.addWidget(self.btn_scan)

        self.day_combo = QComboBox()
        self.day_combo.setEnabled(False)
        left_layout.addWidget(self.day_combo)

        self.session_list = QListWidget()
        self.session_list.setEnabled(False)
        left_layout.addWidget(self.session_list)

        self.btn_export_kml = QPushButton("Export KML (Google Earth)")
        self.btn_export_kml.setEnabled(False)
        left_layout.addWidget(self.btn_export_kml)

        # Map panel
        self.web = QWebEngineView()
        s = self.web.settings()

        # Qt6: attributes are under QWebEngineSettings.WebAttribute
        attr_remote = getattr(QWebEngineSettings, "LocalContentCanAccessRemoteUrls", None)
        attr_file = getattr(QWebEngineSettings, "LocalContentCanAccessFileUrls", None)
        if attr_remote is None:
            attr_remote = QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls
        if attr_file is None:
            attr_file = QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls

        s.setAttribute(attr_remote, True)
        s.setAttribute(attr_file, True)
        
        self.map_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "map_view.html"))
        self.web.load(QUrl.fromLocalFile(self.map_path))
        self.web.loadFinished.connect(self.on_map_loaded)

        # Layout
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.addWidget(left, 3)
        layout.addWidget(self.web, 7)
        self.setCentralWidget(root)

        # Wiring
        self.btn_scan.clicked.connect(self.scan_radio)
        self.day_combo.currentIndexChanged.connect(self.on_day_changed)
        self.session_list.itemClicked.connect(self.on_session_clicked)
        self.btn_export_kml.clicked.connect(self.export_kml)

        # initial scan
        self.scan_radio()

    def on_map_loaded(self, ok: bool):
        self.map_ready = ok
        if ok and self.pending_coords is not None:
            self._plot_coords(self.pending_coords)
            self.pending_coords = None

    def _plot_coords(self, coords):
        # coords = [[lat,lon],...]
        js = f"plotTrack({json.dumps(coords)});"
        self.web.page().runJavaScript(js)

    def scan_radio(self):
        # --- TEST MODE: load logs from src/test ---
        test_logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "test"))
        if os.path.isdir(test_logs_dir) and _LOAD_TEST_DIR:
            self.status.setText(f"TEST MODE: Loading logs from {test_logs_dir} …")
            worker = Worker(scan_logs_dir, test_logs_dir)
            worker.signals.done.connect(self.on_index_built)
            worker.signals.error.connect(self.on_worker_error)
            self.thread_pool.start(worker)
            return

        # --- NORMAL MODE (radio) ---
        self.status.setText("Scanning for radio...")
        mount = find_mounted_edgetx_once()
        if not mount:
            self.mount_root = None
            self.sessions_by_day = {}
            self.day_combo.clear()
            self.session_list.clear()
            self.day_combo.setEnabled(False)
            self.session_list.setEnabled(False)
            self.btn_export_kml.setEnabled(False)
            self.current_track = None
            self.status.setText("No radio found. Plug it in and click Scan.")
            self.web.page().runJavaScript("clearTrack();")
            return

        self.mount_root = mount
        self.status.setText(f"Found radio at {mount}. Indexing logs…")

        logs_dir = os.path.join(mount, "LOGS")
        if not os.path.isdir(logs_dir):
            self.status.setText(f"Found radio, but no LOGS folder at: {logs_dir}")
            return

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

        if not days:
            self.status.setText("No CSV logs found.")
            self.session_list.clear()
            self.web.page().runJavaScript("clearTrack();")
            return

        self.status.setText(f"Indexed {sum(len(v) for v in sessions_by_day.values())} session(s).")
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

        self.btn_export_kml.setEnabled(False)
        self.current_track = None
        self.status.setText(f"{day}: {len(metas)} session(s). Select one to plot.")
        self.web.page().runJavaScript("clearTrack();")

    def on_session_clicked(self, item: QListWidgetItem):
        meta: SessionMeta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return

        self.status.setText(f"Loading track: {os.path.basename(meta.file_path)} …")
        self.btn_export_kml.setEnabled(False)
        self.current_track = None

        worker = Worker(load_track, meta.file_path, 10000)
        worker.signals.done.connect(self.on_track_loaded)
        worker.signals.error.connect(self.on_worker_error)
        self.thread_pool.start(worker)

    def on_track_loaded(self, track: list[TrackPoint]):
        if not track:
            self.status.setText("No GPS points found in session.")
            self.web.page().runJavaScript("clearTrack();")
            return

        self.current_track = track
        coords = [[p.lat, p.lon] for p in track]

        if self.map_ready:
            self._plot_coords(coords)
        else:
            self.pending_coords = coords

        last = track[-1]
        alt_str = f", Alt {last.alt_m:.1f}m" if last.alt_m is not None else ""
        self.status.setText(f"Plotted {len(track)} points. Last: {last.lat:.6f}, {last.lon:.6f}{alt_str}")
        self.btn_export_kml.setEnabled(True)

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