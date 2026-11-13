"""TubeGrab — YouTube downloader. Made by Jhames Martin."""
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from io import BytesIO

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for
import yt_dlp

app = Flask(__name__)

DOWNLOAD_ROOT = os.path.join(tempfile.gettempdir(), "tubegrab")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

MAX_DURATION = int(os.environ.get("MAX_DURATION_SECONDS", "3600"))
JOB_TTL = int(os.environ.get("JOB_TTL_SECONDS", "1800"))
AUDIO_BITRATES = ["320", "192", "128"]
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "3"))

# Cookies help get past YouTube's "confirm you're not a bot" check on cloud IPs.
# yt-dlp rewrites the cookie file as it runs, so always work from a writable copy
# (Render Secret Files under /etc/secrets are read-only).
COOKIES_FILE = None
_cookie_src = os.environ.get("YTDLP_COOKIES_FILE")
_cookie_text = os.environ.get("YTDLP_COOKIES")
if _cookie_src and os.path.exists(_cookie_src):
    with open(_cookie_src) as f:
        _cookie_text = f.read()
if _cookie_text:
    COOKIES_FILE = os.path.join(DOWNLOAD_ROOT, "cookies.txt")
    with open(COOKIES_FILE, "w") as f:
        # env vars pasted as one line sometimes arrive with literal "\n"
        f.write(_cookie_text.replace("\\n", "\n").strip() + "\n")

YT_REGEX = re.compile(
    r"^(https?://)?((www|m|music)\.)?(youtube\.com/(watch\?|shorts/|live/|embed/)|youtu\.be/)\S+", re.I
)

JOBS = {}
ACTIVE = ("queued", "preparing", "downloading", "processing")
JOBS_LOCK = threading.Lock()


class Cancelled(Exception):
    pass


def base_opts():
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "socket_timeout": 30, "retries": 3}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def valid_url(url):
    return bool(url) and bool(YT_REGEX.match(url.strip()))


def friendly_error(msg):
    low = msg.lower()
    if "your age" in low or "age-restricted" in low or "inappropriate for some users" in low:
        return "This video is age-restricted. Add YouTube cookies from a signed-in adult account to download it."
    if "requested format is not available" in low:
        return "That quality isn't available for this video. Pick another one."
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube blocked this server with a bot check. Add YouTube cookies in the "
                "YTDLP_COOKIES setting on Render, then try again.")
    if "private video" in low or "video is private" in low:
        return "This video is private, so it can't be downloaded."
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only."
    if "http error 403" in low or "403: forbidden" in low:
        return "YouTube refused the download. Try again in a minute, or update yt-dlp by redeploying."
    if "no space left" in low:
        return "The server ran out of space. Try a lower quality or a shorter video."
    if "video unavailable" in low or "not available" in low:
        return "This video is unavailable. Check the link and try again."
    if "live event" in low or "is live" in low:
        return "Live streams can't be downloaded until they end."
    if "premieres in" in low:
        return "This video hasn't premiered yet."
    if "timed out" in low or "connection refused" in low or "failed to establish" in low or "connection reset" in low:
        return "The connection to YouTube dropped. Try again."
    return re.sub(r"^ERROR:\s*(\[\w+\]\s*\S+:\s*)?", "", msg)[:300]


# yt-dlp swaps characters Windows can't use for look-alike symbols (： ⧸ ？). Turn them into plain text.
_LOOKALIKES = {"：": " -", "⧸": "-", "⧹": "-", "？": "", "＊": "", "＂": "'", "＜": "", "＞": "", "｜": "-"}


def clean_filename(name):
    stem, ext = os.path.splitext(name)
    for a, b in _LOOKALIKES.items():
        stem = stem.replace(a, b)
    stem = re.sub(r"\s+", " ", stem).strip(" .-") or "download"
    return stem + ext


def video_label(fmt):
    """Label by the shorter side so vertical Shorts don't show as '1920p'."""
    w, h = fmt.get("width") or 0, fmt.get("height") or 0
    return min(w, h) if w and h else h


