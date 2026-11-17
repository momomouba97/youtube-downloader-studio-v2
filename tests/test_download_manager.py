import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
import pytest
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from app import downloader as dl
from app.database import Database
from app.download_manager import COMPLETED, FAILED, CANCELLED, PAUSED, WAITING, DownloadManager
from app.settings_manager import SettingsManager
from tests.test_video_manager import make_videos


def wait_until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return cond()


class Env:
    def __init__(self, tmp, fn, **kw):
        self.db = Database(tmp / "m.db")
        self.db.upsert_channel("UC1", "Chan/Name", "https://www.youtube.com/@chan/videos")
        self.db.upsert_videos("UC1", [v for v in make_videos(40) if v["availability"] == "public"])
        self.settings = SettingsManager(tmp / "s.json")
        self.settings.set("download_dir", str(tmp / "dl"))
        self.mgr = DownloadManager(self.db, self.settings, tmp / "dl", download_fn=fn, ffmpeg_dir=None,
                                   backoff=(0.01,), **kw)

    def rows(self, n):
        return self.db.query_videos("UC1", limit=n)

    def vids(self, a, b):
        return [self.db.get_video(f"vid{i:08d}") for i in range(a, b)]

    def close(self):
        self.mgr.shutdown()
        self.db.close()


def ok_fn(url, out_dir, **kw):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, url.rsplit("=", 1)[-1] + ".mp4")
    open(p, "wb").write(b"x" * 1000)
    kw["on_progress"]({"status": "downloading", "percent": 50.0, "speed": 100, "eta": 1, "downloaded": 500, "total": 1000})
    return {"file_path": p, "size": 1000, "height": 1080}


@pytest.fixture
def env(tmp_path, qapp):
    e = Env(tmp_path, ok_fn)
    yield e
    e.close()


def test_downloads_selected_only_and_persists(env):
    rows = env.rows(5)
    env.db.set_selected([r["video_id"] for r in rows[:3]], True)
    sel = env.db.selected_videos()
    assert len(sel) == 3
    q, s = env.mgr.enqueue(sel)
    assert (q, s) == (3, 0)
    assert wait_until(lambda: env.mgr.counts()[COMPLETED] == 3)
    assert len(env.db.list_downloads("completed")) == 3
    done_ids = {r["video_id"] for r in env.db.list_downloads("completed")}
    assert done_ids == {r["video_id"] for r in sel}
    v = env.db.get_video(sel[0]["video_id"])
    assert v["download_status"] == "completed" and os.path.isfile(v["file_path"])
    assert "Chan_Name" in v["file_path"]            # channel folder name is sanitised
    assert env.db.get_video(rows[4]["video_id"])["download_status"] == "none"


def test_parallel_limit_never_exceeded(tmp_path, qapp):
    lock, state = threading.Lock(), {"cur": 0, "max": 0}

    def slow(url, out_dir, **kw):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.15)
        with lock:
            state["cur"] -= 1
        return ok_fn(url, out_dir, **kw)

    e = Env(tmp_path, slow)
    try:
        e.settings.set("parallel_downloads", 3)
        e.mgr.enqueue(e.rows(12))
        assert wait_until(lambda: e.mgr.counts()[COMPLETED] == 12, 20)
        assert state["max"] <= 3 and state["max"] >= 2
    finally:
        e.close()


def test_ten_parallel_is_the_ceiling(tmp_path, qapp):
    lock, state = threading.Lock(), {"cur": 0, "max": 0}

    def slow(url, out_dir, **kw):
        with lock:
            state["cur"] += 1; state["max"] = max(state["max"], state["cur"])
        time.sleep(0.2)
        with lock:
            state["cur"] -= 1
        return ok_fn(url, out_dir, **kw)

    e = Env(tmp_path, slow)
    try:
        e.settings.set("parallel_downloads", 99)       # clamped to 10 by settings
        assert e.mgr.parallel() == 10
        e.mgr.enqueue(e.rows(30))
        assert wait_until(lambda: e.mgr.counts()[COMPLETED] == 30, 30)
        assert state["max"] <= 10
    finally:
        e.close()


