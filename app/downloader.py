"""Download a single video with yt-dlp (+FFmpeg merge). No UI code; runs in worker threads."""
import errno
import os
import re
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


class DownloadAborted(Exception):
    """Raised inside a download when the user cancelled or paused it."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason  # "cancel" | "pause"


class DownloadError(Exception):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def format_selector(max_quality=1080, fmt="mp4"):
    """Best quality not above max_quality; prefers H.264 so MP4 plays in QuickTime on Big Sur."""
    h = int(max_quality)
    if fmt == "mp4":
        return (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]"
                f"/b[height<={h}][ext=mp4]"
                f"/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
                f"/bv*[height<={h}]+ba"
                f"/b[height<=?{h}]")
    return f"bv*[height<={h}]+ba/b[height<=?{h}]"


def validate_template(template):
    t = (template or "").strip()
    if "%(ext)s" not in t:
        raise ValueError("The filename template must contain %(ext)s.")
    if any(x in t for x in ("/", "\\", "..", "\x00")):
        raise ValueError("The filename template must not contain path separators or '..'.")
    return t


def safe_dirname(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return cleaned[:80] or "Channel"


def validate_video_url(url):
    p = urlparse(url or "")
    if p.scheme != "https" or (p.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise DownloadError("Only https://youtube.com video links are allowed.", retryable=False)


_NON_RETRYABLE = [
    ("private video", "This video is private."),
    ("video unavailable", "This video is unavailable."),
    ("not available", "This video is not available."),
    ("has been removed", "This video has been removed."),
    ("members-only", "Members-only video (login required)."),
    ("join this channel", "Members-only video (login required)."),
    ("sign in to confirm", "YouTube asks to sign in to confirm you are not a bot."),
    ("sign in to view", "Sign-in is required to view this video."),
    ("age-restricted", "Age-restricted video (login required)."),
    ("copyright", "Blocked for copyright reasons."),
    ("requested format is not available", "No format within the quality limit is available."),
    ("ffmpeg is not installed", "FFmpeg was not found, so audio/video cannot be merged."),
    ("ffmpeg not found", "FFmpeg was not found, so audio/video cannot be merged."),
    ("no space left", "No space left on the disk."),
    ("premieres in", "This video has not premiered yet."),
    ("live event will begin", "This live event has not started yet."),
]


def classify_error(message):
    """Return (short_message, retryable)."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", str(message)).replace("ERROR:", "").strip()
    low = clean.lower()
    for needle, friendly in _NON_RETRYABLE:
        if needle in low:
            return friendly, False
    return (clean[:300] or "Unknown error"), True


def _default_factory(opts):
    import yt_dlp  # lazy
    return yt_dlp.YoutubeDL(opts)


def download_video(url, out_dir, *, template=DEFAULT_TEMPLATE, max_quality=1080, fmt="mp4",
                   ffmpeg_dir=None, on_progress=None, should_abort=None,
                   ydl_factory=_default_factory, allow_any_host=False):
    """Download one video. Returns {file_path, size, height}.
    Raises DownloadAborted (cancel/pause) or DownloadError(message, retryable)."""
    if not allow_any_host:
        validate_video_url(url)
    try:
        template = validate_template(template)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    except ValueError as exc:
        raise DownloadError(str(exc), retryable=False) from exc
    except OSError as exc:
        raise DownloadError(f"Cannot create the download folder: {exc.strerror or exc}", retryable=False) from exc

    emit = on_progress or (lambda d: None)
    state = {"done": 0, "n": 1, "bytes_before": 0}

    def hook(d):
        reason = should_abort() if should_abort else None
        if reason:
            raise DownloadAborted(reason)
        info = d.get("info_dict") or {}
        rf = info.get("requested_formats")
        state["n"] = len(rf) if rf else 1
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            frac = (got / total) if total else 0.0
            overall_total = info.get("filesize") or info.get("filesize_approx") or (total if state["n"] == 1 else 0)
            emit({"status": "downloading",
                  "percent": min(99.0, (state["done"] + frac) / state["n"] * 100.0),
                  "speed": d.get("speed") or 0, "eta": d.get("eta"),
                  "downloaded": state["bytes_before"] + got, "total": overall_total})
        elif d.get("status") == "finished":
            state["done"] += 1
            state["bytes_before"] += d.get("downloaded_bytes") or d.get("total_bytes") or 0
            if state["done"] >= state["n"] and state["n"] > 1:
                emit({"status": "merging", "percent": 99.0, "speed": 0, "eta": None,
                      "downloaded": state["bytes_before"], "total": state["bytes_before"]})

    def pp_hook(d):
        reason = should_abort() if should_abort else None
        if reason == "cancel":
            raise DownloadAborted(reason)
        if d.get("status") == "started" and "merg" in str(d.get("postprocessor", "")).lower():
            emit({"status": "merging", "percent": 99.0, "speed": 0, "eta": None})

    opts = {
        "format": format_selector(max_quality, fmt),
        "outtmpl": os.path.join(str(out_dir), template),
        "merge_output_format": fmt,
        "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [hook], "postprocessor_hooks": [pp_hook],
        "retries": 5, "fragment_retries": 5, "socket_timeout": 30,
        "continuedl": True, "overwrites": False, "updatetime": False,
        "trim_file_name": 150, "ignoreerrors": False,
    }
    if ffmpeg_dir:
        opts["ffmpeg_location"] = str(ffmpeg_dir)

    emit({"status": "preparing", "percent": 0.0, "speed": 0, "eta": None})
    try:
        with ydl_factory(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise DownloadError("YouTube returned no data for this video.", retryable=True)
            path = None
            rd = info.get("requested_downloads") or []
            if rd and rd[0].get("filepath"):
                path = rd[0]["filepath"]
            if not path or not os.path.isfile(path):
                guess = ydl.prepare_filename(info)
                base = os.path.splitext(guess)[0]
                for cand in (guess, f"{base}.{fmt}", f"{base}.mkv", f"{base}.mp4"):
                    if os.path.isfile(cand):
                        path = cand
                        break
    except DownloadAborted:
        raise
    except DownloadError:
        raise
    except Exception as exc:
        reason = should_abort() if should_abort else None
        if reason:
            raise DownloadAborted(reason) from exc
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise DownloadError("No space left on the disk.", retryable=False) from exc
        msg, retryable = classify_error(exc)
        raise DownloadError(msg, retryable) from exc

    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise DownloadError("The downloaded file was not found on disk.", retryable=True)
    return {"file_path": path, "size": os.path.getsize(path), "height": int(info.get("height") or 0)}
