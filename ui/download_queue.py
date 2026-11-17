"""Downloads / Completed / Failed pages: Model/View lists, live progress rows, context menus."""
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListView, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QStyledItemDelegate, QStyle, QVBoxLayout, QWidget)
from PySide6.QtCore import QFile

from app.download_manager import (ACTIVE, CANCELLED, COMPLETED, DOWNLOADING, FAILED, IN_PROGRESS, MERGING,
                                  PAUSED, PREPARING, WAITING)
from app.formatting import format_datetime, format_eta, format_size, format_speed
from ui.styles import palette

ROLE_TASK = Qt.UserRole + 1
ROW_HEIGHT = 84


def show_in_finder(path):
    if not path or not os.path.exists(path):
        return False
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])          # argument list: no shell, no injection
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
    return True


def open_file(path):
    return bool(path and os.path.exists(path) and QDesktopServices.openUrl(QUrl.fromLocalFile(path)))


class TaskModel(QAbstractListModel):
    """Shows the manager's tasks whose status is in `statuses`."""

    def __init__(self, manager, statuses, newest_first=False, parent=None):
        super().__init__(parent)
        self.m, self.statuses, self.newest_first = manager, set(statuses), newest_first
        self._rows, self._pos = [], {}
        manager.taskAdded.connect(self._changed)
        manager.taskChanged.connect(self._changed)
        manager.taskRemoved.connect(self._changed)
        self.reload()

    def _reindex(self):
        self._pos = {v: i for i, v in enumerate(self._rows)}

    def reload(self):
        self.beginResetModel()
        ids = [v for v, t in self.m.tasks.items() if t["status"] in self.statuses]
        if self.newest_first:
            ids.sort(key=lambda v: self.m.tasks[v]["finished_at"], reverse=True)
        self._rows = ids
        self._reindex()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        t = self.m.tasks.get(self._rows[index.row()])
        if t is None:
            return None
        if role == Qt.DisplayRole:
            return t["title"]
        if role == ROLE_TASK:
            return t
        return None

    def _changed(self, vid):
        t = self.m.tasks.get(vid)
        want = bool(t and t["status"] in self.statuses)
        pos = self._pos.get(vid)
        if want and pos is not None:
            ix = self.index(pos)
            self.dataChanged.emit(ix, ix)
        elif want:
            row = 0 if self.newest_first else len(self._rows)
            self.beginInsertRows(QModelIndex(), row, row)
            self._rows.insert(row, vid)
            self._reindex()
            self.endInsertRows()
        elif pos is not None:
            self.beginRemoveRows(QModelIndex(), pos, pos)
            del self._rows[pos]
            self._reindex()
            self.endRemoveRows()

    def thumbnail_ready(self, vid):
        pos = self._pos.get(vid)
        if pos is not None:
            ix = self.index(pos)
            self.dataChanged.emit(ix, ix)


