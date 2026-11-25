#!/usr/bin/env python3
"""Stratus House Media dashboard server.

A small local web server (Python standard library only, Python 3.9+) that sits
in front of the OpenShorts autopilot. It reads the Clip Inbox / Clips Ready
folders, keeps the approval queue, posts approved clips through Upload-Post at
the chosen time, imports KDP sales reports, mixes light background music, and
runs the Social Media Manager chat on the Claude API.

It binds to 127.0.0.1 only and idles with one background thread that wakes
once a minute, so it stays light next to other apps.
"""

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
HOME = os.path.expanduser("~")

DEFAULT_CONFIG = {
    "port": 5190,
    "owner_name": "Mr. McDonald",
    "brand": "Stratus House Media",
    "clip_inbox": os.path.join(HOME, "Movies", "Clip Inbox"),
    "clips_ready": os.path.join(HOME, "Movies", "Clips Ready"),
    "music_dir": os.path.join(HOME, "Movies", "Stratus Music"),
    "ugc_dir": os.path.join(HOME, "Movies", "UGC Videos"),
    "openshorts_dir": os.path.join(HOME, "openshorts"),
    "keys_env": os.path.join(HOME, "openshorts", "autopilot-keys.env"),
    "autopilot_log": os.path.join(HOME, "openshorts", "autopilot.log"),
    "upload_post_profile": "MAIN",
    "upload_post_monthly_limit": 10,   # 0 = unlimited (Upload-Post Basic)
    "tiktok_auto": False,              # True = Upload-Post posts to TikTok too (Basic plan)
    "end_tag": True,                   # his OK 2026-11-24: 2 s liquid-gold SH tag on the end of every clip
    # Clipping hours: weekdays from market close until morning, weekends all day.
    "clip_weekday_open": "14:30",
    "clip_close": "06:30",
    "clip_schedule": False,   # his call 2026-11-20: no time limits; he opens/closes it himself
    "clip_weekends_all_day": True,
    "night_run": os.path.join(HOME, "openshorts", "autopilot", "night_run.sh"),
    "morning_stop": os.path.join(HOME, "openshorts", "autopilot", "morning_stop.sh"),
    "trend_brief": "Every night 8:00 PM",
    # Nightly trend brief (written by the cloud Trend Scout, 8 PM Phoenix) is published here as JSON.
    "trend_feed": "https://raw.githubusercontent.com/nathanmcdonaldj-hub/ECC/claude/project-thread-p72cad/trend-briefs/latest.json",
    "ffmpeg": "",
    "default_model": "claude-opus-5-5",
    "models": ["claude-opus-5-5", "claude-sonnet-5-5"],
    "dry_run_posting": False,
    "stay_awake": True,   # his ask 2026-11-25: keep the Mac from idle-sleeping so posting/clipping keep running
}

DATA_DIR = os.environ.get(
    "STRATUS_DATA_DIR",
    os.path.join(HOME, "Library", "Application Support", "StratusMedia"),
)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
SECRETS_FILE = os.path.join(DATA_DIR, "secrets.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac")

LOCK = threading.RLock()


# ---------------------------------------------------------------- storage

def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data, private=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_FILE, {}))
    return cfg


CONFIG = {}
STATE = {}


def blank_state():
    return {
        "clips": {},        # clip_id -> record
        "posts": [],        # posting history
        "errors": [],       # active + past errors
        "books": [],        # coloring books
        "sales": {"imported_at": None, "file": None, "rows": []},
        "stats": {"fetched_at": None, "data": None},
        "ugc": [],          # UGC briefs
        "creations": [],    # Create tab videos (Higgsfield API)
        "chat": {"model": None, "messages": [], "started": None},
        "pending_actions": {},
        "settings": {},
    }


def load_state():
    st = blank_state()
    st.update(load_json(STATE_FILE, {}))
    return st


def persist():
    with LOCK:
        save_json(STATE_FILE, STATE)


def secrets():
    data = load_json(SECRETS_FILE, {})
    # Keys the clip autopilot already uses (never shown, only read).
    env = read_env_file(CONFIG.get("keys_env", ""))
    if not data.get("upload_post_key") and env.get("UPLOAD_POST_API_KEY"):
        data["upload_post_key"] = env["UPLOAD_POST_API_KEY"]
    return data


