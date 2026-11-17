import os
import pytest
from pathlib import Path

from app import downloader as dl
from app.resources import tool_path
from tests.helpers import LocalServer, make_test_video

needs_ffmpeg = pytest.mark.skipif(not tool_path("ffmpeg"), reason="ffmpeg not available")


def test_format_selector_caps_quality_and_prefers_h264():
    s = dl.format_selector(1080, "mp4")
    assert "height<=1080" in s and "vcodec^=avc1" in s and "height<=2160" not in s
    assert dl.format_selector(720, "mkv").startswith("bv*[height<=720]")


@pytest.mark.parametrize("t,ok", [
    ("%(title)s [%(id)s].%(ext)s", True), ("%(id)s.%(ext)s", True),
    ("../%(title)s.%(ext)s", False), ("a/b.%(ext)s", False), ("noext", False), ("", False),
])
def test_template_validation(t, ok):
    if ok:
        assert dl.validate_template(t) == t
    else:
        with pytest.raises(ValueError):
            dl.validate_template(t)


def test_safe_dirname():
    assert dl.safe_dirname('a/b:c*"d') == "a_b_c__d"
    assert dl.safe_dirname("..") == "Channel" and dl.safe_dirname("") == "Channel"


def test_classify_error():
    assert dl.classify_error("ERROR: [youtube] x: Private video. Sign in")[1] is False
    assert dl.classify_error("Video unavailable")[1] is False
    assert dl.classify_error("ERROR: ffmpeg not found")[1] is False
    msg, retry = dl.classify_error("HTTP Error 503: Service Unavailable")
    assert retry is True and "503" in msg


def test_only_youtube_urls_by_default(tmp_path):
    for bad in ("https://evil.com/watch?v=x", "http://www.youtube.com/watch?v=x", "file:///etc/passwd"):
        with pytest.raises(dl.DownloadError) as e:
            dl.download_video(bad, tmp_path)
        assert e.value.retryable is False


def test_bad_template_is_not_retryable(tmp_path):
    with pytest.raises(dl.DownloadError) as e:
        dl.download_video("https://www.youtube.com/watch?v=aaaaaaaaaaa", tmp_path, template="../x.%(ext)s")
    assert e.value.retryable is False


@needs_ffmpeg
def test_real_ytdlp_download_progress_and_output(tmp_path):
    src = tmp_path / "srv"; src.mkdir()
    assert make_test_video(src / "clip.mp4")
    events = []
    with LocalServer(src) as srv:
        res = dl.download_video(srv.url("clip.mp4"), tmp_path / "out", template="%(id)s.%(ext)s",
                                max_quality=1080, fmt="mp4", on_progress=events.append, allow_any_host=True)
    assert os.path.isfile(res["file_path"]) and res["size"] > 0
    assert Path(res["file_path"]).parent == tmp_path / "out"
    assert events[0]["status"] == "preparing"
    assert any(e["status"] == "downloading" for e in events)
    assert all(0 <= e["percent"] <= 100 for e in events)


@needs_ffmpeg
def test_existing_file_is_not_downloaded_twice(tmp_path):
    src = tmp_path / "srv"; src.mkdir()
    make_test_video(src / "clip.mp4")
    with LocalServer(src) as srv:
        a = dl.download_video(srv.url("clip.mp4"), tmp_path / "o", template="%(id)s.%(ext)s", allow_any_host=True)
        mtime = os.path.getmtime(a["file_path"])
        b = dl.download_video(srv.url("clip.mp4"), tmp_path / "o", template="%(id)s.%(ext)s", allow_any_host=True)
    assert a["file_path"] == b["file_path"] and os.path.getmtime(b["file_path"]) == mtime


@needs_ffmpeg
@pytest.mark.parametrize("reason", ["cancel", "pause"])
def test_abort_mid_download(tmp_path, reason):
    src = tmp_path / "srv"; src.mkdir()
    (src / "big.mp4").write_bytes(os.urandom(3_000_000))   # content is irrelevant for the abort test
    flag = {"v": None}

    def on_progress(d):
        if d["status"] == "downloading" and d["downloaded"] > 0:
            flag["v"] = reason

    with LocalServer(src, slow=0.02) as srv:
        with pytest.raises(dl.DownloadAborted) as e:
            dl.download_video(srv.url("big.mp4"), tmp_path / "o", template="%(id)s.%(ext)s",
                              on_progress=on_progress, should_abort=lambda: flag["v"], allow_any_host=True)
    assert e.value.reason == reason


@needs_ffmpeg
def test_server_error_is_reported_as_retryable_error(tmp_path):
    src = tmp_path / "srv"; src.mkdir()
    with LocalServer(src) as srv:
        with pytest.raises(dl.DownloadError):
            dl.download_video(srv.url("missing.mp4"), tmp_path / "o", allow_any_host=True)
