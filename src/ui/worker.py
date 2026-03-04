"""Threading and worker utilities for the UI."""

from PySide6.QtCore import QObject, Signal, QRunnable


class WorkerSignals(QObject):
    """Signals emitted by Worker threads."""
    done = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    """Generic worker for running functions in a thread pool."""
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


def format_time(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    seconds = max(0.0, float(seconds))
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:d}:{sec:02d}"