def read_env_file(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.replace("export ", "").strip()
                out[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def save_secret(name, value):
    data = load_json(SECRETS_FILE, {})
    if value:
        data[name] = value.strip()
    else:
        data.pop(name, None)
    save_json(SECRETS_FILE, data, private=True)


# ---------------------------------------------------------------- errors

def notify_mac(title, message):
    """Show a macOS notification. Silently skipped off macOS."""
    if sys.platform != "darwin":
        return
    script = 'display notification {} with title {} sound name "Basso"'.format(
        json.dumps(message[:220]), json.dumps(title[:80])
    )
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def raise_error(worker, title, what, fix, action=None, key=None):
    """Record a plain-words error, show it on the floor and as a Mac alert.

    key: de-duplicates repeats of the same problem while it is still open.
    """
    with LOCK:
        key = key or "{}:{}".format(worker, title)
        for err in STATE["errors"]:
            if err.get("key") == key and not err.get("resolved"):
                err["count"] = err.get("count", 1) + 1
                err["last"] = now_iso()
                persist()
                return err
        err = {
            "id": uuid.uuid4().hex[:10],
            "key": key,
            "worker": worker,
            "title": title,
            "what": what,
            "fix": fix,
            "action": action,
            "time": now_iso(),
            "last": now_iso(),
            "count": 1,
            "resolved": False,
        }
        STATE["errors"].insert(0, err)
        STATE["errors"] = STATE["errors"][:100]
        persist()
    notify_mac("Stratus House · " + worker, title + " — " + fix)
    return err


def resolve_errors(key_prefix):
    with LOCK:
        changed = False
        for err in STATE["errors"]:
            if not err.get("resolved") and err.get("key", "").startswith(key_prefix):
                err["resolved"] = True
                changed = True
        if changed:
            persist()


# ---------------------------------------------------------------- time

def now():
    return datetime.now()


def now_iso():
    return now().strftime("%Y-%m-%dT%H:%M:%S")


def parse_hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def in_clip_window(dt=None):
    """Weekdays from market close (2:30 PM) to 6:30 AM; Saturday and Sunday all day."""
    dt = dt or now()
    if not CONFIG.get("clip_schedule", False):
        return True
    if CONFIG.get("clip_weekends_all_day", True) and dt.weekday() >= 5:
        return True
    oh, om = parse_hhmm(CONFIG["clip_weekday_open"])
    ch, cm = parse_hhmm(CONFIG["clip_close"])
    t = dt.hour * 60 + dt.minute
    return t >= oh * 60 + om or t < ch * 60 + cm


def _next_flip(dt, want):
    """First moment after dt (15-minute steps, up to 9 days) when the window is `want`."""
    t = dt.replace(second=0, microsecond=0)
    t += timedelta(minutes=15 - t.minute % 15)
    for _ in range(9 * 96):
        if in_clip_window(t) == want:
            return t
        t += timedelta(minutes=15)
    return None


def next_window_open(dt=None):
    """Next time the schedule opens, skipping the window we may be in right now."""
    dt = dt or now()
    if in_clip_window(dt):
        end = _next_flip(dt, False)
        return _next_flip(end, True) if end else None
    return _next_flip(dt, True)


def gate():
    """Clipping gate: the schedule, unless Mr. McDonald pressed Open (override) or Close."""
    g = STATE.setdefault("clip_gate", {"manual": None, "until": None})
    if g.get("manual") and g.get("until") and now_iso() >= g["until"]:
        g["manual"], g["until"] = None, None
    if g.get("manual") == "open":
        return True
    if g.get("manual") == "closed":
        return False
    return in_clip_window()


def nice_dt(dt):
    if not dt:
        return ""
    day = "today" if dt.date() == now().date() else (
        "tomorrow" if dt.date() == (now() + timedelta(days=1)).date() else dt.strftime("%A"))
    return "{} {}".format(dt.strftime("%I:%M %p").lstrip("0"), day)


def nice_time(iso):
    try:
        dt = datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return ""
    day = dt.strftime("%a")
    if dt.date() == now().date():
        day = "Today"
    elif dt.date() == (now() + timedelta(days=1)).date():
        day = "Tomorrow"
    hour = dt.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    return "{} {}".format(day, hour)


# ---------------------------------------------------------------- clips

HIDE_FILE = "hide.json"   # read by the clipping pipeline: {"links": [url], "files": [name]}


def hide_ready():
    """The face/voice helper is set up (written after the helper image builds and passes its test)."""
    return os.path.exists(os.path.join(APP_DIR, "tools", ".hide-ready"))


def clip_work():
    """Inbox items the clipper can do now (hidden ones wait until the helper is set up)."""
    ready = hide_ready()
    return [x for x in list_inbox() if ready or not x.get("hide")]


GAMEPLAY_FILE = "gameplay.json"   # same format: clipper keeps only moments that show game play


def hide_flags(fname=HIDE_FILE):
    d = load_json(os.path.join(CONFIG["clip_inbox"], fname), {})
    return {"links": list(d.get("links") or []), "files": list(d.get("files") or [])}


def set_hide(kind, value, on, fname=HIDE_FILE):
    d = hide_flags(fname)
    key = "links" if kind == "link" else "files"
    d[key] = [x for x in d[key] if x != value] + ([value] if on else [])
    try:
        save_json(os.path.join(CONFIG["clip_inbox"], fname), d)
    except OSError:
        pass


def list_inbox():
    inbox = CONFIG["clip_inbox"]
    hide = hide_flags()
    game = hide_flags(GAMEPLAY_FILE)
    processed = set()
    for name in ("processed.txt",):
        for base in (inbox, os.path.join(CONFIG["openshorts_dir"], "autopilot")):
            try:
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    for l in fh:
                        l = l.strip()
                        if not l:
                            continue
                        processed.add(l)
                        # Autopilot format: "name|size" for files, "link|url" for links.
                        head, _, tail = l.partition("|")
                        processed.add(tail if head == "link" else head)
            except OSError:
                pass
    items = []
    try:
        for name in sorted(os.listdir(inbox)):
            if name.lower().endswith(VIDEO_EXT) and not name.startswith("."):
                full = os.path.join(inbox, name)
                if name in processed or full in processed:
                    continue
                items.append({"kind": "video", "name": name, "hide": name in hide["files"], "game": name in game["files"],
                              "size_mb": round(os.path.getsize(full) / 1e6, 1)})
    except OSError:
        pass
    try:
        with open(os.path.join(inbox, "links.txt"), encoding="utf-8") as fh:
            for line in fh:
                url = line.strip()
                if url and not url.startswith("#") and url not in processed:
                    items.append({"kind": "link", "name": url, "hide": url in hide["links"], "game": url in game["links"]})
    except OSError:
        pass
    return items


def _clip_number(name):
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0


def parse_clips_md(path):
    """Split clips.md into per-clip text keyed by clip number (best effort)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    parts = re.split(r"(?im)^#+\s*clip\s*#?\s*(\d+)\b.*$", text)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[int(parts[i])] = parts[i + 1].strip()[:2000]
    if not out and text.strip():
        out[0] = text.strip()[:2000]
    return out


def scan_clips():
    """Find new clips in Clips Ready and add them to the queue."""
    root = CONFIG["clips_ready"]
    found = {}
    hunted_cache = {}
    try:
        videos = sorted(os.listdir(root))
    except OSError:
        return found
    for vid in videos:
        vdir = os.path.join(root, vid)
        if not os.path.isdir(vdir) or vid.startswith("."):
            continue
        files = os.listdir(vdir)
        notes = parse_clips_md(os.path.join(vdir, "clips.md"))
        images = [f for f in files if f.lower().endswith(IMAGE_EXT)]
        for f in sorted(files):
            if not f.lower().endswith(VIDEO_EXT) or ".music" in f or f.startswith("."):
                continue
            num = _clip_number(f)
            thumb = next((i for i in images if os.path.splitext(i)[0] == os.path.splitext(f)[0]), None)
            if thumb is None:
                thumb = next((i for i in images if _clip_number(i) == num), None)
            cid = vid + "/" + f
            found[cid] = {
                "id": cid,
                "source": vid,
                "file": os.path.join(vdir, f),
                "thumb": os.path.join(vdir, thumb) if thumb else None,
                "num": num,
                "notes": notes.get(num) or notes.get(0, ""),
                "mtime": os.path.getmtime(os.path.join(vdir, f)),
            }
            hunted = hunted_cache.setdefault(vdir, hunter_source(vdir))
            if hunted:
                found[cid]["notes"] = ("Clip Hunter find from {plat}. Creator: {c} ({u}). Credit and tag them in the caption "
                                       "as @{h}. Original title: {t}\n").format(
                    plat=hunted["platform"].title(), c=hunted["creator"], u=hunted.get("creator_url") or "",
                    h=re.sub(r"[^A-Za-z0-9_.]", "", hunted["creator"]), t=hunted["title"]) + found[cid]["notes"]
                found[cid]["hunter"] = True
                found[cid]["creator"] = hunted["creator"]
    with LOCK:
        for cid, info in found.items():
            rec = STATE["clips"].get(cid)
            if rec is None:
                rec = {"status": "new", "caption": "", "hashtags": "",
                       "platforms": ["instagram", "youtube"],
                       "time": suggest_time(sum(1 for r in STATE["clips"].values() if r.get("status") in ("new", "ready"))),
                       "why": "", "music": "auto", "added": now_iso()}
                STATE["clips"][cid] = rec
            elif rec.get("status") == "skipped" and info["mtime"] > (rec.get("mtime") or 0) + 5:
                # same name, but a freshly re-made file: treat it as a new clip
                rec.update({"status": "new", "caption": "", "hashtags": "", "why": "", "added": now_iso()})
                rec.pop("skipped_at", None)
            rec.update({k: info[k] for k in ("id", "source", "file", "thumb", "num", "notes", "mtime")})
            if info.get("hunter"):
                rec["hunter"], rec["creator"] = True, info.get("creator")
            rec["missing"] = False
        for cid, rec in STATE["clips"].items():
            if rec.get("source") == "UGC":   # made videos live in UGC Videos, not the clip folder
                rec["missing"] = not os.path.exists(rec.get("file") or "")
            elif cid not in found and rec.get("status") in ("new", "ready", "scheduled"):
                rec["missing"] = True
    return found


def clip_public(rec):
    out = {k: rec.get(k) for k in (
        "id", "source", "num", "status", "caption", "hashtags", "platforms",
        "time", "why", "music", "voice_text", "voice", "voice_when", "notes", "posted_at", "results", "missing",
        "tiktok_done", "redo", "metrics", "metrics_at", "outside", "yt_title")}
    out["posting"] = rec["id"] in _POSTING
    out["video_url"] = "/media?f=" + urllib.parse.quote(rec["id"])
    out["thumb_url"] = ("/media?thumb=1&f=" + urllib.parse.quote(rec["id"])) if rec.get("thumb") else None
    out["time_nice"] = nice_time(rec.get("time"))
    if rec.get("music_file") and os.path.exists(rec["music_file"]):
        out["music_url"] = "/media?music=1&f=" + urllib.parse.quote(rec["id"])
    return out


def suggest_time(i=0):
    """Default slot when the caption writer gives none: next 6 PM, spaced a day apart."""
    base = now().replace(hour=18, minute=0, second=0, microsecond=0)
    if base <= now() + timedelta(minutes=30):
        base += timedelta(days=1)
    return (base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------- music

def find_ffmpeg():
    cands = [CONFIG.get("ffmpeg"), os.path.join(APP_DIR, "bin", "ffmpeg"),
             shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def list_music():
    d = CONFIG["music_dir"]
    try:
        return sorted(f for f in os.listdir(d) if f.lower().endswith(AUDIO_EXT) and not f.startswith("."))
    except OSError:
        return []


def pick_auto_track(rec):
    tracks = list_music()
    if not tracks:
        return None
    return tracks[hash(rec["id"]) % len(tracks)]


_VOICES = None
VOICE_PREF = ("Samantha", "Daniel", "Karen", "Moira", "Alex", "Tessa", "Fred")
VOICE_SKIP = ("Bad News", "Good News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Jester",
              "Organ", "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox")


def list_voices():
    """English voices built into the Mac (free, offline). Cached after the first look."""
    global _VOICES
    if _VOICES is not None:
        return _VOICES
    names = []
    say = shutil.which("say") or ("/usr/bin/say" if os.path.exists("/usr/bin/say") else None)
    if say:
        try:
            res = subprocess.run([say, "-v", "?"], capture_output=True, text=True, timeout=20)
            for line in res.stdout.splitlines():
                m = re.match(r"^(.+?)\s{2,}(en_[A-Za-z]+)\s", line)
                if m and m.group(1).strip() not in VOICE_SKIP:
                    names.append(m.group(1).strip())
        except (OSError, subprocess.SubprocessError):
            names = []
    names.sort(key=lambda n: (VOICE_PREF.index(n) if n in VOICE_PREF else len(VOICE_PREF), n))
    _VOICES = names
    return names


# Natural voices: VoiceStudio's local engine (Kokoro, Apache-2.0) on 127.0.0.1:3900.
# Started only when a voice line is made, and stopped again after a few idle minutes to keep the Mac light.
VS_URL = "http://127.0.0.1:3900"
VS_SCRIPT = os.path.join(APP_DIR, "bin", "voicestudio-backend.sh")
VS_IDLE_STOP = 300
NATURAL_VOICES = [
    ("af_heart", "Heart · US woman, warm"), ("am_michael", "Michael · US man"),
    ("af_bella", "Bella · US woman"), ("am_adam", "Adam · US man"),
    ("af_nicole", "Nicole · US woman, soft"), ("am_onyx", "Onyx · US man, deep"),
    ("bf_emma", "Emma · UK woman"), ("bm_george", "George · UK man"),
]
_VS_LOCK = threading.Lock()
_VS_TIMER = [None]
_VS_STARTED_BY_US = [False]


def natural_ready():
    return os.path.exists(VS_SCRIPT) and os.path.isdir("/Applications/VoiceStudio.app")


def voice_options():
    """Natural voices first (when VoiceStudio is installed), then the Mac's built-in voices as backup."""
    opts = [{"id": "ks:" + v, "label": label, "group": "Natural voices"} for v, label in NATURAL_VOICES] if natural_ready() else []
    opts += [{"id": n, "label": n, "group": "Mac voices (backup)"} for n in list_voices()]
    return opts


def _vs_up():
    try:
        with urllib.request.urlopen(VS_URL + "/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _vs_stop_idle():
    with _VS_LOCK:
        _VS_TIMER[0] = None
        if _VS_STARTED_BY_US[0]:
            subprocess.run(["/bin/bash", VS_SCRIPT, "stop"], capture_output=True, timeout=30)
            _VS_STARTED_BY_US[0] = False


def _vs_touch():
    if _VS_TIMER[0]:
        _VS_TIMER[0].cancel()
    t = threading.Timer(VS_IDLE_STOP, _vs_stop_idle)
    t.daemon = True
    t.start()
    _VS_TIMER[0] = t


def natural_voice(text, voice, out_path):
    with _VS_LOCK:
        if not _vs_up():
            subprocess.run(["/bin/bash", VS_SCRIPT, "start"], capture_output=True, timeout=60)
            _VS_STARTED_BY_US[0] = True
            for _ in range(60):
                if _vs_up():
                    break
                time.sleep(1)
            else:
                raise RuntimeError("vs-down")
        _vs_touch()
    body = json.dumps({"model": "mlx-audio", "voice": voice, "input": text[:1000],
                       "response_format": "wav", "speed": 1.0}).encode()
    req = urllib.request.Request(VS_URL + "/v1/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    if len(data) < 1000:
        raise RuntimeError("vs-empty")
    with open(out_path, "wb") as fh:
        fh.write(data)
    with _VS_LOCK:
        _vs_touch()
    return out_path


def make_voice(text, voice, out_path):
    """Natural voice (VoiceStudio) when picked; the Mac's own voice otherwise, and as the fallback."""
    if voice and voice.startswith("ks:") and natural_ready():
        try:
            return natural_voice(text, voice[3:], os.path.splitext(out_path)[0] + ".wav")
        except Exception as exc:  # noqa: BLE001 - fall back to the Mac voice, never fail the clip
            print("[Voice] natural voice failed, using Mac voice: {}".format(exc), file=sys.stderr)
            voice = None
    return say_voice(text, voice, out_path)


def say_voice(text, voice, out_path):
    """Speak the line with the Mac's own voice (the `say` tool) into an audio file."""
    say = shutil.which("say") or ("/usr/bin/say" if os.path.exists("/usr/bin/say") else None)
    if not say:
        raise RuntimeError("no-say")
    txt = out_path + ".txt"
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write(text)
    cmd = [say, "-o", out_path, "-f", txt]
    if voice and voice in list_voices():
        cmd[1:1] = ["-v", voice]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    finally:
        try:
            os.remove(txt)
        except OSError:
            pass
    if res.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("say:" + (res.stderr or "").strip()[-200:])
    return out_path


def media_duration(ff, path):
    """Length in seconds, read from ffmpeg's own header line (no ffprobe needed)."""
    res = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", res.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


END_TAG = os.path.join(APP_DIR, "assets", "endtag.mp4")


def video_info(ff, path):
    """(width, height, has_audio) read from ffmpeg's header lines."""
    res = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=60)
    m = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", res.stderr)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
    return w - w % 2, h - h % 2, "Audio:" in res.stderr


def add_end_tag(src):
    """Return a copy of `src` with the 2-second Stratus House end tag on the end.
    Built once per clip and reused; on any problem the clip posts without the tag."""
    if not CONFIG.get("end_tag", True) or not os.path.exists(END_TAG):
        return src
    if os.path.basename(src).lower().startswith("stratus house"):
        return src  # our own finished videos (e.g. Black Friday) already carry the tag
    ff = find_ffmpeg()
    if not ff:
        return src
    out = os.path.splitext(src)[0].replace(".music", "") + ".music.endtag.mp4"
    if os.path.exists(out) and os.path.getmtime(out) >= max(os.path.getmtime(src), os.path.getmtime(END_TAG)):
        return out
    w, h, audio = video_info(ff, src)
    norm = "fps=30,format=yuv420p,setsar=1"
    graph = ("[0:v]scale={w}:{h},{n}[v0];"
             "[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
             "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x000C40,{n}[v1];"
             "{a0}aresample=48000,aformat=channel_layouts=stereo[a0];"
             "[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
             "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]").format(
                 w=w, h=h, n=norm, a0="[0:a]" if audio else "[2:a]")
    cmd = [ff, "-y", "-loglevel", "error", "-i", src, "-i", END_TAG]
    if not audio:
        cmd += ["-f", "lavfi", "-t", "{:.3f}".format(media_duration(ff, src) or 1), "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd   # keep the dashboard (and the trading desk) snappy
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if res.returncode != 0 or not os.path.exists(out):
        print("[EndTag] skipped for {}: {}".format(os.path.basename(src), (res.stderr or "")[-200:]), file=sys.stderr)
        return src
    return out


def wants_mix(rec):
    return (rec.get("music") or "none") != "none" or bool((rec.get("voice_text") or "").strip())


def mix_music(rec):
    """Build the final clip: optional light music bed and optional spoken voice line.
    Music sits at ~15% and dips when people talk; the clip's own sound dips while the voice speaks.
    Returns the output path, or None when there's nothing to add."""
    choice = rec.get("music") or "none"
    line = (rec.get("voice_text") or "").strip()
    if choice == "none" and not line:
        return None
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("no-ffmpeg")
    track_path = None
    if choice != "none":
        track = pick_auto_track(rec) if choice == "auto" else choice
        if not track:
            raise RuntimeError("no-tracks")
        track_path = os.path.join(CONFIG["music_dir"], track)
        if not os.path.exists(track_path):
            raise RuntimeError("missing-track:" + track)
    base, _ = os.path.splitext(rec["file"])
    out = base + ".music.mp4"
    inputs = ["-i", rec["file"]]
    parts = []
    mix = []
    orig = "[0:a]"
    if line:
        vfile = make_voice(line, rec.get("voice"), base + ".voice.aiff")
        vi = len(inputs) // 2
        inputs += ["-i", vfile]
        delay = 400
        if rec.get("voice_when") == "end":
            clip_len, voice_len = media_duration(ff, rec["file"]), media_duration(ff, vfile)
            if clip_len and voice_len:
                delay = max(0, int((clip_len - voice_len - 0.4) * 1000))
        parts.append("[{0}:a]aresample=48000,adelay={1}|{1},volume=1.4,apad,asplit=2[vo][vkey]".format(vi, delay))
        parts.append("[0:a][vkey]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=350[orig]")
        orig = "[orig]"
        mix.append("[vo]")
    if track_path:
        mi = len(inputs) // 2
        inputs = inputs + ["-stream_loop", "-1", "-i", track_path]
        parts.append("[{}:a]volume=0.15,afade=t=in:d=1[m]".format(mi))
        parts.append("[m][0:a]sidechaincompress=threshold=0.03:ratio=6:release=400[duck]")
        mix.append("[duck]")
    parts.append("{}{}amix=inputs={}:duration=first:dropout_transition=0:normalize=0[a]"
                 .format(orig, "".join(mix), 1 + len(mix)))
    cmd = [ff, "-y", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", out]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg:" + res.stderr.strip()[-300:])
    rec["music_file"] = out
    rec["music_track"] = os.path.basename(track_path) if track_path else None
    return out


def music_error(rec, exc):
    code = str(exc)
    name = "clip {} from {}".format(rec.get("num"), rec.get("source"))
    if code == "no-say" or code.startswith("say:"):
        raise_error("Your Desk", "Couldn't make the voice line",
                    "The Mac's built-in voice couldn't read the line on {}.".format(name),
                    "Try a different voice, or clear the voice line to post without it.",
                    key="voice:fail:" + rec["id"])
    elif code == "no-tracks":
        raise_error("Your Desk", "No music tracks yet",
                    "You asked for music on {}, but the music folder is empty.".format(name),
                    "Add a few royalty-free songs to Movies → Stratus Music, or pick “No music”.",
                    key="music:no-tracks")
    elif code == "no-ffmpeg":
        raise_error("Your Desk", "Music mixer is missing",
                    "The tool that adds music (ffmpeg) isn't installed on this Mac.",
                    "Ask Claude in your project chat to reinstall the music mixer. The clip can still post without music.",
                    key="music:no-ffmpeg")
    elif code.startswith("missing-track:"):
        raise_error("Your Desk", "Music track not found",
                    "The song “{}” isn't in Movies → Stratus Music anymore.".format(code.split(":", 1)[1]),
                    "Pick another song for {} or choose “Auto”.".format(name),
                    key="music:missing:" + rec["id"])
    else:
        raise_error("Your Desk", "Couldn't add music to " + name,
                    "The music mixer stopped with: " + code[-200:],
                    "Pick a different song or post it without music. If it keeps happening, ask Claude.",
                    key="music:fail:" + rec["id"])


# ---------------------------------------------------------------- Upload-Post

UPLOAD_POST_URL = "https://api.upload-post.com/api/upload"


def multipart(fields, files):
    boundary = "----stratus" + uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n"
                      .format(boundary, name, value).encode("utf-8"))
    for name, path in files:
        fname = os.path.basename(path)
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        head = ("--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
                "Content-Type: {}\r\n\r\n").format(boundary, name, fname, ctype).encode("utf-8")
        with open(path, "rb") as fh:
            chunks.append(head + fh.read() + b"\r\n")
    chunks.append("--{}--\r\n".format(boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def full_caption(rec):
    cap = (rec.get("caption") or "").strip()
    tags = (rec.get("hashtags") or "").strip()
    return (cap + ("\n\n" + tags if tags else "")).strip()


def youtube_title(rec):
    if (rec.get("yt_title") or "").strip():
        return rec["yt_title"].strip()[:95]
    first = (rec.get("caption") or "").strip().split("\n")[0]
    first = re.sub(r"https?://\S+", "", first).strip()
    return (first or "Stratus House")[:95]


_POSTING = set()


def post_clip(rec):
    """Post one clip, once. "Post now" and the scheduler can both reach a clip in the same minute,
    so a clip being uploaded (or already posted) is never sent a second time."""
    with LOCK:
        if rec["id"] in _POSTING or rec.get("status") in ("posted", "partial"):
            return True
        _POSTING.add(rec["id"])
    try:
        return _post_clip(rec)
    finally:
        with LOCK:
            _POSTING.discard(rec["id"])


def _post_clip(rec, only=None):
    """Post one approved clip to its auto platforms (or just `only`). Returns True on success."""
    key = secrets().get("upload_post_key")
    name = "clip {} from {}".format(rec.get("num"), rec.get("source"))
    platforms = [p for p in rec.get("platforms", []) if p in auto_platforms()
                 and not (p == "tiktok" and rec.get("tiktok_done"))]
    if only:
        platforms = [p for p in only if not (p == "tiktok" and rec.get("tiktok_done"))]
    if not platforms:
        return True
    if not key:
        raise_error("Courier", "Can't post: Upload-Post key missing",
                    "{} was due to post, but the Upload-Post key isn't saved.".format(name),
                    "Open Settings and paste your Upload-Post key. I'll post it right after.",
                    action="settings", key="post:nokey")
        return False
    if not os.path.exists(rec["file"]):
        raise_error("Courier", "Can't post: clip file is gone",
                    "{} isn't in Movies → Clips Ready anymore.".format(name),
                    "Put the file back, or Skip this clip.", key="post:missing:" + rec["id"])
        return False
    path = rec["file"]
    if wants_mix(rec):
        try:
            path = mix_music(rec)
        except Exception as exc:  # noqa: BLE001 - shown to the user in plain words
            music_error(rec, exc)
            return False
    try:
        path = add_end_tag(path)
    except Exception as exc:  # noqa: BLE001 - never block a post over the tag
        print("[EndTag] skipped: {}".format(exc), file=sys.stderr)
    used = posts_this_month()
    if CONFIG["upload_post_monthly_limit"] and used + len(platforms) > CONFIG["upload_post_monthly_limit"]:
        raise_error("Courier", "Free posting limit reached",
                    "{} needs {} post(s) but only {} of your {} free posts are left this month."
                    .format(name, len(platforms), max(0, CONFIG["upload_post_monthly_limit"] - used),
                            CONFIG["upload_post_monthly_limit"]),
                    "Wait until next month, post it by hand, or upgrade Upload-Post to Basic ($24/mo).",
                    key="post:limit")
        return False
    req_id = "sh-{}-{}".format(re.sub(r"[^A-Za-z0-9]", "", rec["id"])[-24:], int(time.time()))
    fields = [("user", CONFIG["upload_post_profile"]), ("request_id", req_id), ("title", youtube_title(rec)),
              ("description", full_caption(rec))]
    for p in platforms:
        fields.append(("platform[]", p))
    fields.append(("instagram_title", full_caption(rec)))
    if "tiktok" in platforms:
        fields.append(("tiktok_title", full_caption(rec)[:2200]))
    if CONFIG.get("dry_run_posting"):
        result = {"dry_run": True, "fields": [f[0] for f in fields]}
    else:
        body, ctype = multipart(fields, [("video", path)])
        req = urllib.request.Request(UPLOAD_POST_URL, data=body, method="POST", headers={
            "Authorization": "Apikey " + key, "Content-Type": ctype, "X-Request-Id": req_id})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            explain_post_http_error(name, exc.code, detail)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise_error("Courier", "Can't reach Upload-Post",
                        "Posting {} failed because the internet connection or Upload-Post didn't answer ({}).".format(name, exc),
                        "Check your Wi-Fi. I'll try again in 5 minutes automatically.",
                        key="post:net")
            rec["retry_at"] = (now() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
            return False
    failed = find_platform_failures(result, platforms)
    with LOCK:
        if only:
            rec["tiktok_results"] = result
        else:
            rec["results"] = result
            rec["request_id"] = (result.get("request_id") if isinstance(result, dict) else None) or req_id
        ok_platforms = [p for p in platforms if p not in failed]
        if ok_platforms:
            STATE["posts"].append({"id": rec["id"], "platforms": ok_platforms,
                                   "at": now_iso(), "caption": rec.get("caption", "")[:80]})
        if not only:
            rec["posted_at"] = now_iso()
            rec["status"] = "posted" if not failed else "partial"
        if "tiktok" in ok_platforms:
            rec["tiktok_done"] = True
        persist()
    for p, why in failed.items():
        raise_error("Courier", "Couldn't post {} to {}".format(name, p.title()),
                    "{} said: {}. {}".format(p.title(), why,
                                             "The other platform posted fine." if ok_platforms else ""),
                    "If it says your connection expired, reconnect {} in Upload-Post, then press Retry.".format(p.title()),
                    action="retry:" + rec["id"], key="post:fail:{}:{}".format(rec["id"], p))
    if not failed:
        resolve_errors("post:")
    return not failed


def find_platform_failures(result, platforms):
    failed = {}
    if not isinstance(result, dict):
        return failed
    if result.get("success") is False and not result.get("results"):
        msg = result.get("message") or result.get("error") or "unknown error"
        return {p: str(msg)[:160] for p in platforms}
    res = result.get("results") or {}
    if isinstance(res, dict):
        for p in platforms:
            r = res.get(p)
            if isinstance(r, dict) and (r.get("success") is False or r.get("skipped")
                                        or str(r.get("status", "")).lower() == "skipped"):
                failed[p] = str(r.get("error") or r.get("message") or r.get("reason")
                                or "it was skipped, usually because the account isn't connected")[:160]
    return failed


def explain_post_http_error(name, code, detail):
    if code in (401, 403):
        what = "Upload-Post refused the key (error {}). {}".format(code, detail[:160])
        fix = "Open Settings and paste a fresh Upload-Post key. If it mentions a plan, the free plan may not cover this."
    elif code == 429:
        what = "Upload-Post says you've hit a limit (error 429)."
        fix = "Wait until next month or upgrade Upload-Post. I'll keep the clip queued."
    else:
        what = "Upload-Post answered with error {}: {}".format(code, detail[:200])
        fix = "Press Retry. If it fails again, ask Claude in your project chat and paste this message."
    raise_error("Courier", "Couldn't post " + name, what, fix,
                action="settings" if code in (401, 403) else None,
                key="post:http:{}".format(code))


def auto_platforms():
    """Platforms Upload-Post posts for him. TikTok joins once he's on the Basic plan."""
    return ("instagram", "youtube", "tiktok") if CONFIG.get("tiktok_auto") else ("instagram", "youtube")


def posts_this_month():
    month = now().strftime("%Y-%m")
    return sum(len(p.get("platforms", [])) for p in STATE["posts"] if p.get("at", "").startswith(month))


# ---------------------------------------------------------------- stats

def fetch_stats():
    key = secrets().get("upload_post_key")
    if not key:
        return
    url = "https://api.upload-post.com/api/analytics/{}?platforms=instagram,youtube".format(
        urllib.parse.quote(CONFIG["upload_post_profile"]))
    req = urllib.request.Request(url, headers={"Authorization": "Apikey " + key})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise_error("Analyst", "Couldn't get your view numbers",
                    "Upload-Post's stats answered with error {}.".format(exc.code),
                    "Nothing to do right now. I'll try again in 6 hours. Your posts are fine.",
                    key="stats:http")
        return
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return  # offline: quietly try later
    with LOCK:
        STATE["stats"] = {"fetched_at": now_iso(), "data": data}
        persist()
    resolve_errors("stats:")


POST_ANALYTICS_URL = "https://api.upload-post.com/api/uploadposts/post-analytics/{}"
METRIC_KEYS = ("views", "likes", "comments", "shares", "saves")


def fetch_post_stats(max_age_days=60):
    """Per-video numbers (views, likes, comments, shares) for every clip posted in the last 60 days."""
    key = secrets().get("upload_post_key")
    if not key:
        return 0
    cutoff = (now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    with LOCK:
        recs = [r for r in STATE["clips"].values() if (r.get("request_id") or r.get("request_ids"))
                and r.get("status") in ("posted", "partial") and (r.get("posted_at") or "") >= cutoff]
    done = 0
    for rec in recs:
        rids = list(rec.get("request_ids") or [])
        if rec.get("request_id") and rec["request_id"] not in rids:
            rids.insert(0, rec["request_id"])
        metrics = dict(rec.get("metrics") or {})
        got = False
        for rid in rids:
            url = POST_ANALYTICS_URL.format(urllib.parse.quote(rid))
            req = urllib.request.Request(url, headers={"Authorization": "Apikey " + key})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue  # not visible yet; next round
                raise_error("Analyst", "Couldn't get numbers for your posts",
                            "Upload-Post's per-post stats answered with error {}.".format(exc.code),
                            "Nothing to do. I'll try again in 6 hours. Your posts are fine.", key="stats:post")
                return done
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                return done  # offline: try later
            for plat, info in ((data or {}).get("platforms") or {}).items():
                if not isinstance(info, dict):
                    continue
                pm = info.get("post_metrics") or {}
                m = {k: pm.get(k) for k in METRIC_KEYS if isinstance(pm.get(k), (int, float))}
                m["url"] = info.get("post_url") or (metrics.get(plat) or {}).get("url")
                if info.get("post_metrics_error"):
                    m["note"] = str(info["post_metrics_error"])[:120]
                if plat in (rec.get("platforms") or [plat]):
                    metrics[plat] = m
            got = True
            time.sleep(1)  # stay far below Upload-Post's 100 requests / 5 minutes
        if not got:
            continue
        with LOCK:
            rec["metrics"] = metrics
            rec["metrics_at"] = now_iso()
        done += 1
    if done:
        persist()
        resolve_errors("stats:post")
    return done


def summarize_stats():
    data = (STATE.get("stats") or {}).get("data") or {}
    out = {"views": None, "followers": {}, "fetched_at": STATE["stats"].get("fetched_at")}
    views = 0
    found_views = False

    def walk(node, platform=None):
        nonlocal views, found_views
        if isinstance(node, dict):
            for k, v in node.items():
                lk = k.lower()
                p = lk if lk in ("instagram", "youtube", "tiktok") else platform
                if isinstance(v, (int, float)) and p:
                    if lk in ("followers", "subscribers", "follower_count", "subscriber_count"):
                        out["followers"][p] = int(v)
                    elif lk in ("views", "impressions", "video_views", "reach"):
                        views += int(v)
                        found_views = True
                else:
                    walk(v, p)
        elif isinstance(node, list):
            for x in node:
                walk(x, platform)

    walk(data)
    if found_views:
        out["views"] = views
    return out


# ---------------------------------------------------------------- KDP sales

def _xlsx_rows(path):
    """Read every sheet of an .xlsx with the standard library."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    sheets = {}
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        names = {}
        try:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rmap = {r.get("Id"): r.get("Target") for r in rels}
            for s in wb.iter("{%s}sheet" % ns["m"]):
                rid = s.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = rmap.get(rid, "")
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                names[target] = s.get("name")
        except (KeyError, ET.ParseError):
            pass
        for n in zf.namelist():
            if not re.match(r"xl/worksheets/sheet\d+\.xml$", n):
                continue
            root = ET.fromstring(zf.read(n))
            rows = []
            for row in root.iter("{%s}row" % ns["m"]):
                cells = {}
                for c in row.findall("m:c", ns):
                    ref = c.get("r", "")
                    col = re.sub(r"\d", "", ref)
                    idx = 0
                    for ch in col:
                        idx = idx * 26 + (ord(ch) - 64)
                    t = c.get("t")
                    v = c.find("m:v", ns)
                    if t == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                    else:
                        val = v.text if v is not None else ""
                    cells[idx - 1] = val
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(i, "") for i in range(width)])
            sheets[names.get(n, n)] = rows
    return sheets


def _csv_rows(path):
    import csv
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return {"CSV": [r for r in csv.reader(fh)]}


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_kdp(path):
    """Pull title / units / royalty rows out of a KDP report."""
    sheets = _xlsx_rows(path) if path.lower().endswith(".xlsx") else _csv_rows(path)
    rows = []
    for sname, srows in sheets.items():
        for hi, header in enumerate(srows[:15]):
            low = [str(h).strip().lower() for h in header]
            if "title" not in low:
                continue
            ti = low.index("title")
            ui = next((i for i, h in enumerate(low) if h in ("net units sold", "units sold", "net units")), None)
            if ui is None:
                ui = next((i for i, h in enumerate(low) if "units" in h and "refund" not in h), None)
            ri = next((i for i, h in enumerate(low) if h == "royalty" or h.startswith("royalty (")), None)
            if ri is None:
                ri = next((i for i, h in enumerate(low) if "royalty" in h and "date" not in h and "type" not in h), None)
            ci = next((i for i, h in enumerate(low) if h == "currency"), None)
            di = next((i for i, h in enumerate(low) if "date" in h), None)
            if ui is None and ri is None:
                continue
            for r in srows[hi + 1:]:
                if ti >= len(r) or not str(r[ti]).strip():
                    continue
                rows.append({
                    "sheet": sname,
                    "title": str(r[ti]).strip(),
                    "units": _num(r[ui]) if ui is not None and ui < len(r) else 0,
                    "royalty": _num(r[ri]) if ri is not None and ri < len(r) else 0,
                    "currency": (r[ci] if ci is not None and ci < len(r) else "USD") or "USD",
                    "date": str(r[di]) if di is not None and di < len(r) else "",
                })
            break
    # The "Combined Sales" sheet repeats the per-format sheets; prefer detailed sheets.
    detailed = [r for r in rows if "combined" not in r["sheet"].lower()]
    return detailed or rows


def sales_summary():
    rows = STATE["sales"].get("rows") or []
    by_title = {}
    other_cur = set()
    for r in rows:
        b = by_title.setdefault(r["title"], {"title": r["title"], "units": 0, "royalty": 0.0})
        b["units"] += r["units"]
        if (r.get("currency") or "USD").upper() == "USD":
            b["royalty"] += r["royalty"]
        else:
            other_cur.add(r["currency"])
    books = sorted(by_title.values(), key=lambda b: -b["units"])
    for b in books:
        b["units"] = int(round(b["units"]))
        b["royalty"] = round(b["royalty"], 2)
    return {
        "imported_at": STATE["sales"].get("imported_at"),
        "file": STATE["sales"].get("file"),
        "books": books,
        "units": sum(b["units"] for b in books),
        "royalty": round(sum(b["royalty"] for b in books), 2),
        "other_currencies": sorted(other_cur),
    }


# ---------------------------------------------------------------- Claude API

ANTHROPIC_URL = os.environ.get("STRATUS_ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")

MANAGER_SYSTEM = """You are the Social Media Manager on the Stratus House Media dashboard, \
working for {owner}. Always address him as "{owner}", never by first name.

How you talk: short, blunt, numbers first, plain words, no jargon, no recaps. Concerns or \
questions first. When there are options give at most 3 and mark your pick. Never show \
passwords or keys. If you were wrong, say "I was wrong" plainly.

What you run: clips are made from his Clip Inbox whenever clipping is OPEN (no fixed hours; he presses Open/Close on the dashboard himself), 3 clips per video max; you and the \
Caption Writer prepare caption (hook, call to action, 3–5 hashtags), platform and time; he \
approves on Your Desk; the Courier posts to Instagram + YouTube through Upload-Post (free \
plan: 10 posts a month); TikTok he posts by hand. He also sells coloring books on Amazon KDP \
and makes UGC-style promo videos in Higgsfield. Niches: business, success, fitness, \
anti-aging, dogs, funny.

Hard rules: nothing posts without his explicit OK. When he asks you to post, schedule, skip \
or queue something, use the matching tool. Scheduling only creates a confirmation card on \
his screen; tell him to press Confirm. Never lecture him about copyright or what he posts; he handles that himself. \
Each user message starts with a [Dashboard now] snapshot: trust it over memory and don't \
repeat it back.

Replies are read aloud when voice is on, so keep them under 80 words unless he asks for more."""

MANAGER_TOOLS = [
    {
        "name": "propose_schedule",
        "description": "Prepare a clip for posting. Creates a confirmation card he must press; nothing posts until he does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "clip_id": {"type": "string", "description": "Clip id from the snapshot."},
                "time": {"type": "string", "description": "Local time, format YYYY-MM-DDTHH:MM."},
                "platforms": {"type": "array", "items": {"type": "string", "enum": ["instagram", "youtube", "tiktok"]}},
                "caption": {"type": "string", "description": "Hook line + call to action. Leave empty to keep the current one."},
                "hashtags": {"type": "string", "description": "3-5 hashtags. Empty keeps current."},
            },
            "required": ["clip_id", "time", "platforms"],
            "additionalProperties": False,
        },
    },
    {
        "name": "skip_clip",
        "description": "Mark a clip as skipped (won't post). Only when he clearly asks.",
        "input_schema": {"type": "object", "properties": {"clip_id": {"type": "string"}},
                         "required": ["clip_id"], "additionalProperties": False},
    },
    {
        "name": "queue_video_link",
        "description": "Add a video link to the Clip Inbox for the next clipping run.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}},
                         "required": ["url"], "additionalProperties": False},
    },
    {
        "name": "request_redo",
        "description": "Ask the Clipper to redo a clip on the next run with his note (e.g. 'make it shorter', 'start at the punchline').",
        "input_schema": {"type": "object", "properties": {"clip_id": {"type": "string"}, "note": {"type": "string"}},
                         "required": ["clip_id", "note"], "additionalProperties": False},
    },
]


def call_claude(body, model):
    key = secrets().get("anthropic_key")
    if not key:
        raise ClaudeError("nokey")
    body = dict(body)
    body["model"] = model
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    with_fallback = dict(body, fallbacks="default")
    for attempt, payload, beta in ((1, with_fallback, "server-side-fallback-2026-07-01"), (2, body, None)):
        h = dict(headers)
        if beta:
            h["anthropic-beta"] = beta
        req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(payload).encode("utf-8"),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 400 and attempt == 1 and ("fallback" in detail or "beta" in detail):
                continue  # account without the fallback beta: retry plain
            raise ClaudeError("http", exc.code, detail[:300])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ClaudeError("net", None, str(exc))
    raise ClaudeError("http", 400, "request rejected")


class ClaudeError(Exception):
    def __init__(self, kind, code=None, detail=""):
        super().__init__(kind)
        self.kind, self.code, self.detail = kind, code, detail


def claude_error_to_alert(worker, exc):
    if exc.kind == "nokey":
        raise_error(worker, "Claude key missing",
                    "The {} needs your Claude API key, and it isn't saved yet.".format(worker),
                    "Open Settings and paste the Claude key you use for your trading desk.",
                    action="settings", key="claude:nokey")
    elif exc.kind == "net":
        raise_error(worker, "Can't reach Claude",
                    "The internet connection to Claude failed ({}).".format(exc.detail[:120]),
                    "Check your Wi-Fi and try again.", key="claude:net")
    elif exc.code in (401, 403):
        raise_error(worker, "Claude refused the key",
                    "Claude said the API key isn't valid (error {}).".format(exc.code),
                    "Open Settings and paste the key again.", action="settings", key="claude:auth")
    elif exc.code == 429:
        raise_error(worker, "Claude is busy or out of credit",
                    "Claude said too many requests or your API credit ran out (error 429).",
                    "Wait a minute and try again. If it keeps happening, check your API balance at console.anthropic.com.",
                    key="claude:429")
    elif exc.code == 529 or (exc.code and exc.code >= 500):
        raise_error(worker, "Claude is having trouble",
                    "Claude's servers answered with error {}.".format(exc.code),
                    "Try again in a few minutes. Nothing on your side is broken.", key="claude:5xx")
    else:
        raise_error(worker, "Claude didn't accept the request",
                    "Error {}: {}".format(exc.code, exc.detail[:200]),
                    "Ask Claude in your project chat and paste this message.", key="claude:other")


def text_of(content):
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def snapshot_text():
    s = build_state(for_model=True)
    lines = ["[Dashboard now {}]".format(now().strftime("%a %b %d %I:%M %p"))]
    lines.append("Free posts used: {}/{}".format(s["posts_used"], s["posts_limit"]))
    for c in s["clips_waiting"][:8]:
        lines.append("Waiting clip {id}: '{cap}' platforms={pl} time={t}".format(
            id=c["id"], cap=(c["caption"] or c["notes"] or "")[:90].replace("\n", " "),
            pl=",".join(c["platforms"]), t=c["time"] or "-"))
    for c in s["clips_scheduled"][:8]:
        lines.append("Scheduled clip {}: {} at {}".format(c["id"], ",".join(c["platforms"]), c["time"]))
    for c in s["tiktok"][:5]:
        lines.append("TikTok to post by hand: " + c["id"])
    lines.append("Inbox: {} item(s)".format(len(s["inbox"])))
    st = s["stats"]
    if st["views"] is not None:
        lines.append("Views (Upload-Post stats): {}".format(st["views"]))
    if st["followers"]:
        lines.append("Followers: " + ", ".join("{} {}".format(k, v) for k, v in st["followers"].items()))
    sales = s["sales"]
    if sales["imported_at"]:
        lines.append("Book sales (last KDP import {}): {} units, ${} royalty".format(
            sales["imported_at"][:10], sales["units"], sales["royalty"]))
    for e in s["errors"][:5]:
        lines.append("OPEN PROBLEM at {}: {}".format(e["worker"], e["title"]))
    return "\n".join(lines)


def chat_turn(user_text):
    """One Social Media Manager turn with a small tool loop."""
    with LOCK:
        chat = STATE["chat"]
        model = chat.get("model") or CONFIG["default_model"]
        # Fresh conversation each day or when it gets long (history is append-only).
        if (chat.get("started") or "")[:10] != now_iso()[:10] or len(chat["messages"]) > 60 \
                or chat.get("conv_model") != model:
            chat["messages"] = []
            chat["started"] = now_iso()
            chat["conv_model"] = model
        msgs = chat["messages"]
        msgs.append({"role": "user", "content": snapshot_text() + "\n\n" + user_text})
    system = MANAGER_SYSTEM.format(owner=CONFIG["owner_name"])
    cards = []
    for _ in range(6):
        try:
            resp = call_claude({"max_tokens": 4000, "system": system, "tools": MANAGER_TOOLS,
                                "messages": msgs}, model)
        except ClaudeError as exc:
            with LOCK:
                msgs.pop()  # drop the unanswered user turn
                persist()
            claude_error_to_alert("Social Media Manager", exc)
            return {"error": True, "reply": "I couldn't reach my brain just now, {}. The red banner says why."
                    .format(CONFIG["owner_name"])}
        content = resp.get("content", [])
        with LOCK:
            msgs.append({"role": "assistant", "content": content})
        if resp.get("stop_reason") == "refusal":
            reply = "I can't help with that one, {}.".format(CONFIG["owner_name"])
            break
        if resp.get("stop_reason") != "tool_use":
            reply = text_of(content) or "Done."
            break
        results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            out, card = run_manager_tool(block["name"], block.get("input") or {})
            if card:
                cards.append(card)
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": out})
        with LOCK:
            msgs.append({"role": "user", "content": results})
    else:
        reply = "That took too many steps. Try asking in a simpler way."
    with LOCK:
        chat.setdefault("log", []).append({"at": now_iso(), "me": user_text, "manager": reply})
        chat["log"] = chat["log"][-200:]
        persist()
    resolve_errors("claude:")
    return {"reply": reply, "cards": cards, "model": model}


def run_manager_tool(name, args):
    with LOCK:
        if name == "propose_schedule":
            rec = STATE["clips"].get(args.get("clip_id"))
            if not rec:
                return "No clip with that id.", None
            aid = uuid.uuid4().hex[:8]
            action = {"id": aid, "type": "schedule", "clip_id": rec["id"],
                      "time": args.get("time"), "platforms": args.get("platforms") or rec["platforms"],
                      "caption": args.get("caption") or rec.get("caption", ""),
                      "hashtags": args.get("hashtags") or rec.get("hashtags", ""),
                      "created": now_iso()}
            STATE["pending_actions"][aid] = action
            persist()
            card = dict(action, time_nice=nice_time(action["time"]),
                        thumb_url=clip_public(rec)["thumb_url"])
            return "Confirmation card shown. Waiting for him to press Confirm.", card
        if name == "skip_clip":
            rec = STATE["clips"].get(args.get("clip_id"))
            if not rec:
                return "No clip with that id.", None
            rec["status"] = "skipped"
            persist()
            return "Skipped.", None
        if name == "queue_video_link":
            ok, msg = add_link(args.get("url", ""))
            return msg, None
        if name == "request_redo":
            rec = STATE["clips"].get(args.get("clip_id"))
            if not rec:
                return "No clip with that id.", None
            write_redo(rec, args.get("note", ""))
            return "Redo queued for the next clipping run.", None
    return "Unknown tool.", None


# ---------------------------------------------------------------- captions

CAPTION_PROMPT = """Write social posts for these short vertical clips for the Stratus House \
account (niches: business, success, fitness, anti-aging, dogs, funny). For each clip return: \
caption = a hook line (first 2 seconds energy) + a short call to action, no hashtags inside; \
hashtags = 3-5 hashtags in one string; platforms = subset of ["instagram","youtube","tiktok"]; \
time = best local posting time as YYYY-MM-DDTHH:MM within the next 4 days, spaced at least \
one day apart, never earlier than {earliest}; why = one short reason for the time. No typos.

Reply with ONLY a JSON array of objects with keys id, caption, hashtags, platforms, time, why.

Clips:
{clips}"""


def write_captions(force_ids=None):
    with LOCK:
        todo = [r for r in STATE["clips"].values()
                if r.get("status") == "new" and not r.get("missing")
                and (force_ids is None or r["id"] in force_ids)]
    if not todo:
        return 0
    listing = "\n\n".join("id: {}\nsource video: {}\nnotes: {}".format(
        r["id"], r["source"], (r.get("notes") or "(no notes)")[:1200]) for r in todo[:6])
    lessons = hunter().get("lessons") or []
    if lessons and any(r.get("hunter") for r in todo[:6]):
        listing += "\n\nLessons from his past Clip Hunter posts (use them):\n- " + "\n- ".join(lessons)
    earliest = (now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    model = STATE["chat"].get("model") or CONFIG["default_model"]
    try:
        resp = call_claude({"max_tokens": 4000, "messages": [{"role": "user", "content":
                            CAPTION_PROMPT.format(earliest=earliest, clips=listing)}]}, model)
    except ClaudeError as exc:
        claude_error_to_alert("Caption Writer", exc)
        return 0
    raw = text_of(resp.get("content", []))
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        items = []
    if not items:
        raise_error("Caption Writer", "Captions came back unreadable",
                    "Claude wrote captions, but not in the format the dashboard reads.",
                    "Press “Write captions” on Your Desk to try again.", key="captions:parse")
        return 0
    done = 0
    with LOCK:
        for i, it in enumerate(items):
            rec = STATE["clips"].get(it.get("id"))
            if not rec or rec.get("status") != "new":
                continue
            rec["caption"] = it.get("caption", "").strip()
            rec["hashtags"] = it.get("hashtags", "").strip()
            pl = [p for p in it.get("platforms", []) if p in ("instagram", "youtube", "tiktok")]
            rec["platforms"] = pl or ["instagram", "youtube"]
            t = it.get("time") or ""
            rec["time"] = t if re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d$", t) else suggest_time(i)
            rec["why"] = it.get("why", "")
            rec["status"] = "ready"
            done += 1
        persist()
    resolve_errors("captions:")
    resolve_errors("claude:")
    return done


# ---------------------------------------------------------------- actions

def add_link(url, hide=False, game=False):
    url = (url or "").strip()
    if not re.match(r"^https?://\S+$", url):
        return False, "That doesn't look like a video link."
    inbox = CONFIG["clip_inbox"]
    try:
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "links.txt"), "a", encoding="utf-8") as fh:
            fh.write(url + "\n")
        if hide:
            set_hide("link", url, True)
        if game:
            set_hide("link", url, True, GAMEPLAY_FILE)
    except OSError as exc:
        raise_error("Inbox Clerk", "Couldn't save the link",
                    "Writing to Movies → Clip Inbox failed ({}).".format(exc),
                    "Ask Claude in your project chat to check the Clip Inbox folder.", key="inbox:write")
        return False, "Couldn't save it."
    return True, "Queued for the next clipping run." + (" The streamer's face and voice will be hidden." if hide else "") \
        + (" Only game play moments will be clipped." if game else "")


def write_redo(rec, note):
    rec["redo"] = {"note": note, "at": now_iso()}
    rec["status"] = "redo"
    path = os.path.join(CONFIG["clip_inbox"], "redo.json")
    data = load_json(path, [])
    data.append({"clip": rec["file"], "source": rec["source"], "num": rec.get("num"),
                 "note": note, "at": now_iso()})
    try:
        save_json(path, data)
    except OSError:
        pass
    persist()


def apply_schedule(clip_id, time_s, platforms, caption, hashtags, music, voice_text=None, voice=None, voice_when=None):
    rec = STATE["clips"].get(clip_id)
    if not rec:
        return False, "Clip not found."
    if not re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d$", time_s or ""):
        return False, "Pick a date and time."
    rec["time"] = time_s
    rec["platforms"] = [p for p in platforms if p in ("instagram", "youtube", "tiktok")]
    if caption is not None:
        rec["caption"] = caption
    if hashtags is not None:
        rec["hashtags"] = hashtags
    if music is not None:
        rec["music"] = music
        rec.pop("music_file", None)
    if voice_text is not None:
        rec["voice_text"] = voice_text.strip()[:400]
        rec.pop("music_file", None)
    if voice is not None:
        rec["voice"] = voice
    if voice_when in ("start", "end") and voice_when != rec.get("voice_when", "start"):
        rec["voice_when"] = voice_when
        rec.pop("music_file", None)
    rec["status"] = "scheduled"
    rec["approved_at"] = now_iso()
    if "tiktok" in rec["platforms"]:
        rec["tiktok_done"] = False
    persist()
    return True, "✅ Approved. Moved to Courier: it posts " + nice_time(time_s) + "."


# ---------------------------------------------------------------- state for UI

def worker(id_, name, status, doing, count=None):
    return {"id": id_, "name": name, "status": status, "doing": doing, "count": count}


def build_state(for_model=False):
    with LOCK:
        clips = [clip_public(r) for r in STATE["clips"].values() if not r.get("missing") or r.get("status") == "posted"]
        clips.sort(key=lambda c: (c.get("time") or "9999", c["id"]))
        waiting = [c for c in clips if c["status"] in ("new", "ready")]
        scheduled = [c for c in clips if c["status"] == "scheduled"]
        tiktok = [c for c in clips if c["status"] in ("scheduled", "posted", "partial")
                  and "tiktok" in (c.get("platforms") or []) and not c.get("tiktok_done")]
        posted = [c for c in clips if c["status"] in ("posted", "partial")]
        posted.sort(key=lambda c: c.get("posted_at") or "", reverse=True)
        redo = [c for c in clips if c["status"] == "redo"]
        errors = [e for e in STATE["errors"] if not e.get("resolved")]
        inbox = list_inbox()
        stats = summarize_stats()
        sales = sales_summary()
        used = posts_this_month()
        sec = secrets()

    err_workers = {e["worker"] for e in errors}

    def st(name, default):
        return "error" if name in err_workers else default

    gate_open = gate()
    running = clipper_running()
    nxt = next_window_open()
    next_post = scheduled[0] if scheduled else None
    sales_age = None
    if sales["imported_at"]:
        sales_age = (now() - datetime.strptime(sales["imported_at"][:10], "%Y-%m-%d")).days

    workers = [
        worker("inbox", "Inbox Clerk", st("Inbox Clerk", "ok" if inbox else "idle"),
               "{} in line to clip".format(len(inbox)) if inbox else "Inbox empty · drop a video or paste a link",
               len(inbox)),
        worker("clipper", "Clipper", st("Clipper", "ok" if running else ("idle" if gate_open else "off")),
               "Clipping now · up to 3 clips per video" if running else
               ("Open · waiting for videos" if gate_open else ("Closed · opens {}".format(nice_dt(nxt)) if nxt else "Closed · press OPEN to start")),
               len(redo) or None),
        worker("captions", "Caption Writer",
               st("Caption Writer", "ok" if any(c["status"] == "new" for c in waiting) else "idle"),
               "{} clip(s) need captions".format(sum(1 for c in waiting if c["status"] == "new"))
               if any(c["status"] == "new" for c in waiting) else "All captions written"),
        worker("desk", "Your Desk", st("Your Desk", "you" if waiting else "idle"),
               "{} clip(s) need Approve or Skip".format(len(waiting)) if waiting else "Nothing waiting on you",
               len(waiting)),
        worker("courier", "Courier", st("Courier", "ok" if scheduled else "idle"),
               "Next: {} · {}".format(", ".join(p.title() for p in next_post["platforms"] if p != "tiktok") or "TikTok only",
                                      next_post["time_nice"]) if next_post else "No posts scheduled"),
        worker("tiktok", "TikTok Runner", st("TikTok Runner", "you" if tiktok else "idle"),
               "{} clip(s) for you to post".format(len(tiktok)) if tiktok else "Nothing to post by hand", len(tiktok)),
        worker("analyst", "Analyst", st("Analyst", "ok" if stats["fetched_at"] else "idle"),
               "{:,} views tracked".format(stats["views"]) if stats["views"] is not None else
               ("Numbers start after your first post" if not posted else "Collecting numbers")),
        worker("scout", "Trend Scout", *scout_status()),
        worker("ugc", "UGC Director", st("UGC Director", "idle"),
               "{} book(s) ready for videos".format(len(STATE["books"])) if STATE["books"] else "Add your first coloring book"),
        worker("sales", "Sales Clerk",
               st("Sales Clerk", "you" if (sales_age is None or sales_age > 7) else "ok"),
               "Needs this week's KDP report" if (sales_age is None or sales_age > 7)
               else "{} sold · ${:,.2f} royalty".format(sales["units"], sales["royalty"])),
        worker("manager", "Social Media Manager",
               st("Social Media Manager", "ok" if sec.get("anthropic_key") else "you"),
               "Runs the floor · talk to me anytime" if sec.get("anthropic_key") else "Needs your Claude key in Settings"),
    ]

    out = {
        "owner": CONFIG["owner_name"],
        "brand": CONFIG["brand"],
        "now": now_iso(),
        "workers": workers,
        "gate": gate_public(),
        "inbox": inbox,
        "hide_ready": hide_ready(),
        "clips_waiting": waiting,
        "clips_scheduled": scheduled,
        "tiktok": tiktok,
        "posted": posted[:120],
        "redo": redo,
        "errors": errors,
        "posts_used": used,
        "posts_limit": CONFIG["upload_post_monthly_limit"],
        "tiktok_auto": bool(CONFIG.get("tiktok_auto")),
        "stats": stats,
        "sales": sales,
    }
    if for_model:
        return out
    out.update({
        "books": STATE["books"],
        "ugc": STATE["ugc"][-20:],
        "creations": [create_public(c) for c in STATE.get("creations", [])[-20:]][::-1],
        "warmup": warmup_public(STATE.get("warmup")),
        "hunter": hunter_public(),
        "trends": trends_public(),
        "warmup_history": STATE.get("warmup_history", [])[::-1],
        "music": list_music(),
        "music_ready": bool(find_ffmpeg()),
        "voices": voice_options(),
        "chat_model": STATE["chat"].get("model") or CONFIG["default_model"],
        "models": CONFIG["models"],
        "chat_log": STATE["chat"].get("log", [])[-30:],
        "pending_actions": [dict(a, time_nice=nice_time(a.get("time"))) for a in STATE["pending_actions"].values()],
        "settings": {
            "has_anthropic_key": bool(sec.get("anthropic_key")),
            "has_upload_post_key": bool(sec.get("upload_post_key")),
            "has_higgsfield_key": bool(hf_key()),
            "profile": CONFIG["upload_post_profile"],
            "dry_run": CONFIG.get("dry_run_posting", False),
            "stay_awake": bool(CONFIG.get("stay_awake", True)),
            "awake_active": awake_active(),
        },
        "past_errors": [e for e in STATE["errors"] if e.get("resolved")][:20],
    })
    return out


# ---------------------------------------------------------------- background loop

def background_loop():
    last_stats = 0.0
    last_captions_try = ""
    while True:
        try:
            scan_clips()
            check_autopilot_log()
            run_clipper_gate()
            # Captions: once new clips appear after the night run (or any time a key exists).
            stamp = now().strftime("%Y-%m-%d %H")
            has_new = any(r.get("status") == "new" and not r.get("missing") for r in STATE["clips"].values())
            if has_new and secrets().get("anthropic_key") and stamp != last_captions_try \
                    and not clipper_running():
                last_captions_try = stamp
                write_captions()
            # Post anything due.
            due = []
            with LOCK:
                for rec in STATE["clips"].values():
                    if rec.get("status") != "scheduled" or not rec.get("time"):
                        continue
                    when = rec.get("retry_at") or rec["time"]
                    if when <= now().strftime("%Y-%m-%dT%H:%M"):
                        due.append(rec)
            for rec in due:
                if rec["id"] in _POSTING or rec.get("status") != "scheduled":
                    continue
                rec.pop("retry_at", None)
                if any(p in auto_platforms() for p in rec.get("platforms", [])):
                    post_clip(rec)
                else:
                    rec["status"] = "posted"
                    rec["posted_at"] = now_iso()
                    persist()
                if "tiktok" in rec.get("platforms", []) and not rec.get("tiktok_done") and not CONFIG.get("tiktok_auto"):
                    notify_mac("Stratus House · TikTok Runner",
                               "Time to post clip {} on TikTok. Open the dashboard to copy the caption.".format(rec.get("num")))
            if trends_due():
                threading.Thread(target=fetch_trends, daemon=True).start()
            h = hunter()
            if h.get("on") and not _HUNT["running"]:
                last = h.get("last_run") or ""
                due_at = (now() - timedelta(hours=h.get("every", 2))).strftime("%Y-%m-%dT%H:%M:%S")
                if not last or last <= due_at:
                    threading.Thread(target=run_hunter, daemon=True).start()
            if time.time() - last_stats > 6 * 3600:
                last_stats = time.time()
                if STATE["posts"]:
                    fetch_stats()
                    fetch_post_stats()
                    hunter_lessons()
            persist()
        except Exception:  # noqa: BLE001 - the loop must keep running
            sys.stderr.write("[StratusMedia] loop error\n" + traceback.format_exc())
        time.sleep(60)


# ---------------------------------------------------------------- Trend Scout (nightly brief)
# The cloud Trend Scout writes each night's brief (8 PM Phoenix) as JSON to the trend feed.
# The dashboard pulls it and shows it in the Trend Scout sheet: table, 3 ideas, ★ pick.

_TRENDS = {"last_try": 0.0, "running": False}


def _clean_brief(raw):
    """Keep only the fields we show, as plain text (the page escapes them again).
    Shape: {report, date, generated_at, brief_url, concerns[], ideas[{rank, pick, niche, title, length,
    why, hook, caption, sound, post_time, build_plan[]}]}"""
    if not isinstance(raw, dict) or not raw.get("date"):
        return None
    txt = lambda v, n=600: str(v or "")[:n]  # noqa: E731
    ideas = []
    for i in (raw.get("ideas") or [])[:5]:
        if not isinstance(i, dict):
            continue
        plan = i.get("build_plan") or []
        ideas.append({"rank": txt(i.get("rank"), 4), "pick": bool(i.get("pick")),
                      "niche": txt(i.get("niche"), 60), "title": txt(i.get("title"), 160),
                      "length": txt(i.get("length"), 40), "why": txt(i.get("why"), 400),
                      "hook": txt(i.get("hook"), 300), "caption": txt(i.get("caption"), 700),
                      "sound": txt(i.get("sound"), 200), "post_time": txt(i.get("post_time"), 120),
                      "build_plan": [txt(x, 300) for x in plan[:10]] if isinstance(plan, list) else []})
    ideas.sort(key=lambda i: (not i["pick"], i["rank"]))
    link = txt(raw.get("brief_url"), 300)
    return {"report": txt(raw.get("report"), 6), "date": txt(raw.get("date"), 10),
            "made_at": txt(raw.get("generated_at"), 40),
            "concerns": [txt(c, 300) for c in (raw.get("concerns") or [])[:6]],
            "ideas": ideas, "link": link if link.startswith("https://") else ""}


def fetch_trends():
    if _TRENDS["running"]:
        return False
    _TRENDS["running"] = True
    _TRENDS["last_try"] = time.time()
    try:
        url = CONFIG.get("trend_feed")
        if not url:
            return False
        req = urllib.request.Request(url + "?t={}".format(int(time.time())),
                                     headers={"User-Agent": "StratusMedia", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            brief = _clean_brief(json.loads(resp.read().decode("utf-8")))
        if not brief:
            return False
        with LOCK:
            t = STATE.setdefault("trends", {"briefs": []})
            t["checked_at"] = now_iso()
            briefs = [b for b in t["briefs"] if b.get("date") != brief["date"]]
            new = len(briefs) == len(t["briefs"])
            briefs.append(brief)
            briefs.sort(key=lambda b: b["date"])
            t["briefs"] = briefs[-14:]
            persist()
        if new:
            pick = next((i for i in brief["ideas"] if i["pick"]), brief["ideas"][0] if brief["ideas"] else None)
            notify_mac("Stratus House · Trend Scout",
                       "Tonight's pick: {}".format(pick["title"]) if pick else "Tonight's trend brief is in.")
        return True
    except Exception as exc:  # noqa: BLE001 - try again later
        sys.stderr.write("[TrendScout] fetch failed: {}\n".format(exc))
        return False
    finally:
        _TRENDS["running"] = False


def trends_due():
    """After 8 PM: check every 15 min until tonight's brief is in. Otherwise every 3 hours."""
    if _TRENDS["running"]:
        return False
    age = time.time() - _TRENDS["last_try"]
    briefs = (STATE.get("trends") or {}).get("briefs") or []
    today = now().strftime("%Y-%m-%d")
    if now().hour >= 20 and not any(b.get("date") == today for b in briefs):
        return age > 15 * 60
    return age > 3 * 3600


def trends_public():
    t = STATE.get("trends") or {}
    briefs = t.get("briefs") or []
    return {"latest": briefs[-1] if briefs else None,
            "history": [{"date": b["date"], "report": b.get("report", ""),
                         "pick": next((i["title"] for i in b.get("ideas", []) if i.get("pick")), "")}
                        for b in reversed(briefs[:-1])][:7],
            "checked_at": t.get("checked_at"), "when": CONFIG.get("trend_brief")}


def scout_status():
    latest = trends_public()["latest"]
    if latest and latest["date"] == now().strftime("%Y-%m-%d"):
        pick = next((i["title"] for i in latest["ideas"] if i.get("pick")), "")
        return "ok", ("Tonight's pick: " + pick) if pick else "Tonight's brief is in"
    return "idle", "Next brief tonight 8:00 PM"


def r_trends_refresh(d):
    ok = fetch_trends()
    return {"ok": ok, "message": "Trend brief updated." if ok else "No new brief yet. It comes in around 8 PM."}


# ---------------------------------------------------------------- stay awake
# macOS `caffeinate -i` stops idle system sleep while the dashboard runs. The screen can still
# turn off. It ends by itself if the dashboard stops (-w). Closing the lid still sleeps a MacBook.

_CAFF = {"proc": None}


def awake_active():
    p = _CAFF["proc"]
    return bool(p and p.poll() is None)


def set_awake(on):
    if awake_active():
        _CAFF["proc"].terminate()
        _CAFF["proc"] = None
    exe = shutil.which("caffeinate") or ("/usr/bin/caffeinate" if os.path.exists("/usr/bin/caffeinate") else None)
    if on and exe:
        try:
            _CAFF["proc"] = subprocess.Popen([exe, "-i", "-w", str(os.getpid())],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            sys.stderr.write("[StayAwake] couldn't start caffeinate: {}\n".format(exc))
    return awake_active()


def r_stay_awake(d):
    on = bool(d.get("on"))
    CONFIG["stay_awake"] = on
    cfg = load_json(CONFIG_FILE, {})
    cfg["stay_awake"] = on
    save_json(CONFIG_FILE, cfg)
    active = set_awake(on)
    if on and not active:
        return {"ok": False, "message": "Couldn't keep the Mac awake (caffeinate not found)."}
    return {"ok": True, "message": "Your Mac stays awake now. The screen can still turn off." if on
            else "Your Mac can sleep again when idle."}


# ---------------------------------------------------------------- clipping gate runner
# OpenShorts (Docker) only runs while the gate is open AND there is something to clip.
# When a run finishes, or the gate closes, it is shut down so the trading desk keeps its memory.

RUN = {"proc": None, "queue_before": None, "cool_until": 0.0, "stopped": True}


def _external_run():
    """A clipping run started outside this process (e.g. before a dashboard restart)."""
    try:
        # Only a shell actually running the script counts (not an editor or a grep that mentions it).
        pat = r"^(/bin/|/usr/bin/)?(ba|z)?sh [^ ]*" + re.escape(os.path.basename(CONFIG["night_run"]))
        r = subprocess.run(["/usr/bin/pgrep", "-f", pat],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def clipper_running():
    p = RUN["proc"]
    if p and p.poll() is None:
        return True
    return p is None and _external_run()


def _redo_pending():
    try:
        with open(os.path.join(CONFIG["clip_inbox"], "redo.json"), "r", encoding="utf-8") as fh:
            return bool(json.load(fh))
    except (OSError, ValueError):
        return False


def _log_line(msg):
    try:
        with open(CONFIG["autopilot_log"], "a", encoding="utf-8") as fh:
            fh.write("[{}] [Dashboard] {}\n".format(now_iso(), msg))
    except OSError:
        pass


def stop_openshorts(reason):
    path = CONFIG["morning_stop"]
    if not os.path.exists(path):
        raise_error("Clipper", "Couldn't stop the clipper", "The stop script is missing ({}).".format(path),
                    "Ask Claude in your project chat.", key="gate:stopmissing")
        return
    _log_line("Stopping OpenShorts: " + reason)
    try:
        subprocess.run(["/bin/bash", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    except Exception:  # noqa: BLE001
        sys.stderr.write("[StratusMedia] stop error\n" + traceback.format_exc())
    RUN["stopped"] = True


def start_clipper():
    path = CONFIG["night_run"]
    if not os.path.exists(path):
        raise_error("Clipper", "Couldn't start clipping", "The clipping script is missing ({}).".format(path),
                    "Ask Claude in your project chat.", key="gate:runmissing")
        return
    env = dict(os.environ, AUTOPILOT_FORCE="1")
    _log_line("Starting clipping run (gate open)")
    log = open(CONFIG["autopilot_log"], "a", encoding="utf-8")
    RUN["proc"] = subprocess.Popen(["/usr/bin/nice", "-n", "10", "/bin/bash", path], env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    RUN["queue_before"] = [x.get("name") or x.get("url") for x in clip_work()]
    RUN["stopped"] = False


def run_clipper_gate():
    is_open = gate()
    proc = RUN["proc"]
    if proc and proc.poll() is not None:  # a run just finished
        RUN["proc"] = None
        left = [x.get("name") or x.get("url") for x in clip_work()]
        RUN["cool_until"] = time.time() + (3 * 3600 if left and left == RUN["queue_before"] else 15 * 60)
        stop_openshorts("run finished")
        return
    if not is_open:
        if clipper_running() or not RUN["stopped"]:
            stop_openshorts("clipping closed")
            RUN["proc"] = None
        return
    if clipper_running():
        RUN["stopped"] = False  # make sure OpenShorts is shut down once the run ends
        return
    if not RUN["stopped"] and RUN["proc"] is None:
        stop_openshorts("earlier run finished")
        return
    if time.time() < RUN["cool_until"]:
        return
    if clip_work() or _redo_pending():
        start_clipper()


def set_gate(action, override=False):
    g = STATE.setdefault("clip_gate", {"manual": None, "until": None})
    if action == "close":
        nxt = next_window_open()
        g["manual"], g["until"] = "closed", (nxt.strftime("%Y-%m-%dT%H:%M:%S") if nxt else None)
        persist()
        if clipper_running() or not RUN["stopped"]:
            threading.Thread(target=stop_openshorts, args=("closed by Mr. McDonald",), daemon=True).start()
            RUN["proc"] = None
        return {"ok": True, "message": ("Clipping closed. It stays closed until {}, or until you press Open.".format(nice_dt(nxt))
                                        if nxt else "Clipping closed. It stays closed until you press Open.")}
    if in_clip_window():
        g["manual"], g["until"] = None, None
        persist()
        RUN["cool_until"] = 0.0
        return {"ok": True, "message": "Clipping is open. It starts as soon as a video is in the Inbox."}
    if not override:
        return {"ok": False, "needs_override": True,
                "message": "It's trading hours. Clipping normally opens {}.".format(nice_dt(next_window_open()))}
    nxt = next_window_open()  # once the schedule opens, it simply carries on
    g["manual"], g["until"] = "open", (nxt.strftime("%Y-%m-%dT%H:%M:%S") if nxt else None)
    persist()
    RUN["cool_until"] = 0.0
    return {"ok": True, "message": "Override on. Clipping is open now and carries on with the normal schedule."}


def gate_public():
    g = STATE.get("clip_gate") or {}
    is_open = gate()
    return {"open": is_open, "running": clipper_running(), "manual": g.get("manual"),
            "in_window": in_clip_window(),
            "next_open": nice_dt(next_window_open()) if not is_open else "",
            "closes": nice_dt(_next_flip(now(), False)) if is_open and not g.get("manual") == "open" else "",
            "hours": "Any time · you open and close it"}


LOG_POS = {"pos": None}


def check_autopilot_log():
    """Turn new error lines in the clipper's log into plain-words alerts."""
    path = CONFIG["autopilot_log"]
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if LOG_POS["pos"] is None or size < LOG_POS["pos"]:
        LOG_POS["pos"] = size  # start from the end on first run
        return
    if size == LOG_POS["pos"]:
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(LOG_POS["pos"])
        new = fh.read()
    LOG_POS["pos"] = size
    for line in new.splitlines():
        low = line.lower()
        if not any(w in low for w in ("error", "failed", "traceback", "exception")):
            continue
        if "unable to find image" in low or "pull access denied" in low or "error response from daemon" in low \
                or "hide failed, left for retry" in low:
            continue   # Docker's own lines for a failed hide step; the HIDE: line explains it
        what, fix = explain_clipper_line(line)
        raise_error("Clipper", "Clipping hit a problem", what, fix, key="clip:" + fix[:40])


def explain_clipper_line(line):
    low = line.lower()
    if "gameplay:" in low and "no game play" in low:
        return ("A “Game play only” video had no moments that show the game (all desktop, web pages or chatting), so no clips were made.",
                "Nothing to do. Try a video with more game play.")
    if "gameplay: check failed" in low:
        return ("The game-play check couldn't run for one clip (Gemini didn't answer), so that clip was left out to be safe.",
                "Nothing to do. If it keeps happening, ask Claude in your project chat.")
    if "hide:" in low and "fills the screen" in low:
        return ("A video with “Hide the streamer” on was skipped: the streamer fills the whole screen, so he can't be hidden.",
                "Nothing to do. Pick videos where he's in a small webcam box, or turn Hide off for that one.")
    if "hide:" in low and "muted" in low:
        return ("His voice couldn't be split out of one clip, so that clip was made silent instead.",
                "Nothing to do. Add music at Your Desk if you like.")
    if "hide:" in low or "stratus-hide" in low:
        return ("Hiding the streamer didn't work this time, so the video was NOT clipped. It stays in line.",
                "Nothing to do. It tries again on the next run.")
    if "docker" in low and ("not running" in low or "cannot connect" in low or "is the docker daemon running" in low):
        return ("Docker (the engine the clipper runs in) didn't start.",
                "Open the Docker app once, then the clipper retries on its next run. Or ask Claude in your project chat.")
    if "429" in low or "quota" in low or "resource_exhausted" in low:
        return ("Gemini (picks the best moments) hit its free daily limit.",
                "Nothing to do. The video stays in line and runs again on the next run tomorrow.")
    if "yt-dlp" in low or "download" in low:
        return ("The video link couldn't be downloaded. It may be private, deleted or blocked.",
                "Check the link works in your browser, or download the video and drag it into the Inbox.")
    if "memory" in low or "oom" in low or "killed" in low:
        return ("The Mac ran out of memory while clipping.",
                "Use a shorter video (10–20 min works best). I'll try it again on the next run.")
    if "no space" in low or "disk" in low:
        return ("The Mac's storage is full.", "Empty the Trash or delete old clips, then it retries on the next run.")
    return ("The clipper's log says: " + line.strip()[:220],
            "Ask Claude in your project chat and paste this message.")


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "StratusMedia/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- helpers
    def send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def same_origin(self):
        # Block other web pages from driving the local API.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urllib.parse.urlparse(origin).netloc != host:
            return False
        return host.split(":")[0] in ("127.0.0.1", "localhost")

    def send_file(self, path, ctype=None):
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(404)
            return
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            elif b:
                start = size - int(b)
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(262144, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    # -- GET
    def do_GET(self):
        if not self.same_origin():
            self.send_error(403)
            return
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            return self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if url.path.startswith("/static/employees/"):
            name = os.path.basename(url.path)
            return self.send_file(os.path.join(STATIC_DIR, "employees", name))
        if url.path.startswith("/static/"):
            name = os.path.basename(url.path)
            return self.send_file(os.path.join(STATIC_DIR, name))
        if url.path == "/api/state":
            return self.send_json(build_state())
        if url.path == "/media":
            cid = (q.get("f") or [""])[0]
            rec = STATE["clips"].get(cid)
            if not rec:
                return self.send_error(404)
            if q.get("thumb"):
                return self.send_file(rec.get("thumb") or "")
            if q.get("music") and rec.get("music_file"):
                return self.send_file(rec["music_file"])
            return self.send_file(rec["file"])
        if url.path == "/music":
            name = os.path.basename((q.get("f") or [""])[0])
            return self.send_file(os.path.join(CONFIG["music_dir"], name))
        if url.path == "/book-cover":
            bid = (q.get("id") or [""])[0]
            book = next((b for b in STATE["books"] if b["id"] == bid), None)
            if not book or not book.get("cover"):
                return self.send_error(404)
            return self.send_file(book["cover"])
        if url.path == "/ugc-file":
            name = os.path.basename((q.get("f") or [""])[0])
            return self.send_file(os.path.join(CONFIG["ugc_dir"], name))
        self.send_error(404)

    # -- POST
    def do_POST(self):
        if not self.same_origin():
            self.send_error(403)
            return
        url = urllib.parse.urlparse(self.path)
        p = url.path
        try:
            if p == "/api/upload":
                return self.handle_upload(url)
            data = self.read_json()
            handler = ROUTES.get(p)
            if not handler:
                return self.send_error(404)
            result = handler(data)
            return self.send_json(result if isinstance(result, dict) else {"ok": True})
        except Exception as exc:  # noqa: BLE001 - always answer the page
            sys.stderr.write("[StratusMedia] " + traceback.format_exc())
            return self.send_json({"ok": False, "message": "Something broke: {}".format(exc)}, 500)

    def handle_upload(self, url):
        """Stream an uploaded file (video, KDP report, cover, UGC video) to disk."""
        q = urllib.parse.parse_qs(url.query)
        kind = (q.get("kind") or ["video"])[0]
        name = os.path.basename(urllib.parse.unquote((q.get("name") or ["upload"])[0]))
        name = re.sub(r"[^\w .()\-]", "_", name).strip() or "upload"
        n = int(self.headers.get("Content-Length") or 0)
        dest_dir = {
            "video": CONFIG["clip_inbox"],
            "kdp": os.path.join(DATA_DIR, "kdp"),
            "cover": os.path.join(DATA_DIR, "covers"),
            "ugc": CONFIG["ugc_dir"],
            "music": CONFIG["music_dir"],
        }.get(kind)
        if not dest_dir:
            return self.send_json({"ok": False, "message": "Unknown upload type."}, 400)
        os.makedirs(dest_dir, exist_ok=True)
        if kind == "cover":
            name = uuid.uuid4().hex[:8] + "_" + name
        dest = os.path.join(dest_dir, name)
        tmp = dest + ".part"
        left = n
        with open(tmp, "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(1048576, left))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
        if left > 0:
            os.remove(tmp)
            return self.send_json({"ok": False, "message": "The upload was cut off. Try again."}, 400)
        os.replace(tmp, dest)
        if kind == "kdp":
            return self.send_json(import_kdp(dest))
        if kind == "ugc":
            return self.send_json(add_ugc_video(dest, q))
        if kind == "video" and (q.get("hide") or [""])[0] == "1":
            set_hide("file", name, True)
        if kind == "video" and (q.get("game") or [""])[0] == "1":
            set_hide("file", name, True, GAMEPLAY_FILE)
        return self.send_json({"ok": True, "path": dest, "name": name,
                               "message": "Added to the Clip Inbox. It's clipped on the next run." if kind == "video" else "Saved."})


# ---------------------------------------------------------------- route handlers

def r_link(d):
    ok, msg = add_link(d.get("url"), bool(d.get("hide")), bool(d.get("game")))
    return {"ok": ok, "message": msg}


def r_approve(d):
    with LOCK:
        ok, msg = apply_schedule(d.get("id"), d.get("time"), d.get("platforms") or [],
                                 d.get("caption"), d.get("hashtags"), d.get("music"),
                                 d.get("voice_text"), d.get("voice"), d.get("voice_when"))
    return {"ok": ok, "message": msg}


def r_skip(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if rec:
            rec["status"] = "skipped"
            persist()
    return {"ok": True}


def r_unschedule(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if rec and rec.get("status") == "scheduled":
            rec["status"] = "ready"
            persist()
    return {"ok": True}


def r_redo(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if not rec:
            return {"ok": False, "message": "Clip not found."}
        write_redo(rec, d.get("note", ""))
    return {"ok": True, "message": "The Clipper will redo it on the next run."}


def r_tiktok_done(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if rec:
            rec["tiktok_done"] = True
            persist()
    return {"ok": True}


def r_reveal(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
    if not rec:
        return {"ok": False}
    path = rec.get("music_file") if rec.get("music_file") and os.path.exists(rec.get("music_file", "")) else rec["file"]
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    return {"ok": True}


def tiktok_post_one(rec):
    """Send one TikTok Runner clip to TikTok only (Instagram/YouTube untouched)."""
    with LOCK:
        if rec["id"] in _POSTING or rec.get("tiktok_done"):
            return True
        _POSTING.add(rec["id"])
    try:
        return _post_clip(rec, only=["tiktok"])
    finally:
        with LOCK:
            _POSTING.discard(rec["id"])


def r_tiktok_post_all(d):
    """He pressed "Post all to TikTok now": every clip waiting in the TikTok Runner goes to TikTok."""
    with LOCK:
        recs = [r for r in STATE["clips"].values()
                if r.get("status") in ("scheduled", "posted", "partial") and not r.get("missing")
                and "tiktok" in (r.get("platforms") or []) and not r.get("tiktok_done")]
    if not recs:
        return {"ok": True, "posted": 0, "failed": 0, "message": "Nothing waiting for TikTok."}
    if d.get("wait"):
        ok = sum(1 for r in recs if tiktok_post_one(r))
        bad = len(recs) - ok
        msg = "Posted {} clip(s) to TikTok.".format(ok) + (" {} didn't post. The red banner says why.".format(bad) if bad else "")
        return {"ok": not bad, "posted": ok, "failed": bad, "message": msg}
    threading.Thread(target=lambda: [tiktok_post_one(r) for r in recs], daemon=True).start()
    return {"ok": True, "posted": 0, "failed": 0,
            "message": "Sending {} clip(s) to TikTok in the background.".format(len(recs))}


def r_record_post(d):
    """Show a video that was posted outside the Approve flow (e.g. a finished skit posted
    straight through Upload-Post) in Live Posts, with its links and numbers."""
    f = d.get("file") or ""
    plats = [p for p in (d.get("platforms") or []) if p in ("instagram", "youtube", "tiktok")]
    if not os.path.isfile(f) or not plats:
        return {"ok": False, "message": "Need the video file and at least one platform."}
    cid = "outside/" + os.path.basename(f)
    ff = find_ffmpeg()
    thumb = os.path.splitext(f)[0] + ".jpg"
    if ff and not os.path.exists(thumb):
        subprocess.run([ff, "-y", "-loglevel", "error", "-ss", "1", "-i", f, "-frames:v", "1",
                        "-vf", "scale=360:-2", thumb], capture_output=True, timeout=60)
    urls = d.get("urls") or {}
    with LOCK:
        rec = STATE["clips"].get(cid) or {}
        rec.update({
            "id": cid, "source": d.get("source") or "Posted directly", "file": f, "num": 1,
            "thumb": thumb if os.path.exists(thumb) else None,
            "caption": d.get("caption") or rec.get("caption", ""), "hashtags": "",
            "platforms": plats, "status": "posted", "missing": False, "outside": True,
            "posted_at": d.get("posted_at") or rec.get("posted_at") or now_iso(),
            "request_ids": [str(x) for x in (d.get("request_ids") or rec.get("request_ids") or []) if x],
            "tiktok_done": True,
        })
        m = {k: v for k, v in (rec.get("metrics") or {}).items() if k in plats}
        rec["metrics"] = m
        for p, u in urls.items():
            if p in plats and str(u).startswith("https://"):
                m.setdefault(p, {})["url"] = u
        STATE["clips"][cid] = rec
        persist()
    threading.Thread(target=fetch_post_stats, daemon=True).start()
    return {"ok": True, "id": cid, "message": "Added to Live Posts."}


def r_set_caption(d):
    """Put a ready-made caption on a desk clip (no posting). Marks it "ready" so the Caption Writer
    won't overwrite it. Optional separate YouTube title."""
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if not rec:
            return {"ok": False, "message": "Clip not found."}
        if rec.get("status") not in ("new", "ready"):
            return {"ok": False, "message": "That clip is already approved or posted."}
        rec["caption"] = (d.get("caption") or "").strip()
        rec["hashtags"] = (d.get("hashtags") or "").strip()
        if "yt_title" in d:
            rec["yt_title"] = (d.get("yt_title") or "").strip()
        if not rec.get("platforms"):
            rec["platforms"] = list(auto_platforms())
        if not rec.get("time"):
            rec["time"] = suggest_time(0)
        rec["status"] = "ready"
        persist()
    return {"ok": True, "message": "Caption saved."}


def r_post_now(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if not rec:
            return {"ok": False, "message": "Clip not found."}
        if rec.get("status") in ("posted", "partial") or rec["id"] in _POSTING:
            return {"ok": True, "message": "Already posted (or posting right now)."}
        rec["status"] = "scheduled"
        rec["time"] = now().strftime("%Y-%m-%dT%H:%M")
        rec.pop("retry_at", None)
        persist()
    # Upload in the background so the dashboard never freezes while a big video uploads.
    threading.Thread(target=post_clip, args=(rec,), daemon=True).start()
    return {"ok": True, "message": "Posting now. It moves to Live Posts when it's done (about a minute)."}


def r_captions(d):
    if not secrets().get("anthropic_key"):
        claude_error_to_alert("Caption Writer", ClaudeError("nokey"))
        return {"ok": False, "message": "Add your Claude key in Settings first."}
    n = write_captions()
    return {"ok": True, "message": "Wrote captions for {} clip(s).".format(n)}


def r_music_preview(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if not rec:
            return {"ok": False, "message": "Clip not found."}
        rec["music"] = d.get("music") or "none"
        if d.get("voice_text") is not None:
            rec["voice_text"] = (d.get("voice_text") or "").strip()[:400]
        if d.get("voice") is not None:
            rec["voice"] = d.get("voice")
        if d.get("voice_when") in ("start", "end"):
            rec["voice_when"] = d.get("voice_when")
        if not wants_mix(rec):
            return {"ok": False, "message": "Pick music or type a voice line first."}
    try:
        mix_music(rec)
    except Exception as exc:  # noqa: BLE001
        music_error(rec, exc)
        return {"ok": False, "message": "Couldn't build the preview. The red banner says why."}
    persist()
    return {"ok": True, "url": clip_public(rec).get("music_url"), "track": rec.get("music_track")}


def r_chat(d):
    text = (d.get("text") or "").strip()
    if not text:
        return {"ok": False}
    return chat_turn(text)


def r_model(d):
    m = d.get("model")
    if m in CONFIG["models"]:
        with LOCK:
            STATE["chat"]["model"] = m
            persist()
    return {"ok": True, "model": STATE["chat"].get("model")}


def r_confirm(d):
    with LOCK:
        action = STATE["pending_actions"].pop(d.get("id"), None)
        if not action:
            return {"ok": False, "message": "That card expired."}
        if not d.get("yes"):
            persist()
            return {"ok": True, "message": "Cancelled."}
        ok, msg = apply_schedule(action["clip_id"], action["time"], action["platforms"],
                                 action["caption"], action["hashtags"], d.get("music"))
    return {"ok": ok, "message": msg}


def r_dismiss_error(d):
    with LOCK:
        for e in STATE["errors"]:
            if e["id"] == d.get("id"):
                e["resolved"] = True
        persist()
    return {"ok": True}


def r_retry(d):
    eid = d.get("error_id")
    with LOCK:
        err = next((e for e in STATE["errors"] if e["id"] == eid), None)
    if err and (err.get("action") or "").startswith("retry:"):
        cid = err["action"].split(":", 1)[1]
        err["resolved"] = True
        return r_post_now({"id": cid})
    return {"ok": False, "message": "Nothing to retry."}


LAST_POST_REFRESH = {"t": 0.0}


def r_live_refresh(d):
    if time.time() - LAST_POST_REFRESH["t"] < 600:
        return {"ok": True, "message": "Numbers were refreshed in the last 10 minutes. Try again a bit later."}
    LAST_POST_REFRESH["t"] = time.time()
    threading.Thread(target=lambda: (fetch_stats(), fetch_post_stats()), daemon=True).start()
    return {"ok": True, "message": "Getting fresh numbers from Instagram and YouTube. Give it a minute."}


def r_clip_gate(d):
    action = d.get("action")
    if action not in ("open", "close"):
        return {"ok": False, "message": "Unknown action."}
    return set_gate(action, bool(d.get("override")))


def r_settings(d):
    for field in ("anthropic_key", "upload_post_key", "higgsfield_key"):
        if field in d and d[field] is not None and str(d[field]).strip():
            save_secret(field, d[field])
            resolve_errors("claude:" if field == "anthropic_key" else "post:")
            if field == "higgsfield_key":
                ok, note = hf_check_key()
                if not ok:
                    return {"ok": False, "message": note}
                threading.Thread(target=reprice_plans, daemon=True).start()
                return {"ok": True, "message": "Saved. " + note}
    if d.get("clear"):
        save_secret(d["clear"], "")
    return {"ok": True, "message": "Saved. Keys stay hidden."}


def r_test_claude(d):
    try:
        resp = call_claude({"max_tokens": 50, "messages": [{"role": "user", "content": "Reply with just: ready"}]},
                           STATE["chat"].get("model") or CONFIG["default_model"])
        resolve_errors("claude:")
        return {"ok": True, "message": "Claude answered: " + (text_of(resp.get("content", [])) or "ok")[:40]}
    except ClaudeError as exc:
        claude_error_to_alert("Social Media Manager", exc)
        return {"ok": False, "message": "That didn't work. The red banner says why."}


def import_kdp(path):
    try:
        rows = parse_kdp(path)
    except (zipfile.BadZipFile, ET.ParseError, OSError, ValueError, KeyError) as exc:
        raise_error("Sales Clerk", "Couldn't read that KDP file",
                    "The file {} isn't a KDP report I can read ({}).".format(os.path.basename(path), exc.__class__.__name__),
                    "On KDP go to Reports → Prior Months' Royalties (or Orders), download the .xlsx and drop it here.",
                    key="kdp:read")
        return {"ok": False, "message": "Couldn't read that file. The red banner says why."}
    if not rows:
        raise_error("Sales Clerk", "No sales found in that file",
                    "{} opened fine but has no Title / Units / Royalty columns.".format(os.path.basename(path)),
                    "Download the royalties report (.xlsx) from KDP Reports and drop it here.", key="kdp:empty")
        return {"ok": False, "message": "No sales rows found."}
    with LOCK:
        STATE["sales"] = {"imported_at": now_iso(), "file": os.path.basename(path), "rows": rows}
        persist()
    resolve_errors("kdp:")
    s = sales_summary()
    return {"ok": True, "message": "Imported {} sales lines: {} books sold, ${:,.2f} royalty."
            .format(len(rows), s["units"], s["royalty"])}


def r_book(d):
    with LOCK:
        if d.get("delete"):
            STATE["books"] = [b for b in STATE["books"] if b["id"] != d["delete"]]
        else:
            title = (d.get("title") or "").strip()
            if not title:
                return {"ok": False, "message": "Give the book a name."}
            book = next((b for b in STATE["books"] if b["id"] == d.get("id")), None)
            if not book:
                book = {"id": uuid.uuid4().hex[:8], "added": now_iso()}
                STATE["books"].append(book)
            book["title"] = title
            book["amazon_url"] = (d.get("amazon_url") or "").strip()
            if d.get("cover"):
                book["cover"] = d["cover"]
        persist()
    return {"ok": True}


UGC_STYLES = {
    "flip": "Flip-through: hands slowly flip pages of the coloring book on a cozy table, close-ups of the line art, warm daylight.",
    "unboxing": "Unboxing: an Amazon package opens to reveal the coloring book, excited reaction, quick page peek.",
    "timelapse": "Coloring time-lapse: a page goes from black-and-white line art to fully colored with markers.",
    "review": "Talking review: a friendly creator holds the book to camera and says why they love it, 2-3 favorite pages.",
    "gift": "Gift idea: the book wrapped as a present with colored pencils, a kid or adult opens it happily.",
}


def r_ugc_brief(d):
    with LOCK:
        book = next((b for b in STATE["books"] if b["id"] == d.get("book_id")), None)
    if not book:
        return {"ok": False, "message": "Pick a book first."}
    style = d.get("style") if d.get("style") in UGC_STYLES else "flip"
    prompt = ("Vertical 9:16 UGC-style product video, 8-10 seconds, realistic phone footage. Product: the coloring book "
              "“{t}”{cover}. {s} Natural light, authentic, no on-screen text, no logos other than the book cover."
              ).format(t=book["title"], cover=" (use the attached cover image exactly)" if book.get("cover") else "",
                       s=UGC_STYLES[style])
    caption = "{} 🎨 Get it on Amazon (link in bio)".format(
        {"flip": "Flip through every page with me", "unboxing": "Look what just came in",
         "timelapse": "Watch this page come alive", "review": "My honest take",
         "gift": "The gift nobody expects but everyone loves"}[style])
    brief = {"id": uuid.uuid4().hex[:8], "book_id": book["id"], "book": book["title"], "style": style,
             "prompt": prompt, "caption": caption, "hashtags": "#coloringbook #adultcoloring #giftideas #amazonfinds",
             "created": now_iso(), "status": "brief"}
    with LOCK:
        STATE["ugc"].append(brief)
        persist()
    return {"ok": True, "brief": brief}


def add_ugc_video(path, q):
    """A finished UGC video goes to Your Desk like any clip."""
    bid = (q.get("brief") or [""])[0]
    with LOCK:
        brief = next((b for b in STATE["ugc"] if b["id"] == bid), None)
        cid = "UGC/" + os.path.basename(path)
        STATE["clips"][cid] = {
            "id": cid, "source": "UGC", "file": path, "thumb": None, "num": 0,
            "notes": "UGC video for " + (brief["book"] if brief else "a coloring book"),
            "mtime": time.time(), "status": "ready" if brief else "new",
            "caption": brief["caption"] if brief else "", "hashtags": brief["hashtags"] if brief else "",
            "platforms": ["instagram", "youtube", "tiktok"], "time": suggest_time(0),
            "why": "Evenings get the most shopping traffic.", "music": "auto", "added": now_iso(),
        }
        if brief:
            brief["status"] = "made"
            book = next((b for b in STATE["books"] if b["id"] == brief["book_id"]), None)
            if book and book.get("amazon_url"):
                STATE["clips"][cid]["caption"] += "\n" + book["amazon_url"]
        persist()
    return {"ok": True, "message": "Sent to Your Desk for your OK."}



# ---------------------------------------------------------------- Create (Higgsfield API)
# Uses Mr. McDonald's Higgsfield API key (console.higgsfield.ai, prepaid USD balance).
# Nothing is spent until he presses "Make it" after seeing the price.

HF_API = "https://api.higgsfield.ai"
HF_MODEL = "kling-video/v2.6/pro"

CREATE_SYSTEM = (
    "You turn Mr. McDonald's plain-words video idea into a plan for an AI video generator (Kling) that makes "
    "one 5- or 10-second shot at a time; longer videos are several shots joined in order. "
    "His brand is Stratus House. He sells coloring books on Amazon KDP and posts short vertical videos. "
    "Reply with JSON only, no other text: {\"title\": short name (max 6 words), "
    "\"scenes\": a list with EXACTLY the number of shots asked for, each one detailed paragraph for the video model "
    "(subject, action, setting, camera move, lighting; realistic phone-footage look for UGC; vertical 9:16; NO on-screen "
    "text, captions, logos or watermarks; describe only what is visible). The video model does not remember earlier shots, "
    "so repeat the same exact description of every person, animal, product, outfit, place and lighting in every shot. "
    "The shots must flow as one story with a strong visual hook in shot 1, "
    "\"caption\": a post caption with a hook in the first line and a short call to action, "
    "\"hashtags\": 3-5 hashtags in one string}.")

LENGTHS = {5: [5], 10: [10], 20: [10, 10], 30: [10, 10, 10], 60: [10] * 6}


def hf_key():
    return (secrets().get("higgsfield_key") or "").strip()


class HFError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(detail)
        self.code, self.detail = code, detail


def hf_call(method, path_or_url, body=None, idem=None, timeout=60):
    key = hf_key()
    if not key:
        raise HFError("nokey")
    url = path_or_url if path_or_url.startswith("http") else HF_API + path_or_url
    headers = {"Authorization": "Key " + key, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if idem:
        headers["Idempotency-Key"] = idem
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise HFError(exc.code, exc.read().decode("utf-8", "replace")[:300])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HFError("net", str(exc))


def hf_check_key():
    """Ask Higgsfield about a made-up request: 404 means the key works, 401/403 means it doesn't. Costs nothing."""
    try:
        hf_call("GET", "/requests/00000000-0000-0000-0000-000000000000/status", timeout=15)
        return True, "Higgsfield accepted the key ✅"
    except HFError as exc:
        if exc.code in (401, 403):
            return False, "Higgsfield didn't accept that key. Copy it again with the Copy API key button and paste it in the first box."
        if exc.code == "net":
            return True, "Couldn't reach Higgsfield to test it right now. It will be checked when you make a video."
        return True, "Higgsfield accepted the key ✅"


def hf_plain(exc):
    """Higgsfield problems in plain words."""
    if exc.code == "nokey":
        return "Your Higgsfield API key isn't saved yet. Open Settings and add it."
    if exc.code == "net":
        return "Couldn't reach Higgsfield. Check your Wi-Fi and try again."
    if exc.code in (401, 403):
        return "Higgsfield didn't accept the API key. Open Settings and paste it again."
    if exc.code == 402 or "balance" in exc.detail.lower() or "credit" in exc.detail.lower():
        return "Your Higgsfield API balance is too low. Add money at console.higgsfield.ai (Billing), then try again."
    if exc.code == 429:
        return "Higgsfield is busy. Wait a minute and try again."
    if isinstance(exc.code, int) and exc.code >= 500:
        return "Higgsfield's servers are having trouble. Nothing was charged. Try again in a few minutes."
    return "Higgsfield said no (error {}): {}".format(exc.code, exc.detail[:160])


def scene_body(prompt, duration, image_url=None):
    body = {"prompt": prompt, "duration": duration, "cfg_scale": 0.5, "sound": "off"}
    if image_url:
        body["image_url"] = image_url
    else:
        body["aspect_ratio"] = "9:16"
    return body


def hf_upload_cover(path):
    """Put the cover somewhere Higgsfield can read it. Returns None if that isn't possible."""
    ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
    try:
        up = hf_call("POST", "/files/generate-upload-url", {"content_type": ctype})
        with open(path, "rb") as fh:
            data = fh.read()
        hdrs = dict(up.get("upload_headers") or {})
        hdrs.setdefault("Content-Type", ctype)
        req = urllib.request.Request(up["upload_url"], data=data, headers=hdrs, method="PUT")
        with urllib.request.urlopen(req, timeout=120):
            pass
        return up.get("public_url")
    except (HFError, KeyError, OSError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def hf_estimate(c):
    """Total USD from Higgsfield's estimate endpoint, or None if it doesn't give one."""
    total = 0.0
    cache = {}
    try:
        for sc in c["scenes"]:
            dur = sc["duration"]
            if dur not in cache:
                usd = hf_call("POST", "/estimate/" + HF_MODEL + "/text-to-video",
                              scene_body(sc["prompt"], dur), timeout=30).get("usd")
                cache[dur] = float(usd)
            total += cache[dur]
        return round(total, 2)
    except (HFError, ValueError, TypeError):
        return None


def reprice_plans():
    for c in STATE.get("creations", []):
        if c.get("status") in ("plan", "failed") and c.get("usd") is None and c.get("scenes"):
            c["usd"] = hf_estimate(c)
    persist()


def _parse_plan(text, n):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json")
    plan = json.loads(m.group(0))
    scenes = plan.get("scenes") or ([plan["prompt"]] if plan.get("prompt") else [])
    scenes = [str(x).strip() for x in scenes if str(x).strip()]
    if not scenes or not str(plan.get("caption") or "").strip():
        raise ValueError("incomplete")
    while len(scenes) < n:
        scenes.append(scenes[-1])
    return {"title": str(plan.get("title") or "New video").strip(), "caption": str(plan["caption"]).strip(),
            "hashtags": str(plan.get("hashtags") or "").strip()}, scenes[:n]


def _migrate(c):
    """Plans made before multi-scene support had one prompt."""
    if not c.get("scenes"):
        c["scenes"] = [{"prompt": (c.get("plan") or {}).get("prompt", ""), "duration": c.get("duration", 5)}]
    return c


def create_public(c):
    _migrate(c)
    out = {k: c.get(k) for k in ("id", "kind", "ask", "book", "duration", "use_cover", "usd", "status",
                                 "error", "created", "done_at", "clip_id", "stage")}
    out["plan"] = {k: (c.get("plan") or {}).get(k) for k in ("title", "caption", "hashtags")}
    out["scenes"] = [{"prompt": sc["prompt"], "duration": sc["duration"], "done": bool(sc.get("file"))} for sc in c["scenes"]]
    if c.get("file"):
        out["video_url"] = "/ugc-file?f=" + urllib.parse.quote(os.path.basename(c["file"]))
    return out


def r_create_plan(d):
    ask = (d.get("ask") or "").strip()
    if len(ask) < 4:
        return {"ok": False, "message": "Type a few words about the video you want."}
    kind = "promo" if d.get("kind") == "promo" else "original"
    try:
        total = int(d.get("duration") or 5)
    except (TypeError, ValueError):
        total = 5
    durs = LENGTHS.get(total, [5])
    total = sum(durs)
    book = None
    if kind == "promo":
        with LOCK:
            book = next((b for b in STATE["books"] if b["id"] == d.get("book_id")), None)
        if not book:
            return {"ok": False, "message": "Pick which coloring book this promo is for (add one in the UGC Director first)."}
    use_cover = bool(book and book.get("cover") and d.get("use_cover", True))
    user = "Video type: {}.\nTotal length: {} seconds = {} shot(s) of {} seconds each, in order.\nHis idea: {}".format(
        "UGC promo for his coloring book “{}”{}".format(
            book["title"], " (shot 1 starts from a photo of the book cover; describe how the scene moves from it)"
            if use_cover else "") if book else "original short video for his channels",
        total, len(durs), " / ".join(str(x) for x in durs), ask[:1500])
    try:
        resp = call_claude({"max_tokens": 900 + 450 * len(durs), "system": CREATE_SYSTEM,
                            "messages": [{"role": "user", "content": user}]},
                           STATE["chat"].get("model") or CONFIG["default_model"])
        plan, prompts = _parse_plan(text_of(resp.get("content", [])), len(durs))
    except ClaudeError as exc:
        claude_error_to_alert("UGC Director", exc)
        return {"ok": False, "message": "Your manager couldn't write the plan. See the alert at the top."}
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "message": "The plan came back garbled. Press Plan it again."}
    if book and book.get("amazon_url"):
        plan["caption"] += "\n" + book["amazon_url"]
    c = {"id": uuid.uuid4().hex[:8], "kind": kind, "ask": ask, "duration": total,
         "book_id": book["id"] if book else None, "book": book["title"] if book else None,
         "use_cover": use_cover, "plan": plan, "status": "plan", "created": now_iso(),
         "scenes": [{"prompt": p, "duration": du} for p, du in zip(prompts, durs)]}
    c["usd"] = hf_estimate(c) if hf_key() else None
    with LOCK:
        STATE.setdefault("creations", []).append(c)
        STATE["creations"] = STATE["creations"][-40:]
        persist()
    return {"ok": True, "creation": create_public(c)}


def r_create_edit(d):
    with LOCK:
        c = next((x for x in STATE.get("creations", []) if x["id"] == d.get("id")), None)
        if not c or c["status"] not in ("plan", "failed"):
            return {"ok": False, "message": "That video can't be changed now."}
        _migrate(c)
        if d.get("prompt") and str(d.get("scene", "")).isdigit():
            i = int(d["scene"])
            if 0 <= i < len(c["scenes"]):
                sc = c["scenes"][i]
                sc["prompt"] = str(d["prompt"]).strip()[:2500]
                for k in ("file", "request_id", "status_url"):
                    sc.pop(k, None)   # changed scene gets made again
        if d.get("caption") is not None:
            c["plan"]["caption"] = str(d["caption"]).strip()[:2200]
        persist()
    return {"ok": True}


def r_create_make(d):
    if not hf_key():
        return {"ok": False, "message": "Add your Higgsfield API key in Settings first.", "need_key": True}
    with LOCK:
        c = next((x for x in STATE.get("creations", []) if x["id"] == d.get("id")), None)
        if not c:
            return {"ok": False, "message": "That plan is gone. Make a new one."}
        _migrate(c)
        if c["status"] in ("submitting", "making", "joining", "done"):
            return {"ok": False, "message": "That one is already being made."}
        if len(c["scenes"]) > 1 and not find_ffmpeg():
            return {"ok": False, "message": "Joining scenes needs ffmpeg on your Mac, and it's missing. Ask Claude in your project chat to install it."}
        c["status"], c["error"] = "submitting", None
        c["attempt"] = c.get("attempt", 0) + 1
        for sc in c["scenes"]:
            if not (sc.get("file") and os.path.exists(sc["file"])):
                for k in ("file", "request_id", "status_url"):
                    sc.pop(k, None)   # finished scenes are kept, so a retry only pays for what failed
        persist()
    threading.Thread(target=_create_run, args=(c,), daemon=True).start()
    n = len(c["scenes"])
    return {"ok": True, "message": "Making it now. About 2 to 6 minutes{}, then it lands at Your Desk.".format(
        "" if n == 1 else " (all {} scenes are made at the same time)".format(n))}


def r_create_delete(d):
    with LOCK:
        before = len(STATE.get("creations", []))
        STATE["creations"] = [x for x in STATE.get("creations", [])
                              if not (x["id"] == d.get("id") and x["status"] not in ("submitting", "making", "joining"))]
        persist()
        return {"ok": len(STATE["creations"]) < before}


def _create_fail(c, msg):
    with LOCK:
        c["status"], c["error"], c["stage"] = "failed", msg, None
        persist()
    notify_mac("Stratus House · Create", "A video didn't finish: " + msg[:120])


def _parts_dir(c):
    d = os.path.join(DATA_DIR, "create-parts", c["id"])
    os.makedirs(d, exist_ok=True)
    return d


def _create_run(c):
    try:
        _migrate(c)
        image_url = None
        if c.get("use_cover") and not c["scenes"][0].get("request_id") and not c["scenes"][0].get("file"):
            with LOCK:
                book = next((b for b in STATE["books"] if b["id"] == c.get("book_id")), None)
            if book and book.get("cover") and os.path.exists(book["cover"]):
                image_url = hf_upload_cover(book["cover"])
        # 1) submit every scene that isn't finished yet
        for i, sc in enumerate(c["scenes"]):
            if sc.get("file") or sc.get("request_id"):
                continue
            img = image_url if i == 0 else None
            endpoint = HF_MODEL + ("/image-to-video" if img else "/text-to-video")
            for attempt in range(4):
                try:
                    r = hf_call("POST", "/" + endpoint, scene_body(sc["prompt"], sc["duration"], img),
                                idem="stratus-{}-{}-{}".format(c["id"], i, c["attempt"]))
                    break
                except HFError as exc:
                    if exc.code == 429 and attempt < 3:
                        time.sleep(20)
                        continue
                    raise
            with LOCK:
                sc["request_id"], sc["status_url"] = r.get("request_id"), r.get("status_url")
                c["status"] = "making"
                persist()
            time.sleep(1)
        with LOCK:
            c["status"] = "making"
            persist()
        # 2) wait for all of them and download each one
        _create_poll_all(c)
        if c.get("status") == "failed":
            return
        # 3) join (or just keep the one scene) and send to Your Desk
        _create_finish(c)
    except HFError as exc:
        _create_fail(c, hf_plain(exc))
    except Exception as exc:  # never kill the server over one video
        traceback.print_exc()
        _create_fail(c, "Something unexpected went wrong ({}).".format(str(exc)[:80]))


def _download(url, dest):
    tmp = dest + ".part"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh, 1048576)
            os.replace(tmp, dest)
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(10)
    return False


def _create_poll_all(c):
    wait, started = 3.0, time.time()
    n = len(c["scenes"])
    while time.time() - started < 45 * 60:
        pending = [(i, sc) for i, sc in enumerate(c["scenes"]) if not sc.get("file")]
        if not pending:
            return
        time.sleep(wait)
        wait = min(wait * 1.5, 12.0)
        for i, sc in pending:
            url = sc.get("status_url") or "{}/requests/{}/status".format(HF_API, sc["request_id"])
            try:
                r = hf_call("GET", url, timeout=30)
            except HFError as exc:
                if exc.code in ("net", 429) or (isinstance(exc.code, int) and exc.code >= 500):
                    continue
                raise
            st = r.get("status")
            if st == "completed":
                vurl = (r.get("video") or {}).get("url")
                dest = os.path.join(_parts_dir(c), "scene{}.mp4".format(i + 1))
                if not vurl or not _download(vurl, dest):
                    return _create_fail(c, "Scene {} was made but the download failed. It's kept at Higgsfield for 7 days. Press Try again (finished scenes aren't paid for twice).".format(i + 1))
                with LOCK:
                    sc["file"] = dest
                    done = sum(1 for x in c["scenes"] if x.get("file"))
                    c["stage"] = "Scene {} of {} done".format(done, n) if n > 1 else None
                    persist()
            elif st in ("failed", "nsfw", "canceled"):
                with LOCK:
                    sc.pop("request_id", None)
                    sc.pop("status_url", None)
                which = "" if n == 1 else "Scene {}: ".format(i + 1)
                return _create_fail(c, which + {
                    "nsfw": "Higgsfield's safety filter blocked it. Nothing was charged for it. Reword that part and press Try again.",
                    "canceled": "It was canceled. Nothing was charged for it."}.get(
                    st, "Higgsfield couldn't make it. Nothing was charged for it. Press Try again."))
    _create_fail(c, "It took over 45 minutes, so I stopped waiting. Press Try again (finished scenes are kept).")


def _join(files, dest):
    """Join scenes with no re-encoding (light on the Mac). Re-encode gently only if that fails."""
    ff = find_ffmpeg()
    lst = dest + ".txt"
    with open(lst, "w", encoding="utf-8") as fh:
        for f in files:
            fh.write("file '{}'\n".format(f.replace("'", "'\\''")))
    nice = ["nice", "-n", "15"] if shutil.which("nice") else []
    tries = [
        [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", dest],
        nice + [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "19", "-pix_fmt", "yuv420p", "-threads", "2", "-an", "-movflags", "+faststart", dest],
    ]
    try:
        for cmd in tries:
            try:
                if subprocess.run(cmd, capture_output=True, timeout=900).returncode == 0 and os.path.getsize(dest) > 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        return False
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass


def _create_finish(c):
    os.makedirs(CONFIG["ugc_dir"], exist_ok=True)
    slug = re.sub(r"[^\w\-]+", "-", c["plan"]["title"]).strip("-")[:40] or "video"
    dest = os.path.join(CONFIG["ugc_dir"], "{}-{}.mp4".format(slug, c["id"]))
    files = [sc["file"] for sc in c["scenes"]]
    if len(files) == 1:
        shutil.copyfile(files[0], dest)
    else:
        with LOCK:
            c["status"], c["stage"] = "joining", "Joining {} scenes into one video".format(len(files))
            persist()
        if not _join(files, dest):
            return _create_fail(c, "All scenes were made, but joining them failed. Press Try again (you won't pay again).")
    cid = "UGC/" + os.path.basename(dest)
    with LOCK:
        STATE["clips"][cid] = {
            "id": cid, "source": "UGC", "file": dest, "thumb": None, "num": 0,
            "notes": ("Promo for " + c["book"]) if c.get("book") else ("Original: " + c["plan"]["title"]),
            "mtime": time.time(), "status": "ready",
            "caption": c["plan"]["caption"], "hashtags": c["plan"]["hashtags"],
            "platforms": ["instagram", "youtube", "tiktok"], "time": suggest_time(0),
            "why": "Evenings get the most traffic.", "music": "auto", "added": now_iso(),
        }
        c.update(status="done", file=dest, clip_id=cid, done_at=now_iso(), stage=None)
        persist()
    shutil.rmtree(os.path.join(DATA_DIR, "create-parts", c["id"]), ignore_errors=True)
    notify_mac("Stratus House · Create", "Your video “{}” is ready at Your Desk.".format(c["plan"]["title"]))


def resume_creations():
    for c in STATE.get("creations", []):
        _migrate(c)
        if c.get("status") in ("submitting", "making", "joining"):
            if any(sc.get("request_id") or sc.get("file") for sc in c["scenes"]):
                c["status"] = "making"
                threading.Thread(target=_create_run, args=(c,), daemon=True).start()
            else:
                c["status"], c["error"] = "failed", "The dashboard restarted before it started. Nothing was charged. Press Try again."


# ---------------------------------------------------------------- Warm-Up (14 days, by hand)

WARMUP_DAYS = 14
WARMUP_SYSTEM = (
    "You coach Mr. McDonald (brand: Stratus House) through a 14-day account warm-up for a new niche on short-video apps. "
    "He does every step BY HAND on his phone: no bots, no automation, no follow-for-follow, no copy-paste spam. "
    "Each day must take about 10 minutes. Keep it safe for the apps: at most 10 follows and 10 comments a day, "
    "real comments that add something, no links in comments. Plain words, short lines, no jargon. "
    "Reply with JSON only, no other text: {"
    "\"hashtags\": 8 hashtags to search for this niche (strings with #), "
    "\"accounts\": 5 short descriptions of the kinds of accounts to follow (e.g. 'small dog trainers posting daily'), "
    "\"comments\": 8 short, genuine comment ideas he can adapt (no emojis spam, no 'follow me'), "
    "\"days\": a list of EXACTLY 14 days, each a list of 4 to 6 tasks; each task is {\"t\": one short instruction, \"m\": minutes as a number}; "
    "the minutes for each day add up to about 10. Days 1-3: set up the profile for the niche and teach the feed by watching and saving; "
    "days 4-10: search hashtags, follow a few, leave real comments, reply to comments; days 11-14: keep engaging and post or share niche content. "
    "Mention which app only when it matters.}")


def warmup_day(w):
    try:
        start = datetime.strptime(w["started"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return 1
    return max(1, (now().date() - start).days + 1)


def warmup_numbers():
    st = summarize_stats()
    return {"followers": dict(st.get("followers") or {}), "views": st.get("views"), "at": now_iso()}


def warmup_public(w):
    if not w:
        return None
    day = warmup_day(w)
    out = {k: w.get(k) for k in ("niche", "platforms", "started", "hashtags", "accounts", "comments", "before", "manual", "status")}
    out["day"] = min(day, WARMUP_DAYS)
    out["over"] = day > WARMUP_DAYS
    out["days"] = []
    for i, tasks in enumerate(w["days"], 1):
        done = sum(1 for t in tasks if t.get("done"))
        state = "done" if done == len(tasks) else ("today" if i == day else ("missed" if i < day else "later"))
        out["days"].append({"n": i, "tasks": tasks, "done": done, "state": state})
    streak = 0
    for i in range(min(day, WARMUP_DAYS), 0, -1):
        if out["days"][i - 1]["state"] == "done":
            streak += 1
        elif i == day:
            continue  # today isn't finished yet: it doesn't break the streak
        else:
            break
    out["streak"] = streak
    out["now"] = warmup_numbers()
    return out


def r_warmup_start(d):
    niche = (d.get("niche") or "").strip()[:120]
    if len(niche) < 3:
        return {"ok": False, "message": "Type the niche first, like “dog training” or “anti-aging”."}
    plats = [p for p in (d.get("platforms") or []) if p in ("instagram", "tiktok", "youtube")] or ["instagram", "tiktok"]
    user = "New niche: {}\nApps he is warming up: {}".format(niche, ", ".join(plats))
    try:
        resp = call_claude({"max_tokens": 3500, "system": WARMUP_SYSTEM,
                            "messages": [{"role": "user", "content": user}]},
                           STATE["chat"].get("model") or CONFIG["default_model"])
        txt = text_of(resp.get("content", []))
        m = re.search(r"\{.*\}", txt, re.S)
        plan = json.loads(m.group(0)) if m else {}
        days = []
        for day in (plan.get("days") or [])[:WARMUP_DAYS]:
            tasks = []
            for t in day[:6]:
                if isinstance(t, dict) and str(t.get("t") or "").strip():
                    try:
                        mins = max(1, min(10, int(float(t.get("m") or 2))))
                    except (TypeError, ValueError):
                        mins = 2
                    tasks.append({"t": str(t["t"]).strip()[:200], "m": mins, "done": False})
                elif isinstance(t, str) and t.strip():
                    tasks.append({"t": t.strip()[:200], "m": 2, "done": False})
            if tasks:
                days.append(tasks)
        if len(days) < WARMUP_DAYS:
            raise ValueError("short")
    except ClaudeError as exc:
        claude_error_to_alert("Warm-Up Coach", exc)
        return {"ok": False, "message": "Your manager couldn't write the plan. See the alert at the top."}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"ok": False, "message": "The plan came back garbled. Press Start again."}
    w = {"niche": niche, "platforms": plats, "started": now().strftime("%Y-%m-%d"), "status": "on",
         "hashtags": [str(x).strip()[:60] for x in (plan.get("hashtags") or [])][:10],
         "accounts": [str(x).strip()[:160] for x in (plan.get("accounts") or [])][:6],
         "comments": [str(x).strip()[:200] for x in (plan.get("comments") or [])][:10],
         "days": days, "before": warmup_numbers(), "manual": {}}
    with LOCK:
        old = STATE.get("warmup")
        if old:
            STATE.setdefault("warmup_history", []).append(_warmup_summary(old))
        STATE["warmup"] = w
        persist()
    return {"ok": True, "message": "Day 1 is ready. About 10 minutes."}


def _warmup_summary(w):
    pub = warmup_public(w)
    return {"niche": w["niche"], "started": w["started"], "ended": now().strftime("%Y-%m-%d"),
            "days_done": sum(1 for x in pub["days"] if x["state"] == "done"),
            "before": w.get("before"), "after": pub["now"], "manual": w.get("manual")}


def r_warmup_check(d):
    with LOCK:
        w = STATE.get("warmup")
        if not w:
            return {"ok": False}
        try:
            task = w["days"][int(d.get("day")) - 1][int(d.get("i"))]
        except (TypeError, ValueError, IndexError):
            return {"ok": False}
        task["done"] = bool(d.get("done"))
        persist()
    return {"ok": True}


def r_warmup_log(d):
    """He types his follower count for an app (TikTok isn't tracked automatically)."""
    plat = d.get("platform")
    try:
        n = int(str(d.get("followers")).replace(",", "").strip())
    except (TypeError, ValueError):
        return {"ok": False, "message": "Type just the number, like 152."}
    if plat not in ("instagram", "tiktok", "youtube") or n < 0:
        return {"ok": False}
    with LOCK:
        w = STATE.get("warmup")
        if not w:
            return {"ok": False}
        rec = w.setdefault("manual", {}).setdefault(plat, {})
        rec.setdefault("first", n)
        rec["first_at"] = rec.get("first_at") or now().strftime("%Y-%m-%d")
        rec["latest"], rec["latest_at"] = n, now().strftime("%Y-%m-%d")
        persist()
    return {"ok": True, "message": "Saved."}


def r_warmup_stop(d):
    with LOCK:
        w = STATE.get("warmup")
        if w:
            STATE.setdefault("warmup_history", []).append(_warmup_summary(w))
            STATE["warmup_history"] = STATE["warmup_history"][-10:]
            STATE["warmup"] = None
            persist()
    return {"ok": True, "message": "Warm-up finished and saved."}


# ---------------------------------------------------------------- Clip Hunter
# Every few hours: find hot new clips for his subject on Twitch or YouTube, skip ones other pages
# already reposted, and drop the best few into the Clip Inbox. The normal clipper edits them
# (captions, hook, facecam layout) and they land at Your Desk. Nothing posts without his OK.

HUNT_MAX_ITEMS = 80
HUNT_INBOX_CAP = 4          # don't pile more than this many hunter links in the inbox at once
_HUNT = {"running": False, "token": None, "token_exp": 0.0}

HUNT_PROFILE_SYSTEM = (
    "You help a short-video clipping tool. Given a subject he typed, reply with JSON only: "
    "{\"is_game\": true if the subject is a specific video game (or a game series), "
    "\"twitch_category\": the exact Twitch category name for that game (e.g. 'Grand Theft Auto VI'), or null, "
    "\"twitch_search\": 1-3 words to find Twitch channels about the subject when it is not a game (e.g. 'dog training'), "
    "\"youtube_query\": the best YouTube search words for fresh, popular videos on the subject, "
    "\"label\": a short clean name for the subject}. "
    "If the subject is news, drama, controversy or reactions ABOUT a game (not playing it), set is_game false, "
    "so talking and reaction clips are kept. Search words must never ask for sexual, nude or explicit footage: "
    "for gossip topics use words like reaction, news, explained, commentary.")

HUNT_LESSONS_SYSTEM = (
    "You review how Mr. McDonald's reposted short clips performed. Reply with JSON only: "
    "{\"lessons\": 3 to 6 short, specific lessons (max 14 words each) about what got more views, "
    "likes or comments, to guide which clips to pick and how to caption them next time}.")


def hunter():
    h = STATE.setdefault("hunter", {})
    h.setdefault("on", False)
    h.setdefault("platform", "twitch")
    h.setdefault("subject", "")
    h.setdefault("every", 2)
    h.setdefault("per_run", 2)
    h.setdefault("items", [])
    h.setdefault("seen", {})
    h.setdefault("lessons", [])
    return h


def find_ytdlp():
    for c in (os.path.join(APP_DIR, "bin", "yt-dlp"), shutil.which("yt-dlp"),
              "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


def ytdlp_json(args, timeout=120):
    yd = find_ytdlp()
    if not yd:
        raise RuntimeError("no-ytdlp")
    res = subprocess.run([yd, "--flat-playlist", "-J", "--no-warnings", "--ignore-errors"] + args,
                         capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(res.stdout or "{}") or {}
    except ValueError:
        raise RuntimeError("ytdlp:" + (res.stderr or "")[-200:])


def twitch_keys():
    sec = secrets()
    return sec.get("twitch_client_id"), sec.get("twitch_client_secret")


def twitch_get(path, params):
    cid, csec = twitch_keys()
    if not (cid and csec):
        raise RuntimeError("no-twitch")
    if not _HUNT["token"] or time.time() > _HUNT["token_exp"] - 300:
        q = urllib.parse.urlencode({"client_id": cid, "client_secret": csec, "grant_type": "client_credentials"})
        req = urllib.request.Request("https://id.twitch.tv/oauth2/token?" + q, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                tok = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError("twitch-auth:{}".format(exc.code))
        _HUNT["token"], _HUNT["token_exp"] = tok["access_token"], time.time() + int(tok.get("expires_in", 3600))
    url = "https://api.twitch.tv/helix/" + path + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Client-Id": cid, "Authorization": "Bearer " + _HUNT["token"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()).get("data") or []
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            _HUNT["token"] = None
        raise RuntimeError("twitch:{}".format(exc.code))


def hunt_profile(subject):
    """Ask Claude once per subject whether it's a game and how to search for it."""
    h = hunter()
    prof = h.get("profile") or {}
    if prof.get("subject") == subject:
        # A profile saved before the Twitch keys existed has no game id: look it up now.
        if prof.get("is_game") and not prof.get("game_id") and all(twitch_keys()):
            _twitch_game(prof, subject)
            with LOCK:
                persist()
        return prof
    try:
        resp = call_claude({"max_tokens": 300, "system": HUNT_PROFILE_SYSTEM,
                            "messages": [{"role": "user", "content": "Subject: " + subject}]},
                           CONFIG["models"][-1] if CONFIG.get("models") else CONFIG["default_model"])
        m = re.search(r"\{.*\}", text_of(resp.get("content", [])), re.S)
        data = json.loads(m.group(0)) if m else {}
    except (ClaudeError, ValueError):
        data = {}
    prof = {"subject": subject, "is_game": bool(data.get("is_game")),
            "twitch_category": data.get("twitch_category") or None,
            "twitch_search": data.get("twitch_search") or subject,
            "youtube_query": data.get("youtube_query") or subject,
            "label": data.get("label") or subject, "game_id": None, "game_name": None}
    if prof["is_game"] and all(twitch_keys()):
        _twitch_game(prof, subject)
    with LOCK:
        h["profile"] = prof
        persist()
    return prof


def _twitch_game(prof, subject):
    """Find the Twitch game category for a game subject (exact name first)."""
    try:
        for name in [prof.get("twitch_category"), subject]:
            if not name:
                continue
            cats = twitch_get("search/categories", {"query": name, "first": 10})
            pick = next((c for c in cats if c["name"].lower() == name.lower()), cats[0] if cats else None)
            if pick:
                prof["game_id"], prof["game_name"] = pick["id"], pick["name"]
                return True
    except RuntimeError:
        pass
    return False


def _iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def twitch_candidates(prof):
    """Top clips for the subject. Starts with the last day; if that's thin, looks back 3 then 7 days."""
    out = []
    for hours in (24, 72, 168):
        out = _twitch_clips(prof, hours)
        if len(out) >= 5:
            break
    return out


def _twitch_clips(prof, hours):
    since = _iso_utc(datetime.utcnow() - timedelta(hours=hours))
    clips = []
    if prof.get("game_id"):
        clips = twitch_get("clips", {"game_id": prof["game_id"], "started_at": since, "first": 100})
    else:
        chans = twitch_get("search/channels", {"query": prof["twitch_search"], "first": 20})
        for ch in chans[:8]:
            try:
                clips += twitch_get("clips", {"broadcaster_id": ch["id"], "started_at": since, "first": 20})
            except RuntimeError:
                continue
    out = []
    for c in clips:
        if c.get("language") and not str(c["language"]).startswith("en"):
            continue
        if float(c.get("duration") or 0) < 8:
            continue
        name = c.get("broadcaster_name") or ""
        out.append({"key": "tw:" + c["id"], "url": c["url"], "title": c.get("title") or "",
                    "creator": name, "creator_url": "https://twitch.tv/" + name.lower(),
                    "views": int(c.get("view_count") or 0), "thumb": c.get("thumbnail_url"),
                    "made": c.get("created_at"), "platform": "twitch", "length": c.get("duration")})
    out.sort(key=lambda x: -x["views"])
    return out


def youtube_candidates(prof):
    # YouTube's own filter: uploaded this week, sorted by view count (sp=CAMSAggD).
    url = "https://www.youtube.com/results?search_query={}&sp=CAMSAggD".format(urllib.parse.quote_plus(prof["youtube_query"]))
    data = ytdlp_json(["--playlist-end", "40", url])
    entries = [e for e in (data.get("entries") or []) if e]
    if not entries:   # fall back to plain search ranking
        data = ytdlp_json(["--playlist-end", "40", "ytsearch40:" + prof["youtube_query"]])
        entries = [e for e in (data.get("entries") or []) if e]
    out = []
    for e in entries:
        dur = e.get("duration") or 0
        if not e.get("id") or (dur and (dur < 120 or dur > 90 * 60)):
            continue   # skip Shorts and very long streams (too heavy for this Mac)
        thumbs = e.get("thumbnails") or []
        out.append({"key": "yt:" + e["id"], "url": "https://www.youtube.com/watch?v=" + e["id"],
                    "title": e.get("title") or "", "creator": e.get("channel") or e.get("uploader") or "",
                    "creator_url": e.get("channel_url") or e.get("uploader_url") or "",
                    "views": int(e.get("view_count") or 0), "thumb": thumbs[-1]["url"] if thumbs else None,
                    "made": None, "platform": "youtube", "length": dur})
    out.sort(key=lambda x: -x["views"])
    return out


def already_reposted(item):
    """True when another page already put this Twitch moment up as a Short (title match)."""
    import difflib
    try:
        data = ytdlp_json(["--playlist-end", "8", "ytsearch8:{} {}".format(item["title"], item["creator"])], timeout=60)
    except RuntimeError:
        return False
    want = re.sub(r"[^a-z0-9 ]", "", item["title"].lower())
    if len(want) < 6:
        return False
    for e in data.get("entries") or []:
        got = re.sub(r"[^a-z0-9 ]", "", (e.get("title") or "").lower())
        if (e.get("duration") or 0) <= 90 and difflib.SequenceMatcher(None, want, got).ratio() >= 0.6:
            return True
    return False


def hunter_pending():
    urls = {i["url"] for i in hunter()["items"] if i.get("state") == "sent"}
    return [x for x in list_inbox() if x["kind"] == "link" and x["name"] in urls]


def run_hunter(manual=False):
    h = hunter()
    if _HUNT["running"]:
        return
    _HUNT["running"] = True
    try:
        subject = (h.get("subject") or "").strip()
        plat = h.get("platform")
        note = ""
        sent = 0
        if not subject:
            note = "Type a subject first."
        elif plat == "instagram":
            note = "Instagram doesn't allow free searching yet. Switch to Twitch or YouTube."
        elif plat == "twitch" and not all(twitch_keys()):
            note = "Twitch needs its one-time setup (the yellow box)."
        elif len(hunter_pending()) >= HUNT_INBOX_CAP:
            note = "The clipper still has {} hunted videos to finish. I'll look again next time.".format(len(hunter_pending()))
        else:
            prof = hunt_profile(subject)
            try:
                cands = twitch_candidates(prof) if plat == "twitch" else youtube_candidates(prof)
            except RuntimeError as exc:
                code = str(exc)
                cands = []
                note = {"no-ytdlp": "The YouTube finder tool isn't installed yet. Ask Claude.",
                        "no-twitch": "Twitch needs its one-time setup (the yellow box)."}.get(
                    code, "Twitch said no to the keys. Check them in the yellow box." if code.startswith("twitch-auth")
                    else "Couldn't search right now ({}). I'll try again next time.".format(code[:60]))
            fell_back = False
            if plat == "twitch" and not cands and not note and find_ytdlp():
                try:   # Twitch had nothing for this subject: look on YouTube instead
                    cands = youtube_candidates(prof)
                    fell_back = bool(cands)
                except RuntimeError:
                    pass
            fresh = [c for c in cands if c["key"] not in h["seen"]]
            found = []
            for c in fresh[:12]:
                if sent >= int(h.get("per_run") or 2):
                    break
                c["found_at"] = now_iso()
                h["seen"][c["key"]] = now_iso()
                if c["platform"] == "twitch" and already_reposted(c):
                    c.update(state="skipped", why="Another page already posted this one")
                    found.append(c)
                    continue
                ok, _ = add_link(c["url"], False, bool(prof.get("is_game")))
                c.update(state="sent" if ok else "skipped", why="Sent to the clipper" if ok else "Couldn't add it")
                sent += 1 if ok else 0
                found.append(c)
            if not cands and not note:
                note = ("Nothing found on {} for “{}” in the last week. Try YouTube, or a broader subject."
                        .format("Twitch" if plat == "twitch" else "YouTube", subject))
            if cands and not note:
                note = ("Nothing on Twitch, so I looked on YouTube. " if fell_back else "") + (
                    "Found {} new, sent {} to the clipper.".format(len(fresh), sent) if fresh else "Nothing new since last time.")
            with LOCK:
                h["items"] = (found + h["items"])[:HUNT_MAX_ITEMS]
                # keep the seen list from growing forever
                cutoff = (now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
                h["seen"] = {k: v for k, v in h["seen"].items() if v >= cutoff}
        with LOCK:
            h["last_run"] = now_iso()
            h["last_note"] = note
            persist()
        if sent:
            notify_mac("Stratus House · Clip Hunter", "Found {} hot clip(s). They'll be at Your Desk after clipping.".format(sent))
    finally:
        _HUNT["running"] = False


def hunter_source(vdir):
    """The hunted item a Clips Ready folder came from (via clips.json's source link)."""
    src = (load_json(os.path.join(vdir, "clips.json"), {}) or {}).get("source")
    if not src:
        return None
    return next((i for i in hunter()["items"] if i.get("url") == src), None)


def hunter_lessons():
    h = hunter()
    if h.get("lessons_at") and h["lessons_at"] > (now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"):
        return
    posted = [r for r in STATE["clips"].values() if r.get("hunter") and r.get("status") in ("posted", "partial") and r.get("metrics")]
    if len(posted) < 3 or not secrets().get("anthropic_key"):
        return
    rows = []
    for r in posted[-20:]:
        tot = {}
        for m in (r.get("metrics") or {}).values():
            for k in ("views", "likes", "comments", "shares"):
                if isinstance(m.get(k), (int, float)):
                    tot[k] = tot.get(k, 0) + m[k]
        rows.append("creator: {} | caption: {} | {}".format(r.get("creator") or "?", (r.get("caption") or "")[:120],
                                                            ", ".join("{} {}".format(v, k) for k, v in tot.items())))
    try:
        resp = call_claude({"max_tokens": 500, "system": HUNT_LESSONS_SYSTEM,
                            "messages": [{"role": "user", "content": "\n".join(rows)}]},
                           STATE["chat"].get("model") or CONFIG["default_model"])
        m = re.search(r"\{.*\}", text_of(resp.get("content", [])), re.S)
        lessons = [str(x).strip() for x in (json.loads(m.group(0)).get("lessons") or [])][:6] if m else []
    except (ClaudeError, ValueError):
        return
    with LOCK:
        h["lessons_at"] = now_iso()
        if lessons:
            h["lessons"] = lessons
        persist()


def hunter_public():
    h = hunter()
    prof = h.get("profile") or {}
    nxt = None
    if h.get("on") and h.get("last_run"):
        try:
            nxt = nice_dt(datetime.strptime(h["last_run"], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=h.get("every", 2)))
        except ValueError:
            nxt = None
    return {k: h.get(k) for k in ("on", "platform", "subject", "every", "per_run", "last_run", "last_note", "lessons")} | {
        "running": _HUNT["running"], "next": nxt, "items": h["items"][:30],
        "is_game": bool(prof.get("is_game")) if prof.get("subject") == h.get("subject") else None,
        "game_name": prof.get("game_name") if prof.get("subject") == h.get("subject") else None,
        "has_twitch": all(twitch_keys()), "has_ytdlp": bool(find_ytdlp()),
        "pending": len(hunter_pending()),
    }


def r_hunter_set(d):
    h = hunter()
    with LOCK:
        if d.get("platform") in ("twitch", "youtube", "instagram"):
            h["platform"] = d["platform"]
        if "subject" in d:
            h["subject"] = (d.get("subject") or "").strip()[:80]
        if d.get("per_run") in (1, 2, 3):
            h["per_run"] = d["per_run"]
        if "on" in d:
            h["on"] = bool(d["on"])
            if h["on"]:
                h["last_run"] = None   # run right away
        persist()
    for f in ("twitch_client_id", "twitch_client_secret"):
        if str(d.get(f) or "").strip():
            save_secret(f, str(d[f]).strip())
            _HUNT["token"] = None
    if d.get("twitch_client_id") or d.get("twitch_client_secret"):
        try:
            twitch_get("games/top", {"first": 1})
        except RuntimeError as exc:
            return {"ok": False, "message": "Twitch didn't accept those keys. Check both and paste again." if "auth" in str(exc)
                    else "Saved, but Twitch didn't answer right now."}
        return {"ok": True, "message": "Twitch is connected ✅"}
    if h.get("subject") and secrets().get("anthropic_key"):
        threading.Thread(target=hunt_profile, args=(h["subject"],), daemon=True).start()
    return {"ok": True, "message": ("Clip Hunter is ON. First search starts now." if d.get("on") else "Saved.") if "on" not in d or d["on"] else "Clip Hunter is OFF."}


def r_hunter_remove(d):
    """✕ on a found card: drop it from the list and, if it hasn't been clipped yet, take it out of the inbox.
    It stays in the seen list so the hunter won't bring it back. Clips already at Your Desk are untouched."""
    key = d.get("key")
    h = hunter()
    with LOCK:
        item = next((i for i in h["items"] if i.get("key") == key), None)
        if not item:
            return {"ok": False, "message": "Already gone."}
        h["items"] = [i for i in h["items"] if i.get("key") != key]
        h["seen"][key] = now_iso()
        persist()
    url = item.get("url")
    cancelled = False
    if url and any(x["kind"] == "link" and x["name"] == url for x in list_inbox()):
        path = os.path.join(CONFIG["clip_inbox"], "links.txt")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(l for l in lines if l.strip() != url)
            set_hide("link", url, False)
            set_hide("link", url, False, GAMEPLAY_FILE)
            cancelled = True
        except OSError:
            pass
    return {"ok": True, "message": "Removed. It won't be clipped." if cancelled else "Removed from the list."}


def r_hunter_run(d):
    if _HUNT["running"]:
        return {"ok": True, "message": "Already searching."}
    threading.Thread(target=run_hunter, args=(True,), daemon=True).start()
    return {"ok": True, "message": "Searching now. Results show in a minute."}


ROUTES = {
    "/api/live-refresh": r_live_refresh,
    "/api/clip-gate": r_clip_gate,
    "/api/link": r_link,
    "/api/approve": r_approve,
    "/api/skip": r_skip,
    "/api/unschedule": r_unschedule,
    "/api/redo": r_redo,
    "/api/tiktok-done": r_tiktok_done,
    "/api/tiktok-post-all": r_tiktok_post_all,
    "/api/record-post": r_record_post,
    "/api/set-caption": r_set_caption,
    "/api/stay-awake": r_stay_awake,
    "/api/reveal": r_reveal,
    "/api/post-now": r_post_now,
    "/api/captions": r_captions,
    "/api/music-preview": r_music_preview,
    "/api/chat": r_chat,
    "/api/model": r_model,
    "/api/confirm": r_confirm,
    "/api/dismiss-error": r_dismiss_error,
    "/api/retry": r_retry,
    "/api/settings": r_settings,
    "/api/test-claude": r_test_claude,
    "/api/book": r_book,
    "/api/ugc-brief": r_ugc_brief,
    "/api/create-plan": r_create_plan,
    "/api/create-edit": r_create_edit,
    "/api/create-make": r_create_make,
    "/api/create-delete": r_create_delete,
    "/api/warmup-start": r_warmup_start,
    "/api/warmup-check": r_warmup_check,
    "/api/warmup-log": r_warmup_log,
    "/api/warmup-stop": r_warmup_stop,
    "/api/hunter-set": r_hunter_set,
    "/api/hunter-run": r_hunter_run,
    "/api/trends-refresh": r_trends_refresh,
    "/api/hunter-remove": r_hunter_remove,
}


# ---------------------------------------------------------------- main

def main():
    global CONFIG, STATE
    _ensure_dirs()
    CONFIG = load_config()
    if not os.path.exists(CONFIG_FILE):
        save_json(CONFIG_FILE, {k: CONFIG[k] for k in ("owner_name", "port", "upload_post_profile")})
    STATE = load_state()
    for d in (CONFIG["clip_inbox"], CONFIG["music_dir"], CONFIG["ugc_dir"]):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    scan_clips()
    resume_creations()
    persist()
    set_awake(CONFIG.get("stay_awake", True))
    threading.Thread(target=background_loop, daemon=True).start()
    port = int(os.environ.get("STRATUS_PORT") or CONFIG["port"])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    sys.stderr.write("[StratusMedia] running on http://127.0.0.1:{}\n".format(port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