def test_one_failure_does_not_stop_others(tmp_path, qapp):
    def fn(url, out_dir, **kw):
        if url.endswith(("vid00000001", "vid00000003")):
            raise dl.DownloadError("This video is private.", retryable=False)
        return ok_fn(url, out_dir, **kw)

    e = Env(tmp_path, fn)
    try:
        e.mgr.enqueue(e.vids(0, 8))
        assert wait_until(lambda: e.mgr.counts()["in_progress"] == 0)
        c = e.mgr.counts()
        assert c[COMPLETED] == 6 and c[FAILED] == 2
        failed = e.db.list_downloads("failed")
        assert len(failed) == 2 and failed[0]["error"] == "This video is private."
        assert e.db.get_video("vid00000001")["download_status"] == "failed"
    finally:
        e.close()


def test_retries_then_succeeds_and_gives_up(tmp_path, qapp):
    calls = {}

    def fn(url, out_dir, **kw):
        n = calls[url] = calls.get(url, 0) + 1
        if url.endswith("vid00000000") and n < 3:
            raise dl.DownloadError("HTTP Error 503", retryable=True)
        if url.endswith("vid00000002"):
            raise dl.DownloadError("HTTP Error 503", retryable=True)
        return ok_fn(url, out_dir, **kw)

    e = Env(tmp_path, fn)
    try:
        e.settings.set("retry_attempts", 3)
        e.mgr.enqueue([e.db.get_video("vid00000000"), e.db.get_video("vid00000002")])
        assert wait_until(lambda: e.mgr.counts()["in_progress"] == 0)
        a, b = e.mgr.tasks["vid00000000"], e.mgr.tasks["vid00000002"]
        assert a["status"] == COMPLETED and a["attempts"] == 3
        assert b["status"] == FAILED and b["attempts"] == 4          # 1 try + 3 retries
        assert calls[[u for u in calls if u.endswith("vid00000002")][0]] == 4
    finally:
        e.close()


def test_non_retryable_error_is_not_retried(tmp_path, qapp):
    n = {"c": 0}

    def fn(url, out_dir, **kw):
        n["c"] += 1
        raise dl.DownloadError("Private", retryable=False)

    e = Env(tmp_path, fn)
    try:
        e.mgr.enqueue(e.rows(1))
        assert wait_until(lambda: e.mgr.counts()[FAILED] == 1)
        assert n["c"] == 1
    finally:
        e.close()


def test_unexpected_exception_is_contained(tmp_path, qapp):
    def fn(url, out_dir, **kw):
        if url.endswith("vid00000000"):
            raise RuntimeError("boom")
        return ok_fn(url, out_dir, **kw)

    e = Env(tmp_path, fn)
    try:
        e.settings.set("retry_attempts", 0)
        e.mgr.enqueue(e.vids(0, 3))
        assert wait_until(lambda: e.mgr.counts()["in_progress"] == 0)
        assert e.mgr.counts()[FAILED] == 1 and e.mgr.counts()[COMPLETED] == 2
    finally:
        e.close()