# ---------- pages ----------

@app.route("/")
def index():
    return render_template("index.html")


# ---------- PWA ----------

STATIC_DIR = os.path.join(app.root_path, "static")
FIND_YT = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)


@app.route("/sw.js")
def service_worker():
    # Served from the root so it can control the whole site; never cached so updates land fast.
    res = send_from_directory(STATIC_DIR, "sw.js", mimetype="text/javascript", max_age=0)
    res.headers["Cache-Control"] = "no-cache"
    res.headers["Service-Worker-Allowed"] = "/"
    return res


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC_DIR, "manifest.webmanifest", mimetype="application/manifest+json", max_age=3600)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(STATIC_DIR, "favicon.ico", max_age=86400)


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_icon():
    return send_from_directory(os.path.join(STATIC_DIR, "icons"), "apple-touch-icon.png", max_age=86400)


@app.route("/share")
def share_target():
    """Android 'Share' from the YouTube app lands here. The link can arrive in url, text or title."""
    for key in ("url", "text", "title"):
        m = FIND_YT.search(request.args.get(key, "") or "")
        if m:
            return redirect(url_for("index", url=m.group(0).rstrip(".,)")))
    return redirect(url_for("index", shared="none"))


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.after_request
def cache_headers(res):
    if request.path.startswith("/static/") and res.status_code == 200:
        res.headers["Cache-Control"] = "public, max-age=604800"
    if request.path == "/":
        res.headers["Cache-Control"] = "no-cache"
    return res


@app.route("/healthz")
def health():
    return "ok"


@app.route("/api/status")
def status():
    """Quick check that everything the downloader needs is installed."""
    try:
        import yt_dlp_ejs  # noqa: F401
        ejs = True
    except ImportError:
        ejs = False
    active = sum(1 for j in JOBS.values() if j["status"] in ACTIVE)
    return jsonify(
        yt_dlp=yt_dlp.version.__version__,
        yt_dlp_ejs=ejs,
        deno=bool(shutil.which("deno")),
        ffmpeg=bool(shutil.which("ffmpeg")),
        cookies=bool(COOKIES_FILE),
        active_downloads=active,
        max_active_downloads=MAX_ACTIVE_JOBS,
    )


# ---------- info ----------

@app.route("/api/info", methods=["POST"])
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="That doesn't look like a YouTube link. Paste a link from youtube.com or youtu.be."), 400
    try:
        with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(error=friendly_error(str(e))), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Something went wrong while reading the video: {e}"), 500

    return jsonify(build_info(data))


def build_info(data):
    duration = int(data.get("duration") or 0)
    formats = data.get("formats") or []

    best_audio = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none"):
            best_audio = max(best_audio, f.get("filesize") or f.get("filesize_approx") or 0)

    by_height = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none") or h < 144:
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if size and f.get("acodec") in (None, "none"):
            size += best_audio
        cur = by_height.get(h)
        if not cur or size > cur["size"]:
            by_height[h] = {"height": h, "label": video_label(f), "size": size, "fps": f.get("fps")}

    resolutions = sorted(by_height.values(), key=lambda r: r["height"], reverse=True)
    audio = [{"bitrate": b, "size": duration * int(b) * 125} for b in AUDIO_BITRATES]

    thumb = data.get("thumbnail")
    thumbs = [t for t in (data.get("thumbnails") or [])
              if t.get("url") and (t.get("width") or 0) >= 480 and ".webp" not in t["url"]]
    if thumbs:
        thumb = max(thumbs, key=lambda t: t.get("width") or 0)["url"]

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "channel": data.get("uploader") or data.get("channel"),
        "duration": duration,
        "views": data.get("view_count"),
        "likes": data.get("like_count"),
        "upload_date": data.get("upload_date"),  # YYYYMMDD
        "thumbnail": thumb,
        "resolutions": resolutions,
        "audio": audio,
        "too_long": bool(duration and duration > MAX_DURATION),
        "max_minutes": MAX_DURATION // 60,
    }


