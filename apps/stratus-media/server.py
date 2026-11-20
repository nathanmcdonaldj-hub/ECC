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
    "upload_post_monthly_limit": 10,
    # Clipping hours: weekdays from market close until morning, weekends all day.
    "clip_weekday_open": "14:30",
    "clip_close": "06:00",
    "clip_weekends_all_day": True,
    "night_run": os.path.join(HOME, "openshorts", "autopilot", "night_run.sh"),
    "morning_stop": os.path.join(HOME, "openshorts", "autopilot", "morning_stop.sh"),
    "trend_brief": "Mon 5:41 AM",
    "ffmpeg": "",
    "default_model": "claude-opus-5-5",
    "models": ["claude-opus-5-5", "claude-sonnet-5-5"],
    "dry_run_posting": False,
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
    """Weekdays from market close (2:30 PM) to 6 AM; Saturday and Sunday all day."""
    dt = dt or now()
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

def list_inbox():
    inbox = CONFIG["clip_inbox"]
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
                items.append({"kind": "video", "name": name,
                              "size_mb": round(os.path.getsize(full) / 1e6, 1)})
    except OSError:
        pass
    try:
        with open(os.path.join(inbox, "links.txt"), encoding="utf-8") as fh:
            for line in fh:
                url = line.strip()
                if url and not url.startswith("#") and url not in processed:
                    items.append({"kind": "link", "name": url})
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
    with LOCK:
        for cid, info in found.items():
            rec = STATE["clips"].get(cid)
            if rec is None:
                rec = {"status": "new", "caption": "", "hashtags": "",
                       "platforms": ["instagram", "youtube"],
                       "time": suggest_time(sum(1 for r in STATE["clips"].values() if r.get("status") in ("new", "ready"))),
                       "why": "", "music": "auto", "added": now_iso()}
                STATE["clips"][cid] = rec
            rec.update({k: info[k] for k in ("id", "source", "file", "thumb", "num", "notes", "mtime")})
            rec["missing"] = False
        for cid, rec in STATE["clips"].items():
            if cid not in found and rec.get("status") in ("new", "ready", "scheduled"):
                rec["missing"] = True
    return found


def clip_public(rec):
    out = {k: rec.get(k) for k in (
        "id", "source", "num", "status", "caption", "hashtags", "platforms",
        "time", "why", "music", "notes", "posted_at", "results", "missing",
        "tiktok_done", "redo")}
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


def mix_music(rec):
    """Mix a light, ducked music bed under the clip. Returns the output path."""
    choice = rec.get("music") or "none"
    if choice == "none":
        return None
    track = pick_auto_track(rec) if choice == "auto" else choice
    if not track:
        raise RuntimeError("no-tracks")
    track_path = os.path.join(CONFIG["music_dir"], track)
    if not os.path.exists(track_path):
        raise RuntimeError("missing-track:" + track)
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("no-ffmpeg")
    base, _ = os.path.splitext(rec["file"])
    out = base + ".music.mp4"
    # Music at ~15% and pushed further down whenever someone speaks.
    filt = ("[1:a]volume=0.15,afade=t=in:d=1[m];"
            "[m][0:a]sidechaincompress=threshold=0.03:ratio=6:release=400[duck];"
            "[0:a][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    cmd = [ff, "-y", "-loglevel", "error", "-i", rec["file"], "-stream_loop", "-1",
           "-i", track_path, "-filter_complex", filt, "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", out]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg:" + res.stderr.strip()[-300:])
    rec["music_file"] = out
    rec["music_track"] = track
    return out


def music_error(rec, exc):
    code = str(exc)
    name = "clip {} from {}".format(rec.get("num"), rec.get("source"))
    if code == "no-tracks":
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
    first = (rec.get("caption") or "").strip().split("\n")[0]
    first = re.sub(r"https?://\S+", "", first).strip()
    return (first or "Stratus House")[:95]


