"""`--selftest`: verifies that a frozen build really contains everything it needs."""
import os
import platform
import subprocess
import sys
import tempfile


def run(require_ffmpeg=False) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    tmp = tempfile.mkdtemp(prefix="ytds-selftest-")
    os.environ["YTDS_DATA_DIR"] = tmp
    failures = []

    def step(name, fn):
        try:
            print(f"[ok]   {name}: {fn()}")
        except Exception as exc:
            print(f"[FAIL] {name}: {exc!r}")
            failures.append(name)

    def versions():
        import PySide6
        import sqlite3
        import yt_dlp
        return (f"python {platform.python_version()} {platform.machine()} | PySide6 {PySide6.__version__} | "
                f"yt-dlp {yt_dlp.version.__version__} | sqlite {sqlite3.sqlite_version}")

    def extractors():
        import yt_dlp.extractor.youtube  # noqa: F401  (must be inside the bundle)
        import certifi, os as _os
        assert _os.path.isfile(certifi.where()), "certifi CA bundle missing"
        return "youtube extractor + CA bundle present"

    def tool(name):
        from app.resources import tool_path
        p = tool_path(name)
        if not p:
            if require_ffmpeg:
                raise RuntimeError(f"{name} not bundled")
            return f"{name} not bundled (allowed)"
        out = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"{name} failed to run: {out.stderr[:200]}")
        return out.stdout.splitlines()[0]

    def download():
        import functools, http.server, threading
        from app.downloader import download_video
        from app.resources import tool_path
        ff = tool_path("ffmpeg")
        if not ff:
            if require_ffmpeg:
                raise RuntimeError("ffmpeg not bundled")
            return "skipped (no ffmpeg)"
        srv_dir = os.path.join(tmp, "srv")
        os.makedirs(srv_dir)
        clip = os.path.join(srv_dir, "clip.mp4")
        for codec in ("libx264", "mpeg4"):
            r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x180:rate=15",
                                "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-pix_fmt", "yuv420p",
                                "-c:v", codec, "-c:a", "aac", "-shortest", clip], capture_output=True, timeout=60)
            if r.returncode == 0:
                break
        else:
            raise RuntimeError("could not generate a test video with the bundled ffmpeg")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=srv_dir)
        handler.func.log_message = lambda *a, **k: None
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            res = download_video(f"http://127.0.0.1:{httpd.server_address[1]}/clip.mp4", os.path.join(tmp, "out"),
                                 template="%(id)s.%(ext)s", allow_any_host=True,
                                 ffmpeg_dir=os.path.dirname(ff))
        finally:
            httpd.shutdown()
        assert res["size"] > 0 and os.path.isfile(res["file_path"])
        return f"downloaded {res['size']} bytes through yt-dlp"

    def gui():
        from PySide6.QtWidgets import QApplication
        from app.database import Database
        from app.main_window import MainWindow
        from app.settings_manager import SettingsManager
        from app.thumbnail_manager import ThumbnailManager
        app = QApplication.instance() or QApplication([])
        db = Database(os.path.join(tmp, "selftest.db"))
        win = MainWindow(db, SettingsManager(os.path.join(tmp, "s.json")),
                         ThumbnailManager(os.path.join(tmp, "thumbs")))
        win.show()
        app.processEvents()
        win.close()
        db.close()
        return "main window created and closed"

    step("versions", versions)
    step("yt-dlp bundle", extractors)
    step("ffmpeg", lambda: tool("ffmpeg"))
    step("ffprobe", lambda: tool("ffprobe"))
    step("download (yt-dlp, local server)", download)
    step("gui (offscreen)", gui)
    print("SELFTEST FAILED: " + ", ".join(failures) if failures else "SELFTEST OK")
    return 1 if failures else 0
