"""Settings: download options, storage, about. Every change is saved immediately."""
import platform
import subprocess
import sys

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from app import __version__
from app import downloader as dl
from app.formatting import format_size
from app.resources import tool_path


def _ffmpeg_version():
    p = tool_path("ffmpeg")
    if not p:
        return "not found"
    try:
        out = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=10).stdout.splitlines()
        return out[0].replace("ffmpeg version ", "")[:60] if out else "unknown"
    except Exception:
        return "unavailable"


class SettingsPage(QWidget):
    def __init__(self, settings, thumbs=None, manager=None, parent=None):
        super().__init__(parent)
        self.s, self.thumbs, self.mgr = settings, thumbs, manager
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(14)
        title = QLabel("Settings")
        title.setObjectName("title")
        lay.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)
        self.folder = QLineEdit(self._folder())
        self.folder.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder.text())))
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        row.addWidget(open_btn)
        form.addRow("Download folder", row)

        self.quality = QComboBox()
        for q in (360, 480, 720, 1080):
            self.quality.addItem(f"{q}p", q)
        self.quality.setCurrentIndex(max(0, self.quality.findData(settings.get("max_quality"))))
        self.quality.currentIndexChanged.connect(lambda: settings.set("max_quality", self.quality.currentData()))
        form.addRow("Maximum quality", self.quality)

        self.fmt = QComboBox()
        self.fmt.addItems(["mp4", "mkv"])
        self.fmt.setCurrentText(settings.get("format"))
        self.fmt.currentTextChanged.connect(lambda v: settings.set("format", v))
        form.addRow("Output format", self.fmt)

        self.parallel = QSpinBox()
        self.parallel.setRange(1, 10)
        self.parallel.setValue(settings.get("parallel_downloads"))
        self.parallel.valueChanged.connect(self._parallel)
        form.addRow("Parallel downloads (1–10)", self.parallel)

        self.retries = QSpinBox()
        self.retries.setRange(0, 10)
        self.retries.setValue(settings.get("retry_attempts"))
        self.retries.valueChanged.connect(lambda v: settings.set("retry_attempts", v))
        form.addRow("Retry attempts", self.retries)

        self.template = QLineEdit(settings.get("filename_template"))
        self.template.editingFinished.connect(self._template)
        form.addRow("Filename template", self.template)
        self.template_msg = QLabel("")
        form.addRow("", self.template_msg)
        lay.addLayout(form)

        self.storage = QLabel("")
        self.storage.setObjectName("muted")
        clear = QPushButton("Clear Thumbnail Cache")
        clear.clicked.connect(self._clear_cache)
        srow = QHBoxLayout()
        srow.addWidget(self.storage, 1)
        srow.addWidget(clear)
        lay.addLayout(srow)

        about = QLabel(f"YouTube Downloader Studio {__version__}   •   Python {platform.python_version()}   •   "
                       f"yt-dlp {self._ytdlp()}   •   FFmpeg {_ffmpeg_version()}")
        about.setObjectName("muted")
        about.setWordWrap(True)
        lay.addWidget(about)
        lay.addStretch(1)
        self.refresh_storage()

    @staticmethod
    def _ytdlp():
        try:
            import yt_dlp
            return yt_dlp.version.__version__
        except Exception:
            return "not installed"

    def _folder(self):
        return self.s.get("download_dir") or (self.mgr.default_dir if self.mgr else "")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose download folder", self.folder.text())
        if d:
            self.s.set("download_dir", d)
            self.folder.setText(d)

    def _parallel(self, v):
        self.s.set("parallel_downloads", v)
        if self.mgr:
            self.mgr._pump()          # a higher limit starts waiting downloads immediately

    def _template(self):
        try:
            self.s.set("filename_template", dl.validate_template(self.template.text()))
            self.template_msg.setText("")
        except ValueError as exc:
            self.template_msg.setText(str(exc))
            self.template_msg.setStyleSheet("color: #EF4444;")
            self.template.setText(self.s.get("filename_template"))

    def refresh_storage(self):
        if self.thumbs:
            self.storage.setText(f"Thumbnail cache: {format_size(self.thumbs.disk_usage())}")

    def _clear_cache(self):
        if self.thumbs:
            self.thumbs.clear()
            self.refresh_storage()