def post_clip(rec):
    """Post one approved clip to its auto platforms. Returns True on success."""
    key = secrets().get("upload_post_key")
    name = "clip {} from {}".format(rec.get("num"), rec.get("source"))
    platforms = [p for p in rec.get("platforms", []) if p in ("instagram", "youtube")]
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
    if rec.get("music", "none") != "none":
        try:
            path = mix_music(rec)
        except Exception as exc:  # noqa: BLE001 - shown to the user in plain words
            music_error(rec, exc)
            return False
    used = posts_this_month()
    if used + len(platforms) > CONFIG["upload_post_monthly_limit"]:
        raise_error("Courier", "Free posting limit reached",
                    "{} needs {} post(s) but only {} of your {} free posts are left this month."
                    .format(name, len(platforms), max(0, CONFIG["upload_post_monthly_limit"] - used),
                            CONFIG["upload_post_monthly_limit"]),
                    "Wait until next month, post it by hand, or upgrade Upload-Post to Basic ($24/mo).",
                    key="post:limit")
        return False
    fields = [("user", CONFIG["upload_post_profile"]), ("title", youtube_title(rec)),
              ("description", full_caption(rec))]
    for p in platforms:
        fields.append(("platform[]", p))
    fields.append(("instagram_title", full_caption(rec)))
    if CONFIG.get("dry_run_posting"):
        result = {"dry_run": True, "fields": [f[0] for f in fields]}
    else:
        body, ctype = multipart(fields, [("video", path)])
        req = urllib.request.Request(UPLOAD_POST_URL, data=body, method="POST", headers={
            "Authorization": "Apikey " + key, "Content-Type": ctype})
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
        rec["results"] = result
        ok_platforms = [p for p in platforms if p not in failed]
        if ok_platforms:
            STATE["posts"].append({"id": rec["id"], "platforms": ok_platforms,
                                   "at": now_iso(), "caption": rec.get("caption", "")[:80]})
        rec["posted_at"] = now_iso()
        rec["status"] = "posted" if not failed else "partial"
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

What you run: clips are made from his Clip Inbox on weekdays from 2:30 PM (after he finishes trading) to 6 AM and all day on weekends, 3 clips per video max (he can press Open/Close on the dashboard); you and the \
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

def add_link(url):
    url = (url or "").strip()
    if not re.match(r"^https?://\S+$", url):
        return False, "That doesn't look like a video link."
    inbox = CONFIG["clip_inbox"]
    try:
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "links.txt"), "a", encoding="utf-8") as fh:
            fh.write(url + "\n")
    except OSError as exc:
        raise_error("Inbox Clerk", "Couldn't save the link",
                    "Writing to Movies → Clip Inbox failed ({}).".format(exc),
                    "Ask Claude in your project chat to check the Clip Inbox folder.", key="inbox:write")
        return False, "Couldn't save it."
    return True, "Queued for the next clipping run."


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