class TaskDelegate(QStyledItemDelegate):
    THUMB = QSize(112, 63)

    def __init__(self, thumbs, theme="dark", accent="#E63946", parent=None):
        super().__init__(parent)
        self.thumbs = thumbs
        self.set_theme(theme, accent)

    def set_theme(self, theme, accent):
        self.c = palette(theme, accent)

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_HEIGHT)

    def _pill(self, t):
        c, st = self.c, t["status"]
        return {WAITING: ("Waiting", c["text2"]), PREPARING: ("Preparing", c["warning"]),
                DOWNLOADING: (f"Downloading {t['percent']:.0f}%", c["accent2"]),
                MERGING: ("Merging", c["accent2"]), PAUSED: ("Paused", c["warning"]),
                COMPLETED: ("Completed", c["success"]), FAILED: ("Failed", c["error"]),
                CANCELLED: ("Cancelled", c["text2"])}[st]

    def paint(self, p, option, index):
        t = index.data(ROLE_TASK)
        if not t:
            return
        c, r = self.c, option.rect
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(r, QColor(c["card_hover"] if option.state & QStyle.State_MouseOver else c["card"]))
        p.setPen(QPen(QColor(c["border"])))
        p.drawLine(r.left() + 12, r.bottom(), r.right() - 12, r.bottom())
        base_font = p.font()
        fm = QFontMetrics(base_font)

        tr = QRect(r.left() + 16, r.top() + (r.height() - self.THUMB.height()) // 2,
                   self.THUMB.width(), self.THUMB.height())
        pm = self.thumbs.get(t["video_id"], t["thumbnail"]) if self.thumbs else None
        if pm is not None and not pm.isNull():
            sc = pm.scaled(tr.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.setClipRect(tr)
            p.drawPixmap(tr.left() - (sc.width() - tr.width()) // 2, tr.top() - (sc.height() - tr.height()) // 2, sc)
            p.setClipping(False)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(c["secondary"]))
            p.drawRoundedRect(tr, 6, 6)

        label, color = self._pill(t)
        pw = fm.horizontalAdvance(label) + 22
        pill = QRect(r.right() - pw - 16, r.top() + 14, pw, 22)
        pc = QColor(color)
        pc.setAlpha(40)
        p.setPen(Qt.NoPen)
        p.setBrush(pc)
        p.drawRoundedRect(pill, 11, 11)
        p.setPen(QColor(color))
        p.drawText(pill, Qt.AlignCenter, label)

        tx = tr.right() + 16
        tw = max(pill.left() - tx - 16, 60)
        bold = p.font()
        bold.setBold(True)
        if bold.pixelSize() > 0:
            bold.setPixelSize(bold.pixelSize() + 1)
        p.setFont(bold)
        p.setPen(QColor(c["text"]))
        p.drawText(QRect(tx, r.top() + 10, tw, 22), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(bold).elidedText(t["title"], Qt.ElideRight, tw))
        p.setFont(base_font)

        line2, line3, line2_color = self._lines(t)
        p.setPen(QColor(line2_color or c["text2"]))
        width2 = r.right() - tx - 16
        p.drawText(QRect(tx, r.top() + 34, width2, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(line2, Qt.ElideMiddle, width2))
        if t["status"] in ACTIVE or t["status"] == PAUSED:
            bar = QRect(tx, r.top() + 60, width2, 6)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(c["secondary"]))
            p.drawRoundedRect(bar, 3, 3)
            frac = max(0.0, min(1.0, t["percent"] / 100.0))
            if frac > 0:
                fill = QRect(bar.left(), bar.top(), max(6, int(bar.width() * frac)), bar.height())
                p.setBrush(QColor(c["accent"]))
                p.drawRoundedRect(fill, 3, 3)
        elif line3:
            p.setPen(QColor(c["text2"]))
            p.drawText(QRect(tx, r.top() + 56, width2, 18), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(line3, Qt.ElideMiddle, width2))
        p.restore()

    def _lines(self, t):
        st = t["status"]
        if st in ACTIVE:
            if t.get("note"):
                return t["note"], "", self.c["warning"]
            size = format_size(t["downloaded"]) + (f" / {format_size(t['total'])}" if t["total"] else "")
            return f"{format_speed(t['speed'])}   •   {size}   •   ETA {format_eta(t['eta'])}", "", None
        if st == COMPLETED:
            meta = " • ".join(x for x in (f"{t['height']}p" if t["height"] else "", format_size(t["size"]),
                                          format_datetime(t["finished_at"])) if x)
            return meta, t["file_path"], None
        if st == FAILED:
            return t["error"] or "Unknown error", f"Attempts: {t['attempts']}   •   {format_datetime(t['finished_at'])}", self.c["error"]
        if st == PAUSED:
            return "Paused — will continue where it stopped", "", None
        if st == CANCELLED:
            return "Cancelled", "", None
        return "Waiting in queue", "", None


class TaskView(QListView):
    def __init__(self, manager, model, delegate, parent=None):
        super().__init__(parent)
        self.mgr = manager
        self.setModel(model)
        self.setItemDelegate(delegate)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSelectionMode(QListView.NoSelection)
        self.setFrameShape(QListView.NoFrame)

    def build_menu(self, vid):
        t = self.mgr.tasks.get(vid)
        if not t:
            return None
        st, path = t["status"], t["file_path"]
        menu = QMenu(self)
        if st in IN_PROGRESS:
            menu.addAction("Cancel", lambda: self.mgr.cancel(vid))
        if st in (FAILED, CANCELLED):
            menu.addAction("Retry", lambda: self.mgr.retry(vid))
        if st == COMPLETED and path and os.path.exists(path):
            menu.addAction("Open File", lambda: open_file(path))
            menu.addAction("Show in Finder", lambda: show_in_finder(path))
            menu.addAction("Copy Path", lambda: QApplication.clipboard().setText(path))
        menu.addAction("Copy Video URL", lambda: QApplication.clipboard().setText(t["url"]))
        if st not in IN_PROGRESS:
            menu.addSeparator()
            menu.addAction("Remove from List (keeps the file)", lambda: self.mgr.remove(vid))
            if st == COMPLETED and path and os.path.exists(path):
                menu.addAction("Move File to Trash…", lambda: self._trash(vid, path))
        return menu

    def _trash(self, vid, path):
        ans = QMessageBox.question(self, "Move to Trash", f"Move this file to the Trash?\n\n{path}")
        if ans == QMessageBox.Yes:
            if QFile.moveToTrash(path):
                self.mgr.remove(vid)
            else:
                QMessageBox.warning(self, "Move to Trash", "The file could not be moved to the Trash.")

    def contextMenuEvent(self, ev):
        idx = self.indexAt(ev.pos())
        if idx.isValid():
            menu = self.build_menu(idx.data(ROLE_TASK)["video_id"])
            if menu:
                menu.exec(ev.globalPos())


def _empty_label(text):
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setAlignment(Qt.AlignCenter)
    lab.setWordWrap(True)
    return lab


class _ListPage(QWidget):
    """Common structure: title, button bar, list with an empty-state label."""

    def __init__(self, manager, thumbs, statuses, title, empty_text, newest_first, theme, accent, parent=None):
        super().__init__(parent)
        self.mgr = manager
        self.model = TaskModel(manager, statuses, newest_first)
        self.delegate = TaskDelegate(thumbs, theme, accent)
        self.view = TaskView(manager, self.model, self.delegate)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(20, 16, 20, 16)
        self.lay.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("title")
        self.lay.addWidget(t)
        self.extra_top = QVBoxLayout()
        self.lay.addLayout(self.extra_top)
        self.bar = QHBoxLayout()
        self.lay.addLayout(self.bar)
        self.empty = _empty_label(empty_text)
        self.lay.addWidget(self.empty, 1)
        self.lay.addWidget(self.view, 1)
        for sig in (self.model.rowsInserted, self.model.rowsRemoved, self.model.modelReset):
            sig.connect(self._toggle_empty)
        self._toggle_empty()

    def _toggle_empty(self, *a):
        has = self.model.rowCount() > 0
        self.view.setVisible(has)
        self.empty.setVisible(not has)

    def button(self, text, fn, primary=False):
        b = QPushButton(text)
        if primary:
            b.setObjectName("primary")
        b.clicked.connect(fn)
        self.bar.addWidget(b)
        return b


class DownloadsPage(_ListPage):
    def __init__(self, manager, thumbs, theme="dark", accent="#E63946", parent=None):
        super().__init__(manager, thumbs, IN_PROGRESS | {CANCELLED}, "Downloads",
                         "No active downloads.\nSelect videos in the Library and click “Download Selected”.",
                         False, theme, accent, parent)
        self.stats = QLabel("")
        self.stats.setObjectName("muted")
        self.overall = QProgressBar()
        self.overall.setRange(0, 1000)
        self.overall.setTextVisible(False)
        self.extra_top.addWidget(self.stats)
        self.extra_top.addWidget(self.overall)
        self.pause_btn = self.button("Pause All", manager.pause_all)
        self.resume_btn = self.button("Resume All", manager.resume_all)
        self.button("Cancel All", self._cancel_all)
        self.button("Retry Failed", manager.retry_failed)
        self.button("Clear Cancelled", lambda: manager.clear({CANCELLED}))
        self.bar.addStretch(1)
        manager.countsChanged.connect(self.refresh_stats)
        manager.taskChanged.connect(lambda v: self.refresh_stats())
        self.refresh_stats()

    def _cancel_all(self):
        if self.mgr.counts()["in_progress"] and QMessageBox.question(
                self, "Cancel all", "Cancel all downloads in the queue?\nPartial files are kept.") == QMessageBox.Yes:
            self.mgr.cancel_all()

    def refresh_stats(self):
        s, c = self.mgr.summary(), self.mgr.counts()
        self.stats.setText(f"Total: {s['total']}   •   Downloaded: {s['completed']}   •   Failed: {s['failed']}   •   "
                           f"Remaining: {s['remaining']}   •   Active: {c['active']}"
                           + ("   •   PAUSED" if self.mgr.is_paused() else ""))
        self.overall.setValue(int(s["percent"] * 10))
        self.pause_btn.setEnabled(not self.mgr.is_paused() and c["in_progress"] > 0)
        self.resume_btn.setEnabled(self.mgr.is_paused())


class CompletedPage(_ListPage):
    zipRequested = Signal()

    def __init__(self, manager, thumbs, theme="dark", accent="#E63946", parent=None):
        super().__init__(manager, thumbs, {COMPLETED}, "Completed",
                         "Nothing downloaded yet.", True, theme, accent, parent)
        self.button("Create ZIP Archive…", self.zipRequested.emit, primary=True)
        self.button("Open Download Folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(manager.download_dir())))
        self.button("Clear Completed", self._clear)
        self.bar.addStretch(1)

    def _clear(self):
        n = self.mgr.counts()[COMPLETED]
        if n and QMessageBox.question(self, "Clear Completed",
                                      f"Remove {n} item(s) from this list?\nThe downloaded files are NOT deleted.") == QMessageBox.Yes:
            self.mgr.clear({COMPLETED})


class FailedPage(_ListPage):
    def __init__(self, manager, thumbs, theme="dark", accent="#E63946", parent=None):
        super().__init__(manager, thumbs, {FAILED}, "Failed Downloads",
                         "No failed downloads.", True, theme, accent, parent)
        self.button("Retry All Failed", manager.retry_failed, primary=True)
        self.button("Clear Failed", lambda: manager.clear({FAILED}))
        self.bar.addStretch(1)