def test_pause_resume_cancel(tmp_path, qapp):
    gate = threading.Event()

    def fn(url, out_dir, should_abort=None, **kw):
        end = time.monotonic() + 5
        while not gate.is_set() and time.monotonic() < end:
            r = should_abort()
            if r:
                raise dl.DownloadAborted(r)
            time.sleep(0.01)
        return ok_fn(url, out_dir, should_abort=should_abort, **kw)

    e = Env(tmp_path, fn)
    try:
        e.settings.set("parallel_downloads", 2)
        e.mgr.enqueue(e.vids(0, 5))
        assert wait_until(lambda: e.mgr.counts()["active"] == 2)
        e.mgr.pause_all()
        assert wait_until(lambda: e.mgr.counts()[PAUSED] == 5)       # 2 running aborted + 3 held
        assert e.mgr.counts()["active"] == 0
        gate.set()
        e.mgr.resume_all()
        assert wait_until(lambda: e.mgr.counts()[COMPLETED] == 5)

        gate.clear()
        e.mgr.enqueue(e.vids(5, 9))                                   # 4 new
        assert wait_until(lambda: e.mgr.counts()["active"] == 2)
        e.mgr.cancel_all()
        assert wait_until(lambda: e.mgr.counts()[CANCELLED] == 4)
        assert e.mgr.counts()["in_progress"] == 0
        assert e.db.list_downloads("failed") == []                    # cancelling is not a failure
        assert e.mgr.retry("vid00000007") is True
        gate.set()
        assert wait_until(lambda: e.mgr.tasks["vid00000007"]["status"] == COMPLETED)
    finally:
        gate.set()
        e.close()


def test_duplicates_and_already_downloaded_are_skipped(env):
    rows = env.rows(3)
    assert env.mgr.enqueue(rows) == (3, 0)
    assert env.mgr.enqueue(rows)[1] >= 0
    assert wait_until(lambda: env.mgr.counts()[COMPLETED] == 3)
    assert env.mgr.enqueue(rows) == (0, 3)                            # file exists -> skipped
    os.remove(env.mgr.tasks[rows[0]["video_id"]]["file_path"])
    assert env.mgr.enqueue(rows[:1]) == (1, 0)                        # file gone -> downloads again


def test_history_loaded_after_restart_and_remove_keeps_file(tmp_path, qapp):
    e = Env(tmp_path, ok_fn)
    e.mgr.enqueue(e.rows(2))
    assert wait_until(lambda: e.mgr.counts()[COMPLETED] == 2)
    path = e.mgr.tasks[e.rows(1)[0]["video_id"]]["file_path"]
    e.mgr.shutdown()
    mgr2 = DownloadManager(e.db, e.settings, tmp_path / "dl", download_fn=ok_fn, ffmpeg_dir=None)
    mgr2.load_history()
    assert mgr2.counts()[COMPLETED] == 2 and mgr2.summary()["total"] == 0   # history is not "this session"
    vid = e.rows(1)[0]["video_id"]
    assert mgr2.remove(vid) is True
    assert os.path.isfile(path)                                           # real file untouched
    assert e.db.get_video(vid)["download_status"] == "none"
    assert mgr2.clear_completed() == 1
    e.close()


def test_low_disk_space_blocks_queueing(tmp_path, qapp):
    e = Env(tmp_path, ok_fn, min_free_bytes=10 ** 18)
    msgs = []
    e.mgr.message.connect(lambda lvl, t: msgs.append((lvl, t)))
    try:
        assert e.mgr.enqueue(e.rows(3)) == (0, 3)
        assert msgs and msgs[0][0] == "error" and "disk space" in msgs[0][1]
        assert e.mgr.counts()["in_progress"] == 0
    finally:
        e.close()


def test_summary_overall_progress(tmp_path, qapp):
    gate = threading.Event()

    def fn(url, out_dir, **kw):
        kw["on_progress"]({"status": "downloading", "percent": 50.0, "speed": 1, "eta": 1, "downloaded": 1, "total": 2})
        gate.wait(5)
        return ok_fn(url, out_dir, **kw)

    e = Env(tmp_path, fn)
    try:
        e.settings.set("parallel_downloads", 2)
        e.mgr.enqueue(e.rows(2))
        assert wait_until(lambda: all(t["percent"] == 50.0 for t in e.mgr.tasks.values()))
        s = e.mgr.summary()
        assert s["total"] == 2 and s["remaining"] == 2 and 49 < s["percent"] < 51
        gate.set()
        assert wait_until(lambda: e.mgr.summary()["completed"] == 2)
        assert e.mgr.summary()["percent"] == 100.0
    finally:
        gate.set()
        e.close()