def apply_schedule(clip_id, time_s, platforms, caption, hashtags, music):
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
    rec["status"] = "scheduled"
    rec["approved_at"] = now_iso()
    if "tiktok" in rec["platforms"]:
        rec["tiktok_done"] = False
    persist()
    return True, "Scheduled for " + nice_time(time_s)


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
               ("Open · waiting for videos" if gate_open else "Closed · opens {}".format(nice_dt(nxt))),
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
        worker("scout", "Trend Scout", "off", "Next brief {} · in your project chat".format(CONFIG["trend_brief"])),
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
        "clips_waiting": waiting,
        "clips_scheduled": scheduled,
        "tiktok": tiktok,
        "posted": posted[:30],
        "redo": redo,
        "errors": errors,
        "posts_used": used,
        "posts_limit": CONFIG["upload_post_monthly_limit"],
        "stats": stats,
        "sales": sales,
    }
    if for_model:
        return out
    out.update({
        "books": STATE["books"],
        "ugc": STATE["ugc"][-20:],
        "music": list_music(),
        "music_ready": bool(find_ffmpeg()),
        "chat_model": STATE["chat"].get("model") or CONFIG["default_model"],
        "models": CONFIG["models"],
        "chat_log": STATE["chat"].get("log", [])[-30:],
        "pending_actions": [dict(a, time_nice=nice_time(a.get("time"))) for a in STATE["pending_actions"].values()],
        "settings": {
            "has_anthropic_key": bool(sec.get("anthropic_key")),
            "has_upload_post_key": bool(sec.get("upload_post_key")),
            "has_higgsfield_key": bool(sec.get("higgsfield_key")),
            "profile": CONFIG["upload_post_profile"],
            "dry_run": CONFIG.get("dry_run_posting", False),
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
                rec.pop("retry_at", None)
                if any(p in ("instagram", "youtube") for p in rec.get("platforms", [])):
                    post_clip(rec)
                else:
                    rec["status"] = "posted"
                    rec["posted_at"] = now_iso()
                    persist()
                if "tiktok" in rec.get("platforms", []) and not rec.get("tiktok_done"):
                    notify_mac("Stratus House · TikTok Runner",
                               "Time to post clip {} on TikTok. Open the dashboard to copy the caption.".format(rec.get("num")))
            if time.time() - last_stats > 6 * 3600:
                last_stats = time.time()
                if STATE["posts"]:
                    fetch_stats()
            persist()
        except Exception:  # noqa: BLE001 - the loop must keep running
            sys.stderr.write("[StratusMedia] loop error\n" + traceback.format_exc())
        time.sleep(60)


# ---------------------------------------------------------------- clipping gate runner
# OpenShorts (Docker) only runs while the gate is open AND there is something to clip.
# When a run finishes, or the gate closes, it is shut down so the trading desk keeps its memory.

RUN = {"proc": None, "queue_before": None, "cool_until": 0.0, "stopped": True}


def _external_run():
    """A clipping run started outside this process (e.g. before a dashboard restart)."""
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-f", os.path.basename(CONFIG["night_run"])],
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
    RUN["queue_before"] = [x.get("name") or x.get("url") for x in list_inbox()]
    RUN["stopped"] = False


def run_clipper_gate():
    is_open = gate()
    proc = RUN["proc"]
    if proc and proc.poll() is not None:  # a run just finished
        RUN["proc"] = None
        left = [x.get("name") or x.get("url") for x in list_inbox()]
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
    if list_inbox() or _redo_pending():
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
        return {"ok": True, "message": "Clipping closed. It stays closed until {}, or until you press Open.".format(
            nice_dt(nxt))}
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
            "hours": "Weekdays 2:30 PM to 6 AM · weekends all day"}


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
        what, fix = explain_clipper_line(line)
        raise_error("Clipper", "Clipping hit a problem", what, fix, key="clip:" + fix[:40])


def explain_clipper_line(line):
    low = line.lower()
    if "docker" in low and ("not running" in low or "cannot connect" in low or "daemon" in low):
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
        return self.send_json({"ok": True, "path": dest, "name": name,
                               "message": "Added to the Clip Inbox. It's clipped on the next run." if kind == "video" else "Saved."})


# ---------------------------------------------------------------- route handlers

def r_link(d):
    ok, msg = add_link(d.get("url"))
    return {"ok": ok, "message": msg}


def r_approve(d):
    with LOCK:
        ok, msg = apply_schedule(d.get("id"), d.get("time"), d.get("platforms") or [],
                                 d.get("caption"), d.get("hashtags"), d.get("music"))
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


def r_post_now(d):
    with LOCK:
        rec = STATE["clips"].get(d.get("id"))
        if not rec:
            return {"ok": False, "message": "Clip not found."}
        rec["status"] = "scheduled"
        rec["time"] = now().strftime("%Y-%m-%dT%H:%M")
        rec.pop("retry_at", None)
        persist()
    ok = post_clip(rec)
    return {"ok": ok, "message": "Posted." if ok else "It didn't post. The red banner says why."}


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
        rec["music"] = d.get("music") or "auto"
    try:
        mix_music(rec)
    except Exception as exc:  # noqa: BLE001
        music_error(rec, exc)
        return {"ok": False, "message": "Couldn't add music. The red banner says why."}
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


ROUTES = {
    "/api/clip-gate": r_clip_gate,
    "/api/link": r_link,
    "/api/approve": r_approve,
    "/api/skip": r_skip,
    "/api/unschedule": r_unschedule,
    "/api/redo": r_redo,
    "/api/tiktok-done": r_tiktok_done,
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
    persist()
    threading.Thread(target=background_loop, daemon=True).start()
    port = int(os.environ.get("STRATUS_PORT") or CONFIG["port"])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    sys.stderr.write("[StratusMedia] running on http://127.0.0.1:{}\n".format(port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