# ---------- thumbnail ----------

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@app.route("/api/thumbnail/<video_id>")
def thumbnail(video_id):
    """Fetch the best available thumbnail from YouTube and send it as a JPG download."""
    import urllib.request
    if not VIDEO_ID.match(video_id):
        abort(404)
    for name in ("maxresdefault", "sddefault", "hqdefault", "mqdefault"):
        try:
            req = urllib.request.Request(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
            # YouTube returns a tiny grey placeholder (120x90, ~1 KB) when a size is missing
            if len(data) > 2000:
                title = re.sub(r'[\\/:*?"<>|]+', "", request.args.get("title", "") or video_id)[:100].strip()
                return send_file(BytesIO(data), mimetype="image/jpeg", as_attachment=True,
                                 download_name=f"{title or video_id} thumbnail.jpg")
        except Exception:  # noqa: BLE001
            continue
    return jsonify(error="Couldn't get the thumbnail for this video."), 404


# ---------- jobs ----------

def new_job(url, kind, quality):
    job_id = uuid.uuid4().hex[:16]
    job = {
        "id": job_id, "url": url, "type": kind, "quality": quality,
        "status": "queued", "stage": "Starting", "percent": 0.0,
        "speed": None, "eta": None, "downloaded": 0, "total": 0,
        "filename": None, "size": 0, "error": None,
        "cancel": False, "created": time.time(),
        "dir": os.path.join(DOWNLOAD_ROOT, job_id),
    }
    os.makedirs(job["dir"])
    with JOBS_LOCK:
        JOBS[job_id] = job
    return job


def public(job):
    keys = ("id", "type", "quality", "status", "stage", "percent", "speed", "eta",
            "downloaded", "total", "filename", "size", "error")
    return {k: job[k] for k in keys}


def build_opts(job):
    opts = {
        **base_opts(),
        "outtmpl": os.path.join(job["dir"], "%(title).120B.%(ext)s"),
        "windowsfilenames": True,  # file names that also work on Windows phones/PCs
    }
    if job["type"] == "audio":
        bitrate = job["quality"] if job["quality"] in AUDIO_BITRATES else "192"
        opts["format"] = "bestaudio/best"
        opts["writethumbnail"] = True  # used as the MP3's cover art
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": bitrate},
            {"key": "FFmpegMetadata"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "EmbedThumbnail"},
        ]
    else:
        h = int(job["quality"]) if str(job["quality"]).isdigit() else 100000
        # Prefer H.264 + AAC for maximum compatibility (phones, Windows, editors), then anything.
        opts["format"] = (
            f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/"
            f"b[height<={h}]/b"
        )
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    return opts


