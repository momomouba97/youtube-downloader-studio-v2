"""Phase 1 preview window: import a channel and work with the paginated, selectable library.
(The full sidebar / dashboard / downloads UI arrives in later phases.)"""
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from app.channel_parser import InvalidChannelURL, normalize_channel_url
from app.download_manager import COMPLETED, FAILED, DownloadManager
from app.formatting import format_size
from app.video_manager import VideoManager, format_duration
from app.workers import ImportWorker, ZipWorker
from ui.download_queue import CompletedPage, DownloadsPage, FailedPage, show_in_finder
from ui.settings_page import SettingsPage
from ui.styles import build_qss
from ui.video_list import VideoListModel, VideoRowDelegate, make_video_view

SORTS = [("Date", "date"), ("Title", "title"), ("Duration", "duration")]
FILTERS = [("All Videos", "all"), ("Downloaded", "downloaded"), ("Not Downloaded", "not_downloaded"),
           ("Failed", "failed"), ("Available", "available"), ("Unavailable", "unavailable")]


PAGES = ["Library", "Downloads", "Completed", "Failed", "Settings"]


class MainWindow(QMainWindow):
    def __init__(self, db, settings, thumbs, download_fn=None, manager=None):
        super().__init__()
        self.db, self.settings, self.thumbs = db, settings, thumbs
        self.worker = None
        self.zip_worker = None
        base = Path(settings.path).parent
        self.exports_dir = base / "Exports" / "ZIP Files"
        self.manager = None
        self.dl_manager = manager or DownloadManager(db, settings, base / "Downloads", download_fn=download_fn)
        self.dl_manager.load_history()
        self.setWindowTitle("YouTube Downloader Studio")
        self.resize(1180, 780)
        self.manager = VideoManager(db, settings.get("page_size"))   # library paging/selection
        self.model = VideoListModel(self.manager)
        theme, accent = settings.get("theme"), settings.get("accent")
        self.delegate = VideoRowDelegate(thumbs, theme, accent)
        self.view = make_video_view(self.model, self.delegate)
        thumbs.thumbnailReady.connect(self.model.thumbnail_ready)
        self.model.selectionChanged.connect(self._update_labels)
        self._build_ui()
        self._build_pages(theme, accent)
        self._build_shortcuts()
        self.setStyleSheet(build_qss(theme, accent))
        self._reload_channels(settings.get("last_channel_id"))

    # ---- UI ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        title = QLabel("Video Library")
        title.setObjectName("title")
        lay.addWidget(title)

        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Channel URL or @handle  (e.g. https://www.youtube.com/@name)")
        self.url_edit.returnPressed.connect(self.start_import)
        self.import_btn = QPushButton("Import Channel")
        self.import_btn.setObjectName("primary")
        self.import_btn.clicked.connect(self.start_import)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(lambda: self.worker and self.worker.cancel())
        clear = QPushButton("Clear")
        clear.clicked.connect(self.url_edit.clear)
        for w in (self.url_edit, self.import_btn, self.cancel_btn, clear):
            row.addWidget(w, 1 if w is self.url_edit else 0)
        lay.addLayout(row)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        lay.addWidget(self.status)
        lay.addWidget(self.progress)

        tools = QHBoxLayout()
        self.channel_box = QComboBox()
        self.channel_box.currentIndexChanged.connect(self._channel_changed)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title or video ID…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search_changed)
        self.sort_box = QComboBox()
        for label, key in SORTS:
            self.sort_box.addItem(f"Sort: {label}", key)
        self.order_btn = QPushButton("Descending")
        self.order_btn.clicked.connect(self._toggle_order)
        self.filter_box = QComboBox()
        for label, key in FILTERS:
            self.filter_box.addItem(label, key)
        self.sort_box.currentIndexChanged.connect(self._sort_changed)
        self.filter_box.currentIndexChanged.connect(self._filter_changed)
        tools.addWidget(self.channel_box, 2)
        tools.addWidget(self.search, 3)
        tools.addWidget(self.sort_box)
        tools.addWidget(self.order_btn)
        tools.addWidget(self.filter_box)
        lay.addLayout(tools)

        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._library_menu)
        lay.addWidget(self.view, 1)

        bar = QFrame()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        self.sel_label = QLabel("")
        self.sel_label.setObjectName("muted")
        bl.addWidget(self.sel_label, 1)
        self.dl_btn = QPushButton("Download Selected")
        self.dl_btn.setObjectName("primary")
        self.dl_btn.clicked.connect(self.download_selected)
        bl.addWidget(self.dl_btn)
        for text, fn in (("Select Current Page", lambda: self._act(self.manager.select_current_page, True)),
                         ("Select Entire Channel", lambda: self._act(self.manager.select_entire_channel)),
                         ("Select All Matching", lambda: self._act(self.manager.select_matching)),
                         ("Deselect All", lambda: self._act(self.manager.deselect_all))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bl.addWidget(b)
        lay.addWidget(bar)

        pager = QHBoxLayout()
        self.prev_btn, self.next_btn = QPushButton("◀ Previous"), QPushButton("Next ▶")
        self.prev_btn.clicked.connect(lambda: self._page(-1))
        self.next_btn.clicked.connect(lambda: self._page(1))
        self.page_label = QLabel("")
        self.page_label.setAlignment(Qt.AlignCenter)
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.page_label, 1)
        pager.addWidget(self.next_btn)
        lay.addLayout(pager)
        self.library_page = root

    def _build_shortcuts(self):
        def add(seq, fn):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)
        add("Ctrl+I", self.url_edit.setFocus)          # Ctrl maps to Command on macOS
        add("Ctrl+F", self.search.setFocus)
        add("Ctrl+D", lambda: self._act(self.manager.deselect_all))
        add("Ctrl+,", lambda: self._goto(4))
        add("Ctrl+Return", self.download_selected)

    # ---- actions -------------------------------------------------------------
    def _act(self, fn, *args):
        fn(*args)
        self.model.reload()
        self._update_labels()

    def _page(self, delta):
        self.manager.goto(self.manager.page + delta)
        self.model.reload()
        self.view.scrollToTop()
        self._update_labels()

    def refresh(self):
        self.model.reload()
        self._update_labels()

    def _reload_channels(self, select_id=None):
        self.channel_box.blockSignals(True)
        self.channel_box.clear()
        self.channel_box.addItem("All channels", "")
        for ch in self.db.list_channels():
            self.channel_box.addItem(f"{ch['title']}  ({ch['video_count']})", ch["channel_id"])
        idx = self.channel_box.findData(select_id) if select_id else 0
        self.channel_box.setCurrentIndex(max(idx, 0))
        self.channel_box.blockSignals(False)
        self._channel_changed()

    def _channel_changed(self):
        cid = self.channel_box.currentData() or None
        self.manager.set_channel(cid)
        self.settings.set("last_channel_id", cid or "")
        self.refresh()

    def _search_changed(self, text):
        self.manager.set_search(text)
        self.refresh()

    def _sort_changed(self):
        self.manager.set_sort(self.sort_box.currentData())
        self.refresh()

    def _toggle_order(self):
        self.manager.set_sort(self.manager.sort, not self.manager.descending)
        self.order_btn.setText("Descending" if self.manager.descending else "Ascending")
        self.refresh()

    def _filter_changed(self):
        self.manager.set_filter(self.filter_box.currentData())
        self.refresh()

    def _update_labels(self):
        total, pages = self.manager.total(), self.manager.total_pages()
        self.page_label.setText(f"Page {self.manager.page + 1} of {pages}   •   {total:,} videos")
        self.prev_btn.setEnabled(self.manager.page > 0)
        self.next_btn.setEnabled(self.manager.page < pages - 1)
        s = self.manager.summary()
        self.sel_label.setText(f"Selected: {s['selected']:,}   •   On this page: {s['on_page']}   •   "
                               f"Total duration: {s['duration_text']}")
        self.dl_btn.setText(f"Download Selected ({s['selected']:,})" if s["selected"] else "Download Selected")
        self.dl_btn.setEnabled(s["selected"] > 0)
        self._update_sidebar()

    # ---- import ----------------------------------------------------------------
    def start_import(self):
        if self.worker and self.worker.isRunning():
            return
        try:
            normalize_channel_url(self.url_edit.text())
        except InvalidChannelURL as exc:
            QMessageBox.warning(self, "Invalid channel link", str(exc))
            return
        self.import_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.progress.setVisible(True)
        self.status.setText("Contacting YouTube…")
        self.worker = ImportWorker(self.url_edit.text(), self.db, parent=self)
        self.worker.progress.connect(lambda n: self.status.setText(f"Found {n:,} videos…"))
        self.worker.imported.connect(self._import_done)
        self.worker.failed.connect(lambda m: self._import_end(f"Import failed: {m}", True))
        self.worker.cancelled.connect(lambda: self._import_end("Import cancelled.", False))
        self.worker.start()

    def _import_done(self, channel_id, title, saved, skipped):
        note = f" ({skipped} non-video entries skipped)" if skipped else ""
        self._import_end(f"Imported “{title}”: {saved:,} videos{note}.", False)
        self._reload_channels(channel_id)

    def _import_end(self, message, is_error):
        self.status.setText(message)
        self.import_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.progress.setVisible(False)
        if is_error:
            QMessageBox.warning(self, "Import", message)


    # ---- pages / sidebar --------------------------------------------------------
    def _build_pages(self, theme, accent):
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(190)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 16, 12, 16)
        sl.setSpacing(4)
        brand = QLabel("YouTube\nDownloader Studio")
        brand.setObjectName("title")
        sl.addWidget(brand)
        sl.addSpacing(14)
        self.nav = []
        for i, name in enumerate(PAGES):
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, i=i: self._goto(i))
            sl.addWidget(b)
            self.nav.append(b)
        sl.addStretch(1)
        outer.addWidget(side)

        self.downloads_page = DownloadsPage(self.dl_manager, self.thumbs, theme, accent)
        self.completed_page = CompletedPage(self.dl_manager, self.thumbs, theme, accent)
        self.failed_page = FailedPage(self.dl_manager, self.thumbs, theme, accent)
        self.settings_page = SettingsPage(self.settings, self.thumbs, self.dl_manager)
        self.completed_page.zipRequested.connect(self.create_zip)
        self.stack = QStackedWidget()
        for w in (self.library_page, self.downloads_page, self.completed_page, self.failed_page, self.settings_page):
            self.stack.addWidget(w)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        m = self.dl_manager
        m.countsChanged.connect(self._update_sidebar)
        m.taskChanged.connect(self._task_changed)
        m.message.connect(lambda lvl, text: (self.status.setText(text),
                                             QMessageBox.warning(self, "Downloads", text) if lvl == "error" else None))
        for pg in (self.downloads_page, self.completed_page, self.failed_page):
            self.thumbs.thumbnailReady.connect(pg.model.thumbnail_ready)
        self._goto(0)

    def _goto(self, i):
        self.stack.setCurrentIndex(i)
        for j, b in enumerate(self.nav):
            b.setChecked(j == i)
        if i == 4:
            self.settings_page.refresh_storage()

    def _update_sidebar(self):
        c = self.dl_manager.counts()
        labels = [f"Library  ({self.manager.total():,})", f"Downloads  ({c['in_progress']})",
                  f"Completed  ({c[COMPLETED]})", f"Failed  ({c[FAILED]})", "Settings"]
        for b, t in zip(self.nav, labels):
            b.setText(t)

    def _task_changed(self, vid):
        t = self.dl_manager.tasks.get(vid)
        if t and t["status"] in (COMPLETED, FAILED, "cancelled"):
            self.model.refresh_row(vid)

    # ---- downloading ------------------------------------------------------------------
    def download_selected(self):
        rows = self.db.selected_videos()
        if not rows:
            QMessageBox.information(self, "Download Selected", "Select one or more videos first (tick the checkboxes).")
            return
        self._queue(rows)

    def _queue(self, rows):
        queued, skipped = self.dl_manager.enqueue(rows)
        note = f" ({skipped} skipped: already downloaded or already in the queue)" if skipped else ""
        if queued:
            self.status.setText(f"Queued {queued} video(s){note}.")
            self._goto(1)
        elif skipped:
            self.status.setText(f"Nothing new to download{note}.")
            QMessageBox.information(self, "Download Selected", f"Nothing new to download{note}.")

    def _library_menu(self, pos):
        ix = self.view.indexAt(pos)
        if not ix.isValid():
            return
        row = ix.data(Qt.UserRole + 1)
        menu = QMenu(self)
        menu.addAction("Download This Video", lambda: self._queue([row]))
        menu.addAction("Copy URL", lambda: QApplication.clipboard().setText(row["url"]))
        menu.addAction("Open Video Link", lambda: QDesktopServices.openUrl(QUrl(row["url"])))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    # ---- ZIP export -------------------------------------------------------------------------
    def create_zip(self):
        files = [t["file_path"] for t in self.dl_manager.tasks.values()
                 if t["status"] == COMPLETED and t["file_path"] and Path(t["file_path"]).is_file()]
        if not files:
            QMessageBox.information(self, "Create ZIP", "There are no completed downloads to export.")
            return
        if self.zip_worker and self.zip_worker.isRunning():
            return
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        default = str(self.exports_dir / f"Videos-{time.strftime('%Y%m%d-%H%M')}.zip")
        dest, _ = QFileDialog.getSaveFileName(self, "Create ZIP Archive", default, "ZIP archive (*.zip)")
        if dest:
            self.start_zip(files, dest)

    def start_zip(self, files, dest):
        self.zip_progress = QProgressDialog("Creating archive…", "Cancel", 0, 1000, self)
        self.zip_progress.setWindowTitle("Create ZIP Archive")
        self.zip_progress.setMinimumDuration(0)
        self.zip_progress.setWindowModality(Qt.WindowModal)
        self.zip_worker = ZipWorker(files, dest, parent=self)
        self.zip_progress.canceled.connect(self.zip_worker.cancel)
        self.zip_worker.progress.connect(lambda d, t, n: (self.zip_progress.setValue(int(d * 1000 / max(t, 1))),
                                                          self.zip_progress.setLabelText(f"Adding {n}…")))
        self.zip_worker.created.connect(self._zip_done)
        self.zip_worker.failed.connect(lambda m: (self.zip_progress.reset(), QMessageBox.critical(self, "Create ZIP", m)))
        self.zip_worker.cancelled.connect(lambda: (self.zip_progress.reset(), self.status.setText("ZIP export cancelled.")))
        self.zip_worker.start()

    def _zip_done(self, res):
        self.zip_progress.reset()
        self.db.log_activity("export", f"ZIP created: {res['path']} ({res['count']} videos)")
        extra = f"\n{len(res['skipped'])} missing file(s) were skipped." if res["skipped"] else ""
        box = QMessageBox(QMessageBox.Information, "ZIP created",
                          f"{res['count']} video(s) • {format_size(res['size'])}\n{res['path']}{extra}", parent=self)
        finder = box.addButton("Show in Finder", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is finder:
            show_in_finder(res["path"])

    def closeEvent(self, event):
        if self.dl_manager.has_active_work():
            ans = QMessageBox.question(self, "Downloads in progress",
                                       "Downloads are still running. Quit anyway?\n"
                                       "Partial files are kept and can be resumed later.")
            if ans != QMessageBox.Yes:
                event.ignore()
                return
        self.dl_manager.shutdown()
        if self.zip_worker and self.zip_worker.isRunning():
            self.zip_worker.cancel()
            self.zip_worker.wait(5000)
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        self.thumbs.shutdown()
        super().closeEvent(event)