def run_job(job):
    state = {"done_files": 0, "n_files": 1}

    def progress_hook(d):
        if job["cancel"]:
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if total:
                frac = min(got / total, 1)
            elif d.get("fragment_count"):  # streams sent in pieces have no total size
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0
            overall = (state["done_files"] + frac) / state["n_files"]
            fmt = d.get("info_dict") or {}
            is_audio = fmt.get("vcodec") == "none"
            job.update(
                status="downloading",
                stage="Downloading audio" if is_audio else "Downloading video",
                percent=round(min(overall * 92, 92), 1),  # last 8% is reserved for processing
                speed=d.get("speed"), eta=d.get("eta"),
                downloaded=got, total=total,
            )
        elif d["status"] == "finished":
            state["done_files"] += 1
            job["percent"] = round(min(state["done_files"] / state["n_files"] * 92, 92), 1)

    def pp_hook(d):
        if job["cancel"]:
            raise Cancelled()
        if d["status"] == "started":
            name = d.get("postprocessor", "")
            stage = {
                "Merger": "Joining video and audio",
                "ExtractAudio": "Converting to MP3",
                "VideoRemuxer": "Finishing MP4",
                "Metadata": "Adding song info",
                "EmbedThumbnail": "Adding cover art",
            }.get(name)
            if stage:
                job.update(status="processing", stage=stage, speed=None, eta=None,
                           percent=max(job["percent"], 94.0))

    opts = build_opts(job)
    opts["progress_hooks"] = [progress_hook]
    opts["postprocessor_hooks"] = [pp_hook]

    try:
        job.update(status="preparing", stage="Getting the video ready")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=False)
            if info.get("is_live"):
                raise yt_dlp.utils.DownloadError("is live")
            if (info.get("duration") or 0) > MAX_DURATION:
                raise yt_dlp.utils.DownloadError(
                    f"Videos longer than {MAX_DURATION // 60} minutes are turned off on this site.")
            state["n_files"] = len(info.get("requested_formats") or []) or 1
            ydl.process_ie_result(info, download=True)

        want = ".mp3" if job["type"] == "audio" else ".mp4"
        files = [f for f in os.listdir(job["dir"]) if f.lower().endswith(want)]
        if not files:  # fall back to whatever media file exists
            files = [f for f in os.listdir(job["dir"])
                     if not f.endswith((".part", ".ytdl", ".temp", ".webp", ".jpg", ".png"))]
        files = sorted(files, key=lambda f: os.path.getsize(os.path.join(job["dir"], f)), reverse=True)
        if not files:
            raise RuntimeError("The download finished but no file was created.")
        nice = clean_filename(files[0])
        path = os.path.join(job["dir"], nice)
        os.replace(os.path.join(job["dir"], files[0]), path)
        job.update(status="done", stage="Ready", percent=100.0, filename=nice,
                   size=os.path.getsize(path), speed=None, eta=None)
    except Cancelled:
        job.update(status="cancelled", stage="Cancelled", speed=None, eta=None)
        shutil.rmtree(job["dir"], ignore_errors=True)
    except yt_dlp.utils.DownloadError as e:
        if job["cancel"]:
            job.update(status="cancelled", stage="Cancelled")
        else:
            job.update(status="error", stage="Failed", error=friendly_error(str(e)))
        shutil.rmtree(job["dir"], ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        job.update(status="error", stage="Failed", error=f"Something went wrong: {e}")
        shutil.rmtree(job["dir"], ignore_errors=True)


@app.route("/api/jobs", methods=["POST"])
def create_job():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    kind = body.get("type")
    quality = str(body.get("quality") or "")
    if not valid_url(url):
        return jsonify(error="That doesn't look like a YouTube link."), 400
    if kind not in ("video", "audio"):
        return jsonify(error="Choose Video or Audio."), 400
    if sum(1 for j in JOBS.values() if j["status"] in ACTIVE) >= MAX_ACTIVE_JOBS:
        return jsonify(error="The server is busy with other downloads. Try again in a minute."), 429
    job = new_job(url, kind, quality)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(public(job)), 202


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="This download has expired. Start it again."), 404
    return jsonify(public(job))


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Not found"), 404
    job["cancel"] = True
    return jsonify(public(job))


@app.route("/api/jobs/<job_id>/file")
def job_file(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    path = os.path.join(job["dir"], job["filename"])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=job["filename"])


# ---------- housekeeping ----------

def janitor():
    while True:
        time.sleep(120)
        now = time.time()
        with JOBS_LOCK:
            expired = [j for j in JOBS.values() if now - j["created"] > JOB_TTL]
            for j in expired:
                j["cancel"] = True
                JOBS.pop(j["id"], None)
        for j in expired:
            shutil.rmtree(j["dir"], ignore_errors=True)
        live = {j["dir"] for j in JOBS.values()}
        for name in os.listdir(DOWNLOAD_ROOT):
            p = os.path.join(DOWNLOAD_ROOT, name)
            if os.path.isdir(p) and p not in live and now - os.path.getmtime(p) > JOB_TTL:
                shutil.rmtree(p, ignore_errors=True)


threading.Thread(target=janitor, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
