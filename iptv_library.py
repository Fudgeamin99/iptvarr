#!/usr/bin/env python3
"""
IPTV Library - browse the Movies and TV Shows on your IPTV account.

Supports:
  * Xtream Codes logins (server URL + username + password)  <- most providers
  * M3U / M3U8 playlist URLs (get.php links are auto-upgraded to Xtream)

Run:   python3 iptv_library.py            (opens http://127.0.0.1:8765)
       python3 iptv_library.py --downloads "D:/Media" --port 9000 --no-browser

Downloads go to  ~/Downloads/IPTV Library/Movies/...  and  .../TV Shows/<Show>/Season 01/...
(change the folder in the app's Downloads panel). Interrupted downloads resume.

No third-party packages needed - Python 3.8+ standard library only.
Everything runs locally; your login is only ever sent to your own IPTV server.
"""
import argparse
import base64
import hmac
import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.2.0"
REPO = os.environ.get("IPTV_REPO", "Fudgeamin99/iptvarr")   # GitHub repo the Update button checks
UA = "Mozilla/5.0 (IPTV Library) VLC/3.0.20"
# Many IPTV panels use self-signed / expired certificates.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# Updates come from GitHub, so for those the certificate IS checked.
GH_CTX = ssl.create_default_context()

M3U_CACHE = {}


def fetch(url, timeout=90, ctx=CTX, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def normalize_server(s):
    s = (s or "").strip()
    if not s:
        raise ValueError("Server URL is required")
    if not re.match(r"^https?://", s, re.I):
        s = "http://" + s
    p = urllib.parse.urlsplit(s)
    return f"{p.scheme}://{p.netloc}"


# ---------------------------------------------------------------- M3U parsing
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
SXE = re.compile(r"^(?P<show>.*?)[\s._\-\[(|]*S(?P<s>\d{1,2})\s*[._\-]?\s*E(?P<e>\d{1,4})", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|mkv|avi|mov|m4v|wmv|flv|webm)$", re.I)


def parse_m3u(text):
    movies, shows = [], {}
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = dict(ATTR.findall(line))
            rest = ATTR.sub("", line)
            name = rest.split(",", 1)[1].strip() if "," in rest else attrs.get("tvg-name", "")
            cur = {"name": name or attrs.get("tvg-name", "Untitled"),
                   "logo": attrs.get("tvg-logo", ""),
                   "group": attrs.get("group-title", "") or "Uncategorised"}
            continue
        if line.startswith("#") or cur is None:
            continue
        url, item, cur = line, cur, None
        path = urllib.parse.urlsplit(url).path.lower()
        m = SXE.match(item["name"])
        if "/series/" in path or (VIDEO_EXT.search(path) and m and "/movie/" not in path):
            show = (m.group("show").strip(" -_.|") if m else item["name"]) or item["name"]
            season = str(int(m.group("s"))) if m else "1"
            ep = int(m.group("e")) if m else 0
            key = (item["group"], show.lower())
            s = shows.setdefault(key, {"name": show, "logo": item["logo"],
                                       "group": item["group"], "seasons": {}})
            s["seasons"].setdefault(season, []).append(
                {"num": ep, "title": item["name"], "url": url})
        elif "/movie/" in path or VIDEO_EXT.search(path):
            movies.append({**item, "url": url})
        # anything else is a live channel - skipped
    series = list(shows.values())
    for s in series:
        for eps in s["seasons"].values():
            eps.sort(key=lambda e: e["num"])
            for i, e in enumerate(eps, 1):
                e["num"] = e["num"] or i
    return {"movies": movies, "series": series}


# ---------------------------------------------------------------- Downloads
# Docker / NAS: IPTV_CONFIG_DIR, IPTV_DOWNLOADS and IPTV_APP_PASSWORD can be set as environment variables
CONFIG_DIR = os.environ.get("IPTV_CONFIG_DIR")
SETTINGS_FILE = Path(CONFIG_DIR) / "settings.json" if CONFIG_DIR else Path.home() / ".iptv_library.json"
JOBS_FILE = Path(CONFIG_DIR) / "downloads.json" if CONFIG_DIR else Path.home() / ".iptv_library_downloads.json"
APP_USER = os.environ.get("IPTV_APP_USER", "iptv")
APP_PASSWORD = os.environ.get("IPTV_APP_PASSWORD", "")
# Folder picker limit: on Docker this is /downloads so the picker can't wander round the container.
BROWSE_ROOT = Path(os.environ["IPTV_BROWSE_ROOT"]).resolve() if os.environ.get("IPTV_BROWSE_ROOT") else None
JUNK_DIRS = ("@eaDir", "#recycle", "#snapshot", "@Recycle", "$RECYCLE.BIN", "System Volume Information", "lost+found")
CAN_OPEN = sys.platform.startswith("win") or sys.platform == "darwin" or bool(shutil.which("xdg-open"))
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
ACTIVE = ("queued", "downloading", "processing")


def clean_segment(s):
    s = re.sub(r"\s+", " ", BAD_CHARS.sub(" ", str(s))).strip(" .")
    if not s:
        s = "Untitled"
    if s.split(".")[0].upper() in RESERVED:
        s = "_" + s
    if len(s) > 150:
        stem, ext = os.path.splitext(s)
        s = stem[: 150 - len(ext)].rstrip(" .") + ext
    return s


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(d):
    try:
        SETTINGS_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- Audio & subtitle tracks (needs FFmpeg)
FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
# (code used in settings, name, ISO 639-1 for .srt names, every tag spelling seen in the wild)
LANGS = [
    ("eng", "English", "en", "en eng english"), ("spa", "Spanish", "es", "es spa esp spanish espanol español castellano"),
    ("fre", "French", "fr", "fr fre fra french francais français"), ("ger", "German", "de", "de ger deu german deutsch"),
    ("ita", "Italian", "it", "it ita italian italiano"), ("por", "Portuguese", "pt", "pt por portuguese portugues português pob ptbr pt-br"),
    ("dut", "Dutch", "nl", "nl dut nld dutch nederlands"), ("pol", "Polish", "pl", "pl pol polish polski"),
    ("tur", "Turkish", "tr", "tr tur turkish turkce türkçe"), ("ara", "Arabic", "ar", "ar ara arabic"),
    ("hin", "Hindi", "hi", "hi hin hindi"), ("urd", "Urdu", "ur", "ur urd urdu"), ("ben", "Bengali", "bn", "bn ben bengali bangla"),
    ("pan", "Punjabi", "pa", "pa pan punjabi"), ("tam", "Tamil", "ta", "ta tam tamil"), ("tel", "Telugu", "te", "te tel telugu"),
    ("mal", "Malayalam", "ml", "ml mal malayalam"), ("rus", "Russian", "ru", "ru rus russian"), ("ukr", "Ukrainian", "uk", "uk ukr ukrainian"),
    ("jpn", "Japanese", "ja", "ja jpn japanese"), ("kor", "Korean", "ko", "ko kor korean"), ("chi", "Chinese", "zh", "zh chi zho chinese mandarin cantonese"),
    ("swe", "Swedish", "sv", "sv swe swedish svenska"), ("nor", "Norwegian", "no", "no nor nob nno norwegian norsk"),
    ("dan", "Danish", "da", "da dan danish dansk"), ("fin", "Finnish", "fi", "fi fin finnish suomi"), ("gre", "Greek", "el", "el gre ell greek"),
    ("heb", "Hebrew", "he", "he heb hebrew"), ("per", "Persian", "fa", "fa per fas persian farsi"), ("rum", "Romanian", "ro", "ro rum ron romanian"),
    ("hun", "Hungarian", "hu", "hu hun hungarian magyar"), ("cze", "Czech", "cs", "cs cze ces czech"), ("slo", "Slovak", "sk", "sk slo slk slovak"),
    ("bul", "Bulgarian", "bg", "bg bul bulgarian"), ("hrv", "Croatian", "hr", "hr hrv croatian"), ("srp", "Serbian", "sr", "sr srp serbian"),
    ("alb", "Albanian", "sq", "sq alb sqi albanian shqip"), ("vie", "Vietnamese", "vi", "vi vie vietnamese"), ("tha", "Thai", "th", "th tha thai"),
    ("ind", "Indonesian", "id", "id ind indonesian"), ("may", "Malay", "ms", "ms may msa malay"), ("fil", "Filipino", "tl", "tl fil tgl filipino tagalog"),
    ("kur", "Kurdish", "ku", "ku kur kurdish"), ("som", "Somali", "so", "so som somali"),
]
LANG_ALIASES = {a: code for code, _, _, al in LANGS for a in al.split()}
LANG_NAME = {code: name for code, name, _, _ in LANGS}
LANG_2 = {code: two for code, _, two, _ in LANGS}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "tx3g"}
TRACK_CACHE = {}


def lang_code(tag):
    t = str(tag or "").strip().lower()
    return LANG_ALIASES.get(t) or LANG_ALIASES.get(t.split("-")[0].split("_")[0]) or ("" if t in ("und", "unk", "") else t)


def probe_tracks(src, remote=True):
    """List the audio and subtitle tracks inside a video (a URL or a local file)."""
    if not FFPROBE:
        raise RuntimeError("FFmpeg isn't installed, so tracks can't be read")
    if remote and src in TRACK_CACHE:
        return TRACK_CACHE[src]
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams"]
    if remote:
        if not src.startswith(("http://", "https://")):
            raise ValueError("Only http(s) links can be checked")
        cmd += ["-user_agent", UA, "-protocol_whitelist", "http,https,tcp,tls,crypto", "-rw_timeout", "20000000"]
    r = subprocess.run(cmd + [src], capture_output=True, timeout=90)
    if r.returncode:
        raise RuntimeError("Couldn't read the file's tracks: " + (r.stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown error"])[-1][:200])
    audio, subs = [], []
    for st in json.loads(r.stdout or b"{}").get("streams", []):
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        disp = st.get("disposition") or {}
        lang = lang_code(tags.get("language"))
        t = {"index": st.get("index"), "lang": lang, "lang_name": LANG_NAME.get(lang, lang.upper() if lang else ""),
             "title": tags.get("title", ""), "codec": st.get("codec_name", ""), "default": bool(disp.get("default")),
             "forced": bool(disp.get("forced")) or "forced" in tags.get("title", "").lower()}
        if st.get("codec_type") == "audio":
            audio.append({**t, "n": len(audio), "channels": st.get("channels") or 0})
        elif st.get("codec_type") == "subtitle":
            subs.append({**t, "n": len(subs), "text": t["codec"] in TEXT_SUBS})
    res = {"audio": audio, "subs": subs}
    if remote:
        TRACK_CACHE[src] = res
    return res


def pick_audio(audio, want, lang):
    if not audio:
        return None
    if isinstance(want, int):
        for a in audio:
            if a["index"] == want:
                return a
    same = [a for a in audio if a["lang"] == lang]
    if same:
        return sorted(same, key=lambda a: (not a["default"], -a["channels"]))[0]
    return next((a for a in audio if a["default"]), audio[0])


def pick_sub(subs, want, lang):
    if want == "none" or not subs:
        return None
    if isinstance(want, int):
        for x in subs:
            if x["index"] == want:
                return x
    if not lang or lang == "off":
        return None
    same = [x for x in subs if x["lang"] == lang]
    return sorted(same, key=lambda x: (x["forced"], not x["text"]))[0] if same else None


DEFAULT_PREFS = {"audio_lang": "eng", "sub_lang": "eng", "keep_audio": False, "srt": True}


def allowed_path(f, base=None):
    """Turn what the user typed/picked into an absolute folder, kept inside BROWSE_ROOT if one is set."""
    p = Path(str(f or "").strip() or (base or ".")).expanduser()
    if not p.is_absolute():
        p = Path(base or BROWSE_ROOT or Path.home()) / p
    p = p.resolve()
    if BROWSE_ROOT and p != BROWSE_ROOT and BROWSE_ROOT not in p.parents:
        raise ValueError(f"Choose a folder inside {BROWSE_ROOT} (that's your NAS download share)")
    return p


def list_folders(path, base):
    p = allowed_path(path, base)
    while not p.is_dir() and p != p.parent and (not BROWSE_ROOT or p != BROWSE_ROOT):
        p = p.parent                                   # folder not created yet - show the nearest existing one
    dirs = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith(".") and e.name not in JUNK_DIRS:
                        dirs.append(e.name)
                except OSError:
                    pass
    except OSError as e:
        raise ValueError(f"Can't open {p}: {e.strerror}")
    at_top = p == p.parent or (BROWSE_ROOT is not None and p == BROWSE_ROOT)
    return {"path": str(p), "parent": None if at_top else str(p.parent), "root": str(BROWSE_ROOT or ""),
            "dirs": sorted(dirs, key=str.lower)[:1000]}


class Downloader:
    def __init__(self, folder, workers, movies=None, tv=None, prefs=None):
        self.lock = threading.Lock()
        self.jobs, self.seq, self.threads, self.max = {}, 0, 0, 1
        self.q = queue.Queue()
        self.folder = Path(folder).expanduser()
        self.movies = Path(movies).expanduser() if movies else self.folder / "Movies"
        self.tv = Path(tv).expanduser() if tv else self.folder / "TV Shows"
        self.prefs = dict(DEFAULT_PREFS)
        self.set_prefs(prefs or {})
        self.set_workers(workers)

    def set_prefs(self, p):
        if p.get("audio_lang"):
            self.prefs["audio_lang"] = lang_code(p["audio_lang"]) or "eng"
        if p.get("sub_lang"):
            self.prefs["sub_lang"] = "off" if p["sub_lang"] == "off" else (lang_code(p["sub_lang"]) or "eng")
        for k in ("keep_audio", "srt"):
            if k in p:
                self.prefs[k] = bool(p[k])

    # settings
    def set_workers(self, n):
        self.max = max(1, min(6, int(n)))
        while self.threads < self.max:
            threading.Thread(target=self._worker, args=(self.threads,), daemon=True).start()
            self.threads += 1

    def set_folder(self, f):
        p = allowed_path(f, self.folder)
        p.mkdir(parents=True, exist_ok=True)
        self.folder = p

    def set_kind_folder(self, kind, f):
        p = allowed_path(f, self.folder)
        p.mkdir(parents=True, exist_ok=True)
        if kind == "movie":
            self.movies = p
        else:
            self.tv = p

    def save(self):
        save_settings({"folder": str(self.folder), "movies": str(self.movies), "tv": str(self.tv), "workers": self.max,
                       "prefs": self.prefs})

    # the queue is saved so restarts and updates don't lose downloads (they resume from .part files)
    def persist(self):
        keep = ("title", "url", "dest", "status", "done", "total", "error", "audio", "subs")
        with self.lock:
            rows = [{k: j.get(k) for k in keep} for j in sorted(self.jobs.values(), key=lambda j: j["id"])
                    if j["status"] in ACTIVE + ("failed", "cancelled")]
        try:
            tmp = JOBS_FILE.with_name(JOBS_FILE.name + ".tmp")
            tmp.write_text(json.dumps(rows))
            os.replace(tmp, JOBS_FILE)
        except Exception:
            pass

    def restore(self):
        try:
            rows = json.loads(JOBS_FILE.read_text())
        except Exception:
            return
        with self.lock:
            for r in rows:
                self.seq += 1
                st = "queued" if r.get("status") in ACTIVE else r.get("status", "failed")
                self.jobs[self.seq] = {**r, "id": self.seq, "status": st, "speed": 0, "cancel": False, "note": ""}
                if st == "queued":
                    self.q.put(self.seq)

    # queue management
    def add(self, items):
        added = skipped = 0
        with self.lock:
            busy = {j["dest"] for j in self.jobs.values() if j["status"] in ACTIVE}
            for it in items or []:
                url = str(it.get("url", ""))
                segs = [clean_segment(s) for s in (it.get("path") or [])]
                if not url.startswith(("http://", "https://")) or not segs:
                    skipped += 1
                    continue
                kind = it.get("kind")
                base = self.movies if kind == "movie" else self.tv if kind == "tv" else self.folder
                dest = str(base.joinpath(*segs))
                if dest in busy:
                    skipped += 1
                    continue
                self.seq += 1
                aud, sub = it.get("audio"), it.get("subs")
                self.jobs[self.seq] = {"id": self.seq, "title": str(it.get("title") or segs[-1]), "url": url,
                                       "dest": dest, "status": "queued", "done": 0, "total": 0,
                                       "speed": 0, "error": "", "cancel": False, "note": "",
                                       "audio": aud if isinstance(aud, int) else None,
                                       "subs": sub if isinstance(sub, int) or sub == "none" else None}
                busy.add(dest)
                self.q.put(self.seq)
                added += 1
        self.persist()
        return {"added": added, "skipped": skipped}

    def _targets(self, jid):
        return list(self.jobs.values()) if jid == "all" else [self.jobs[int(jid)]] if int(jid) in self.jobs else []

    def cancel(self, jid):
        for j in self._targets(jid):
            if j["status"] == "queued":
                j["status"] = "cancelled"
            elif j["status"] == "downloading":
                j["cancel"] = True
        self.persist()

    def retry(self, jid):
        for j in self._targets(jid):
            if j["status"] in ("failed", "cancelled"):
                j.update(status="queued", cancel=False, error="", speed=0)
                self.q.put(j["id"])
        self.persist()

    def clear(self):
        with self.lock:
            for k in [k for k, j in self.jobs.items() if j["status"] not in ACTIVE]:
                del self.jobs[k]
        self.persist()

    def snapshot(self):
        keep = ("id", "title", "dest", "status", "done", "total", "speed", "error", "note")
        return {"folder": str(self.folder), "movies_folder": str(self.movies), "tv_folder": str(self.tv),
                "workers": self.max, "can_open": CAN_OPEN, "prefs": self.prefs, "ffmpeg": bool(FFMPEG and FFPROBE),
                "langs": [[c, n] for c, n, _, _ in LANGS],
                "jobs": [{k: j.get(k, "") for k in keep} for j in sorted(self.jobs.values(), key=lambda j: -j["id"])]}

    # workers
    def _worker(self, idx):
        while True:
            jid = self.q.get()
            if idx >= self.max:          # concurrency was lowered - hand the job back
                self.q.put(jid)
                time.sleep(1)
                continue
            j = self.jobs.get(jid)
            if not j or j["status"] != "queued":
                continue
            j.update(status="downloading", error="")
            for attempt in range(4):
                try:
                    self._download(j)
                    break
                except Cancelled:
                    j["status"] = "cancelled"
                    break
                except Exception as e:  # noqa
                    j["error"] = (f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)) or "Error"
                    fatal = isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404)
                    if fatal or attempt == 3 or j["cancel"]:
                        j["status"] = "cancelled" if j["cancel"] else "failed"
                        break
                    time.sleep(3 * (attempt + 1))   # then resume from the .part file
            j["speed"] = 0
            self.persist()

    def _download(self, j):
        dest = Path(j["dest"])
        part = dest.with_name(dest.name + ".part")
        if dest.exists():
            j["done"] = j["total"] = dest.stat().st_size
            j["status"] = "exists"
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        start = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": UA}
        if start:
            headers["Range"] = f"bytes={start}-"
        try:
            resp = urllib.request.urlopen(urllib.request.Request(j["url"], headers=headers), timeout=60, context=CTX)
        except urllib.error.HTTPError as e:
            if e.code == 416 and start:     # .part already holds the whole file
                j["done"] = j["total"] = start
                return self._finish(j, part, dest)
            raise
        with resp:
            if start and resp.status != 206:
                start = 0                    # server ignored the resume request - start over
            ctype = resp.headers.get("Content-Type", "")
            if "text/html" in ctype or "json" in ctype:
                raise RuntimeError("Server sent a web page, not a video (connection limit reached or account expired?)")
            length = int(resp.headers.get("Content-Length") or 0)
            j["done"], j["total"] = start, (start + length if length else 0)
            t0, b0 = time.time(), start
            with open(part, "ab" if start else "wb") as f:
                while True:
                    if j["cancel"]:
                        raise Cancelled()
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    j["done"] += len(chunk)
                    now = time.time()
                    if now - t0 >= 1:
                        j["speed"], t0, b0 = (j["done"] - b0) / (now - t0), now, j["done"]
        if j["total"] and j["done"] < j["total"]:
            raise RuntimeError("Connection dropped - resuming")
        self._finish(j, part, dest)

    def _finish(self, j, part, dest):
        """Apply the audio/subtitle choice (if FFmpeg is there), then put the file in place."""
        j["error"] = ""
        try:
            self._tracks(j, part, dest)
        except Exception as e:  # noqa - never lose a finished download over this
            j["note"] = f"Kept the original file (couldn't change tracks: {e})"
        if part.exists():
            part.replace(dest)
        j["status"], j["speed"] = "done", 0

    def _tracks(self, j, part, dest):
        fmt = {".mkv": "matroska", ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov"}.get(dest.suffix.lower())
        if not (FFMPEG and FFPROBE and fmt):
            return
        tr = probe_tracks(str(part), remote=False)
        audio, subs = tr["audio"], tr["subs"]
        if len(audio) <= 1 and not subs:
            return                                     # nothing to choose between
        P = self.prefs
        a = pick_audio(audio, j.get("audio"), P["audio_lang"])
        sub = pick_sub(subs, j.get("subs"), P["sub_lang"])
        order = ([a] + ([x for x in audio if x is not a] if P["keep_audio"] else [])) if a else []
        embed = bool(sub) and (fmt == "matroska" or sub["text"])
        j["status"] = "processing"
        j["note"] = "Audio: " + ((a["lang_name"] or "default") if a else "none") + \
                    " · Subtitles: " + ((sub["lang_name"] or "on") if sub else "off")
        tmp = dest.with_name(dest.stem + ".processing" + dest.suffix)
        cmd = [FFMPEG, "-v", "error", "-y", "-i", str(part), "-map", "0:v?"]
        for x in order:
            cmd += ["-map", f"0:{x['index']}"]
        if embed:
            cmd += ["-map", f"0:{sub['index']}"]
        if fmt == "matroska":
            cmd += ["-map", "0:t?"]                      # keep attached fonts for styled subtitles
        cmd += ["-c", "copy"]
        if embed and fmt != "matroska":
            cmd += ["-c:s", "mov_text"]
        for i in range(len(order)):
            cmd += [f"-disposition:a:{i}", "default" if i == 0 else "0"]
        if embed:
            cmd += ["-disposition:s:0", "default"]
        r = subprocess.run(cmd + ["-f", fmt, str(tmp)], capture_output=True, timeout=6 * 3600)
        if r.returncode or not tmp.exists():
            tmp.unlink(missing_ok=True) if tmp.exists() else None
            raise RuntimeError((r.stderr.decode("utf-8", "replace").strip().splitlines() or ["FFmpeg failed"])[-1][:200])
        if sub and sub["text"] and P["srt"]:            # a separate .srt too, which Plex/Jellyfin/TVs pick up
            name = f"{dest.stem}.{LANG_2.get(sub['lang'], sub['lang'] or 'und')}{'.forced' if sub['forced'] else ''}.srt"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(part), "-map", f"0:{sub['index']}", "-c:s", "srt",
                            str(dest.with_name(name))], capture_output=True, timeout=600)
        os.replace(tmp, dest)
        part.unlink(missing_ok=True)


def open_folder(p):
    p = str(p)
    Path(p).mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(p)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


# ---------------------------------------------------------------- Updates (GitHub releases)
_RELEASE = {"t": 0, "data": None}


def ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def update_info(force=False):
    if force or not _RELEASE["data"] or time.time() - _RELEASE["t"] > 3600:
        api_url = os.environ.get("IPTV_UPDATE_URL") or f"https://api.github.com/repos/{REPO}/releases/latest"
        data = json.loads(fetch(api_url, timeout=20,
                                ctx=GH_CTX, accept="application/vnd.github+json"))
        _RELEASE.update(t=time.time(), data=data)
    rel = _RELEASE["data"]
    latest = str(rel.get("tag_name", "")).lstrip("vV")
    asset = next((a for a in rel.get("assets", []) if a.get("name") == "iptv_library.py"), None)
    target = Path(__file__).resolve()
    writable = os.access(target, os.W_OK) and os.access(target.parent, os.W_OK)
    available = ver_tuple(latest) > ver_tuple(VERSION)
    reason = ("" if not available else "This release has no file for this version of the app yet - try again in a few minutes."
              if not asset else "" if writable else
              f"The app can't replace its own file ({target}). On Docker, remove ':ro' from the app volume.")
    return {"current": VERSION, "latest": latest or VERSION, "available": available, "notes": rel.get("body") or "",
            "page": rel.get("html_url") or f"https://github.com/{REPO}/releases",
            "can_apply": bool(available and asset and writable), "reason": reason,
            "asset": asset and asset.get("browser_download_url")}


def apply_update():
    info = update_info(force=True)
    if not info["can_apply"]:
        raise RuntimeError(info["reason"] or "You already have the latest version")
    data = fetch(info["asset"], timeout=600, ctx=GH_CTX)
    compile(data, "iptv_library.py", "exec")                # refuse anything that isn't valid Python
    if b"VERSION = " not in data:
        raise RuntimeError("Downloaded update doesn't look like IPTV Library")
    path = Path(__file__).resolve()
    tmp = path.with_name(path.name + ".new")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    threading.Timer(1.0, restart_self).start()
    return {"ok": True, "updating_to": info["latest"]}


def restart_self():
    DL.persist()
    os.environ["IPTV_RESTART"] = "1"
    args = [sys.executable] + sys.argv + ([] if "--no-browser" in sys.argv else ["--no-browser"])
    if sys.platform.startswith("win"):
        subprocess.Popen(args, close_fds=True)
        os._exit(0)
    os.execv(sys.executable, args)


DL = None  # set in main()


# ---------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorised(self):
        """Optional password (HTTP Basic auth) for when the app is reachable from the network."""
        if not APP_PASSWORD:
            return True
        try:
            kind, _, b64 = (self.headers.get("Authorization") or "").partition(" ")
            user, _, pw = base64.b64decode(b64).decode("utf-8").partition(":")
            if kind.lower() == "basic" and hmac.compare_digest(user, APP_USER) and hmac.compare_digest(pw, APP_PASSWORD):
                return True
        except Exception:
            pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="IPTV Library", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.authorised():
            return
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        try:
            if u.path == "/":
                return self.send(200, PAGE, "text/html")
            if u.path == "/api/xtream":
                server = normalize_server(q.pop("server", ""))
                params = {k: v for k, v in q.items() if v != ""}
                url = server + "/player_api.php?" + urllib.parse.urlencode(params)
                data = fetch(url)
                try:
                    json.loads(data)
                except ValueError:
                    raise RuntimeError("Server did not return valid data - check the server URL")
                return self.send(200, data)
            if u.path == "/api/m3u":
                url = q.get("url", "").strip()
                if not url:
                    raise ValueError("Playlist URL is required")
                if q.get("refresh") or url not in M3U_CACHE:
                    text = fetch(url, timeout=180).decode("utf-8", "replace")
                    if "#EXTINF" not in text:
                        raise RuntimeError("That URL is not an M3U playlist")
                    M3U_CACHE[url] = parse_m3u(text)
                return self.send(200, M3U_CACHE[url])
            if u.path == "/api/downloads":
                return self.send(200, DL.snapshot())
            if u.path == "/api/folders":
                try:
                    return self.send(200, list_folders(q.get("path"), DL.folder))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if u.path == "/api/tracks":
                try:
                    return self.send(200, probe_tracks(q.get("url", "")))
                except Exception as e:  # noqa
                    return self.send(400, {"error": str(e), "no_ffmpeg": not FFPROBE})
            if u.path == "/api/version":
                return self.send(200, {"version": VERSION})
            if u.path == "/api/update/check":
                try:
                    return self.send(200, update_info(force=bool(q.get("force"))))
                except Exception as e:  # noqa
                    return self.send(200, {"current": VERSION, "available": False, "error": f"Couldn't reach GitHub: {e}",
                                           "page": f"https://github.com/{REPO}/releases"})
            self.send(404, {"error": "Not found"})
        except urllib.error.HTTPError as e:
            self.send(502, {"error": f"IPTV server replied HTTP {e.code}"})
        except urllib.error.URLError as e:
            self.send(502, {"error": f"Could not reach server: {e.reason}"})
        except Exception as e:  # noqa
            self.send(502, {"error": str(e)})

    def do_POST(self):
        # Only accept JSON from this app's own page (blocks other websites poking the local server)
        if not self.authorised():
            return
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send(403, {"error": "Forbidden"})
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send(415, {"error": "JSON required"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            p = urllib.parse.urlsplit(self.path).path
            if p == "/api/update/apply":
                return self.send(200, apply_update())
            if p == "/api/downloads/add":
                return self.send(200, DL.add(body.get("items")))
            if p == "/api/downloads/cancel":
                DL.cancel(body.get("id", "all"))
            elif p == "/api/downloads/retry":
                DL.retry(body.get("id", "all"))
            elif p == "/api/downloads/clear":
                DL.clear()
            elif p == "/api/folders/create":
                parent = allowed_path(body.get("path"), DL.folder)
                new = allowed_path(clean_segment(body.get("name") or "New folder"), parent)
                new.mkdir(parents=True, exist_ok=True)
                return self.send(200, list_folders(str(new), DL.folder))
            elif p == "/api/settings":
                if body.get("folder"):
                    DL.set_folder(body["folder"])
                if body.get("movies_folder"):
                    DL.set_kind_folder("movie", body["movies_folder"])
                if body.get("tv_folder"):
                    DL.set_kind_folder("tv", body["tv_folder"])
                if body.get("workers"):
                    DL.set_workers(body["workers"])
                if isinstance(body.get("prefs"), dict):
                    DL.set_prefs(body["prefs"])
                DL.save()
            elif p == "/api/open-folder":
                open_folder(DL.folder)
            else:
                return self.send(404, {"error": "Not found"})
            self.send(200, DL.snapshot())
        except Exception as e:  # noqa
            self.send(400, {"error": str(e)})


def main():
    global DL
    ap = argparse.ArgumentParser(description="IPTV Library")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--downloads", help="folder to save downloads in")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if os.environ.get("IPTV_UMASK"):           # Docker: e.g. 000 so NAS users can manage the files over SMB
        os.umask(int(os.environ["IPTV_UMASK"], 8))
    st = load_settings()
    folder = (a.downloads or st.get("folder") or os.environ.get("IPTV_DOWNLOADS")
              or str(Path.home() / "Downloads" / "IPTV Library"))
    DL = Downloader(folder, st.get("workers", 1),
                    movies=st.get("movies") or os.environ.get("IPTV_MOVIES_DIR"),
                    tv=st.get("tv") or os.environ.get("IPTV_TV_DIR"),
                    prefs=st.get("prefs") or {"audio_lang": os.environ.get("IPTV_AUDIO_LANG"),
                                               "sub_lang": os.environ.get("IPTV_SUB_LANG")})
    print(f"IPTV Library v{VERSION}  -  movies: {DL.movies}  -  TV shows: {DL.tv}")
    srv = None
    if os.environ.pop("IPTV_RESTART", None):       # restarting after an update: wait for the old copy to let go
        for _ in range(40):
            try:
                srv = ThreadingHTTPServer((a.host, a.port), Handler)
                break
            except OSError:
                time.sleep(0.5)
    try:
        srv = srv or ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError:
        # Port busy - most likely the app is already running, so just show it.
        url = f"http://127.0.0.1:{a.port}"
        try:
            running = b"IPTV Library" in fetch(url, timeout=3)
        except urllib.error.HTTPError as e:
            running = "IPTV Library" in (e.headers.get("WWW-Authenticate") or "")
        except Exception:
            running = False
        if running:
            print(f"IPTV Library is already running at {url}")
            if not a.no_browser:
                webbrowser.open(url)
            return
        srv = ThreadingHTTPServer((a.host, 0), Handler)   # some other program has the port - pick a free one
        a.port = srv.server_address[1]
    DL.restore()
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}"
    if a.host in ("0.0.0.0", "", "::"):
        print(f"IPTV Library running on port {a.port} - open http://<this-device-ip>:{a.port} on any device on your network")
    else:
        print(f"IPTV Library running at {url}  (Ctrl+C to stop)")
    if APP_PASSWORD:
        print(f"Password protection on (username: {APP_USER})")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------- Web UI
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPTV Library</title>
<style>
:root{--bg:#0e0f13;--panel:#171920;--panel2:#1f222b;--line:#2a2e3a;--text:#e9eaee;--mute:#9097a6;--acc:#e5a442;--acc2:#f3c27a;--bad:#e2625b}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer;border:1px solid var(--line);background:var(--panel2);padding:8px 14px;border-radius:8px}
button:hover{border-color:var(--acc)}
button.pri{background:var(--acc);border-color:var(--acc);color:#1a1205;font-weight:600}
button.pri:hover{background:var(--acc2)}
input,select{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px 11px;outline:none;width:100%}
input:focus,select:focus{border-color:var(--acc)}
.hidden{display:none!important}
/* login */
#login{min-height:100%;display:grid;place-items:center;padding:24px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px;width:min(440px,100%)}
.brand{font-size:22px;font-weight:700;letter-spacing:.2px;margin:0 0 4px}.brand span{color:var(--acc)}
.sub{color:var(--mute);margin:0 0 20px}
.seg{display:flex;background:var(--panel2);border-radius:9px;padding:3px;margin-bottom:16px}
.seg button{flex:1;border:0;background:none;padding:7px}.seg button.on{background:var(--bg);color:var(--acc)}
label{display:block;font-size:12px;color:var(--mute);margin:12px 0 5px}
.row{display:flex;gap:8px;align-items:center}
.chk{display:flex;gap:8px;align-items:center;margin:14px 0 18px;color:var(--mute)}.chk input{width:auto}
.err{color:var(--bad);margin-top:12px;min-height:1em}
/* app */
#app{display:grid;grid-template-columns:250px 1fr;grid-template-rows:auto 1fr;height:100vh}
header{grid-column:1/3;display:flex;align-items:center;gap:14px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
header .brand{font-size:18px;margin:0;white-space:nowrap}
.tabs{display:flex;gap:4px}.tabs button{border:0;background:none;color:var(--mute);font-weight:600}
.tabs button.on{color:var(--text);background:var(--panel2)}
header .grow{flex:1}#q{max-width:340px}#sort{width:auto}
.acct{color:var(--mute);font-size:12px;text-align:right;white-space:nowrap}
aside{border-right:1px solid var(--line);overflow:auto;padding:10px}
aside input{margin-bottom:8px;padding:7px 10px}
.cat{display:flex;justify-content:space-between;gap:8px;padding:7px 10px;border-radius:7px;cursor:pointer;color:var(--mute)}
.cat:hover{background:var(--panel2);color:var(--text)}.cat.on{background:var(--panel2);color:var(--acc)}
.cat b{font-weight:400;font-size:12px;opacity:.7}
main{overflow:auto;padding:18px}
.bar{display:flex;align-items:center;gap:10px;margin-bottom:14px;color:var(--mute)}.bar .grow{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:16px}
.tile{cursor:pointer;border-radius:10px;overflow:hidden;background:var(--panel);border:1px solid var(--line);transition:transform .12s,border-color .12s}
.tile:hover{transform:translateY(-3px);border-color:var(--acc)}
.poster{aspect-ratio:2/3;background:var(--panel2);position:relative;display:grid;place-items:center;color:var(--mute);font-size:28px;font-weight:700}
.poster img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.rate{position:absolute;top:6px;right:6px;background:#000b;color:var(--acc2);font-size:11px;padding:2px 6px;border-radius:5px;font-weight:600}
.tile .t{padding:8px 9px;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty,.loading{color:var(--mute);padding:60px 0;text-align:center}
#sentinel{height:40px}
/* modal */
.ov{position:fixed;inset:0;background:#000c;display:grid;place-items:center;padding:20px;z-index:10}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:min(900px,100%);max-height:92vh;overflow:auto;position:relative}
.hero{height:220px;background:var(--panel2) center/cover;position:relative}
.hero::after{content:"";position:absolute;inset:0;background:linear-gradient(transparent 30%,var(--panel))}
.mbody{display:flex;gap:22px;padding:0 24px 24px;margin-top:-110px;position:relative;z-index:1}
.mbody .pp{width:150px;flex:none;border-radius:10px;overflow:hidden;aspect-ratio:2/3;background:var(--panel2);box-shadow:0 8px 30px #0008}
.mbody .pp img{width:100%;height:100%;object-fit:cover}
.mbody h2{margin:60px 0 6px;font-size:24px}
.meta{color:var(--mute);display:flex;flex-wrap:wrap;gap:4px 14px;margin-bottom:10px}
.plot{color:#c9ccd4;margin:0 0 14px}
.actions{display:flex;flex-wrap:wrap;gap:8px}
.x{position:absolute;top:12px;right:12px;z-index:3;background:#000a;border:0;border-radius:50%;width:34px;height:34px;padding:0;font-size:18px}
.seasons{padding:0 24px 24px}.stabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.stabs button.on{border-color:var(--acc);color:var(--acc)}
.ep{display:flex;gap:12px;align-items:center;padding:10px;border-radius:9px;background:var(--panel2);margin-bottom:8px}
.ep .n{color:var(--acc);font-weight:700;width:34px;text-align:center;flex:none}
.ep .info{flex:1;min-width:0}.ep .info div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ep .info small{color:var(--mute)}.ep button{padding:6px 10px;font-size:12px}
.player{width:min(1100px,100%)}.player video{width:100%;max-height:80vh;background:#000;border-radius:10px;display:block}
.pmsg{color:var(--mute);margin-top:10px;text-align:center}
#dlBtn{position:relative}#dlBadge{position:absolute;top:-6px;right:-6px;background:var(--acc);color:#1a1205;font-size:11px;font-weight:700;border-radius:10px;padding:1px 6px}
#dl{position:fixed;top:0;right:0;bottom:0;width:min(460px,100%);background:var(--panel);border-left:1px solid var(--line);z-index:15;display:flex;flex-direction:column;box-shadow:-10px 0 40px #0008}
#dl .dh{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:10px}
#dl .dh h3{margin:0;font-size:17px;display:flex;justify-content:space-between;align-items:center}
#dl .dh small{color:var(--mute)}
#dl{overflow-y:auto}#dl .dh{flex:none}
#dlList{flex:1 0 auto;padding:12px 18px;display:flex;flex-direction:column;gap:10px}
.job{background:var(--panel2);border-radius:9px;padding:10px 12px}
.job .jt{display:flex;gap:8px;align-items:center}.job .jt div{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.job .jt button{padding:4px 9px;font-size:12px}
.prog{height:5px;background:var(--bg);border-radius:3px;margin:8px 0 5px;overflow:hidden}.prog i{display:block;height:100%;background:var(--acc)}
.job small{color:var(--mute);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job.done .prog i{background:#5bbf7a}.job.failed .prog i,.job.cancelled .prog i{background:var(--bad)}
#verBtn{background:none;border:0;color:var(--mute);font-size:12px;padding:4px 6px}#verBtn:hover{color:var(--text)}
#updBtn{white-space:nowrap}
.upd{padding:26px;display:flex;flex-direction:column;gap:14px}.upd h2{margin:0}
.upd pre{white-space:pre-wrap;font:inherit;color:#c9ccd4;background:var(--panel2);border-radius:9px;padding:12px 14px;margin:0;max-height:300px;overflow:auto}
.upd .note{color:var(--mute)}.upd .warn{color:var(--acc2)}
#dl .frow{display:grid;grid-template-columns:78px 1fr auto;gap:8px;align-items:center}
#dl .frow span{color:var(--mute);font-size:13px}
#picker{border:1px solid var(--line);border-radius:10px;background:var(--bg);padding:10px;display:flex;flex-direction:column;gap:8px}
#picker .ph{display:flex;align-items:center;gap:8px;font-weight:600}
#pkPath{font-size:12px;color:var(--mute);word-break:break-all}
#pkList{max-height:240px;overflow:auto;display:flex;flex-direction:column;gap:2px}
#pkList button{text-align:left;border:0;background:none;padding:8px 10px;border-radius:7px}
#pkList button:hover{background:var(--panel2)}
#pkList .empty{padding:14px;font-size:13px}
#trk{z-index:12}#trk .modal{width:min(560px,100%)}
.tk{padding:24px;display:flex;flex-direction:column;gap:12px}.tk h2{margin:0;font-size:19px;padding-right:40px}
.tk label{margin:6px 0 0}.tk .note{color:var(--mute);font-size:13px;margin:0}
#dl .prefs{display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--line);padding-top:10px}
#dl .prefs .chk{margin:0;font-size:13px}#ffNote{color:var(--acc2)}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--acc);color:#1a1205;padding:9px 16px;border-radius:8px;font-weight:600;z-index:20}
@media (max-width:760px){#app{grid-template-columns:1fr}aside{display:none}header{flex-wrap:wrap}.mbody{flex-direction:column;margin-top:-60px}.mbody h2{margin-top:0}}
</style></head><body>

<section id="login">
  <div class="card">
    <h1 class="brand">IPTV <span>Library</span></h1>
    <p class="sub">Browse the movies &amp; TV shows on your IPTV account.</p>
    <div class="seg"><button id="m-x" class="on">Xtream login</button><button id="m-m">M3U playlist URL</button></div>
    <form id="fx">
      <label>Server URL</label><input id="server" placeholder="http://example-provider.com:8080" required>
      <label>Username</label><input id="user" autocomplete="username" required>
      <label>Password</label><input id="pass" type="password" autocomplete="current-password" required>
      <label class="chk"><input type="checkbox" id="rem" checked> Remember on this computer</label>
      <button class="pri" style="width:100%">Connect</button>
    </form>
    <form id="fm" class="hidden">
      <label>Playlist URL</label><input id="m3u" placeholder="http://provider.com/get.php?username=…&password=…&type=m3u_plus" required>
      <label class="chk"><input type="checkbox" id="rem2" checked> Remember on this computer</label>
      <button class="pri" style="width:100%">Load playlist</button>
    </form>
    <div class="err" id="err"></div>
  </div>
</section>

<section id="app" class="hidden">
  <header>
    <h1 class="brand">IPTV <span>Library</span></h1>
    <button id="verBtn" title="Check for updates">v…</button>
    <button id="updBtn" class="pri hidden">⬆ Update</button>
    <div class="tabs"><button data-tab="movies" class="on">Movies</button><button data-tab="series">TV Shows</button></div>
    <input id="q" placeholder="Search…">
    <select id="sort"><option value="name">A – Z</option><option value="added">Recently added</option><option value="rating">Top rated</option></select>
    <div class="grow"></div>
    <div class="acct" id="acct"></div>
    <button id="dlBtn">⬇ Downloads<span id="dlBadge" class="hidden"></span></button>
    <button id="logout">Log out</button>
  </header>
  <aside><input id="cq" placeholder="Filter categories…"><div id="cats"></div></aside>
  <main>
    <div class="bar"><span id="count"></span><div class="grow"></div>
      <button id="expM3u">Export .m3u</button><button id="expCsv">Export .csv</button><button id="reload">Refresh</button></div>
    <div class="grid" id="grid"></div><div id="sentinel"></div>
  </main>
</section>

<div id="ov" class="ov hidden"></div>
<div id="trk" class="ov hidden"></div>

<div id="dl" class="hidden">
  <div class="dh">
    <h3>Downloads <button id="dlClose" aria-label="Close">✕</button></h3>
    <label style="margin:0">Save downloads to</label>
    <div class="frow"><span>Movies</span><input id="fMovies" aria-label="Movies folder"><button data-browse="movie">Browse…</button></div>
    <div class="frow"><span>TV Shows</span><input id="fTv" aria-label="TV Shows folder"><button data-browse="tv">Browse…</button></div>
    <div class="row"><button id="dlSave" class="pri">Save folders</button><button id="dlOpen">Open downloads</button></div>
    <div id="picker" class="hidden">
      <div class="ph"><button id="pkUp" aria-label="Up one folder">↑</button><span id="pkTitle">Choose folder</span></div>
      <div id="pkPath"></div>
      <div id="pkList"></div>
      <div class="row"><input id="pkNew" placeholder="New folder name"><button id="pkMake">Create</button></div>
      <div class="row"><button id="pkUse" class="pri">Use this folder</button><button id="pkCancel">Cancel</button></div>
    </div>
    <div class="row"><label style="margin:0;white-space:nowrap">At the same time</label>
      <select id="dlWorkers" style="width:auto"><option>1</option><option>2</option><option>3</option><option>4</option></select>
      <small>Keep within your account's connection limit</small></div>
    <div class="prefs">
      <label style="margin:0">Audio &amp; subtitles (when a title has a choice)</label>
      <div class="frow"><span>Audio</span><select id="pAudio" style="grid-column:span 2" aria-label="Preferred audio language"></select></div>
      <div class="frow"><span>Subtitles</span><select id="pSub" style="grid-column:span 2" aria-label="Preferred subtitle language"></select></div>
      <label class="chk"><input type="checkbox" id="pKeep"> Keep the other audio languages too</label>
      <label class="chk"><input type="checkbox" id="pSrt"> Also save subtitles as a separate .srt file</label>
      <small id="ffNote" class="hidden">FFmpeg isn't installed, so audio and subtitle choices can't be applied to downloads. Use the latest docker-compose.yml from GitHub (it installs FFmpeg).</small>
    </div>
    <div class="row"><button id="dlRetry">Retry failed</button><button id="dlCancel">Cancel all</button><button id="dlClear">Clear finished</button></div>
  </div>
  <div id="dlList"></div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PAGE = 120;
const S = {mode:null, server:'', user:'', pass:'', m3uUrl:'', m3u:null, tab:'movies', cache:{}, cats:[], items:[], cat:'all', view:[], shown:0};

async function api(path, params){
  const r = await fetch(path + '?' + new URLSearchParams(params));
  let d = null; try { d = await r.json(); } catch(e) {}
  if (!r.ok) throw new Error((d && d.error) || ('Error ' + r.status));
  return d;
}
const xt = (action, extra={}) => api('/api/xtream', {server:S.server, username:S.user, password:S.pass, action, ...extra});
function normServer(s){ s = s.trim(); if(!/^https?:\/\//i.test(s)) s = 'http://' + s; return new URL(s).origin; }
const enc = encodeURIComponent;
const movieUrl = (id, ext) => `${S.server}/movie/${enc(S.user)}/${enc(S.pass)}/${id}.${ext||'mp4'}`;
const epUrl = (id, ext) => `${S.server}/series/${enc(S.user)}/${enc(S.pass)}/${id}.${ext||'mp4'}`;
function toast(t){ const d=document.createElement('div'); d.className='toast'; d.textContent=t; document.body.appendChild(d); setTimeout(()=>d.remove(),1800); }
function store(k,v){ try{ v===null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); }catch(e){} }
function load(k){ try{ return JSON.parse(localStorage.getItem(k)); }catch(e){ return null; } }

/* ---------- login ---------- */
function setMode(m){ $('#m-x').classList.toggle('on', m==='x'); $('#m-m').classList.toggle('on', m==='m');
  $('#fx').classList.toggle('hidden', m!=='x'); $('#fm').classList.toggle('hidden', m!=='m'); $('#err').textContent=''; }
$('#m-x').onclick = () => setMode('x'); $('#m-m').onclick = () => setMode('m');

async function connectXtream(server, user, pass, remember){
  S.server = normServer(server); S.user = user.trim(); S.pass = pass;
  const d = await xt('');
  if (!d || !d.user_info || String(d.user_info.auth) !== '1') throw new Error('Login failed — check the server URL, username and password.');
  const ui = d.user_info;
  if (ui.status && ui.status !== 'Active') throw new Error('Account status: ' + ui.status);
  S.mode = 'xtream';
  const exp = ui.exp_date && +ui.exp_date ? new Date(ui.exp_date*1000).toLocaleDateString() : 'never';
  $('#acct').innerHTML = `${esc(S.user)}<br>Expires ${esc(exp)} · ${esc(ui.active_cons||0)}/${esc(ui.max_connections||'?')} streams`;
  if (remember) store('iptv', {mode:'xtream', server:S.server, user:S.user, pass:S.pass});
}
async function connectM3u(url, remember){
  // A get.php link carries Xtream credentials - use the richer API when possible
  try { const u = new URL(url); const un = u.searchParams.get('username'), pw = u.searchParams.get('password');
    if (un && pw) { try { await connectXtream(u.origin, un, pw, remember); return; } catch(e) {} }
  } catch(e) {}
  S.m3u = await api('/api/m3u', {url});
  S.mode = 'm3u'; S.m3uUrl = url;
  $('#acct').textContent = `Playlist · ${S.m3u.movies.length} movies · ${S.m3u.series.length} shows`;
  if (remember) store('iptv', {mode:'m3u', url});
}
async function doLogin(fn){
  const btn = document.activeElement; $('#err').textContent = '';
  document.querySelectorAll('#login button.pri').forEach(b => { b.disabled = true; b.dataset.t = b.textContent; b.textContent = 'Connecting…'; });
  try { await fn(); S.cache = {}; $('#login').classList.add('hidden'); $('#app').classList.remove('hidden'); switchTab('movies'); }
  catch(e){ $('#err').textContent = e.message; }
  finally { document.querySelectorAll('#login button.pri').forEach(b => { b.disabled = false; b.textContent = b.dataset.t; }); }
}
$('#fx').onsubmit = e => { e.preventDefault(); doLogin(() => connectXtream($('#server').value, $('#user').value, $('#pass').value, $('#rem').checked)); };
$('#fm').onsubmit = e => { e.preventDefault(); doLogin(() => connectM3u($('#m3u').value.trim(), $('#rem2').checked)); };
$('#logout').onclick = () => { store('iptv', null); location.reload(); };

/* ---------- library ---------- */
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
async function switchTab(tab, refresh){
  S.tab = tab; S.cat = 'all'; $('#q').value = ''; $('#cq').value = '';
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  $('#expM3u').classList.toggle('hidden', tab !== 'movies');
  $('#grid').innerHTML = '<div class="loading" style="grid-column:1/-1">Loading ' + (tab==='movies'?'movies':'TV shows') + '… big libraries can take a moment.</div>';
  $('#cats').innerHTML = ''; $('#count').textContent = '';
  try {
    if (refresh || !S.cache[tab]) S.cache[tab] = await fetchTab(tab, refresh);
    ({cats:S.cats, items:S.items} = S.cache[tab]);
    renderCats(); applyFilter();
  } catch(e) { $('#grid').innerHTML = `<div class="empty" style="grid-column:1/-1">Couldn't load: ${esc(e.message)}</div>`; }
}
async function fetchTab(tab, refresh){
  if (S.mode === 'xtream') {
    const mv = tab === 'movies';
    const [cats, items] = await Promise.all([xt(mv?'get_vod_categories':'get_series_categories'), xt(mv?'get_vod_streams':'get_series')]);
    return {
      cats: (Array.isArray(cats)?cats:[]).map(c => ({id:String(c.category_id), name:c.category_name})),
      items: (Array.isArray(items)?items:[]).map(it => mv
        ? {id:it.stream_id, name:it.name||'Untitled', img:it.stream_icon, rating:+it.rating||0, cat:String(it.category_id), added:+it.added||0, ext:it.container_extension}
        : {id:it.series_id, name:it.name||'Untitled', img:it.cover, rating:+it.rating||0, cat:String(it.category_id), added:+it.last_modified||0, raw:it})
    };
  }
  if (refresh) S.m3u = await api('/api/m3u', {url:S.m3uUrl, refresh:1});
  const list = tab === 'movies' ? S.m3u.movies : S.m3u.series;
  const groups = [...new Set(list.map(x => x.group))];
  return { cats: groups.map(g => ({id:g, name:g})),
           items: list.map((x,i) => ({id:i, name:x.name, img:x.logo, rating:0, cat:x.group, added:i, url:x.url, seasons:x.seasons})) };
}
function renderCats(){
  const counts = {}; S.items.forEach(i => counts[i.cat] = (counts[i.cat]||0) + 1);
  const f = $('#cq').value.toLowerCase();
  const rows = [{id:'all', name:'All', n:S.items.length}, ...S.cats.filter(c => counts[c.id]).map(c => ({...c, n:counts[c.id]}))]
    .filter(c => c.id === 'all' || !f || c.name.toLowerCase().includes(f));
  $('#cats').innerHTML = rows.map(c => `<div class="cat ${c.id===S.cat?'on':''}" data-id="${esc(c.id)}"><span>${esc(c.name)}</span><b>${c.n}</b></div>`).join('');
}
$('#cats').onclick = e => { const c = e.target.closest('.cat'); if (!c) return; S.cat = c.dataset.id; renderCats(); applyFilter(); };
$('#cq').oninput = renderCats;
let qt; $('#q').oninput = () => { clearTimeout(qt); qt = setTimeout(applyFilter, 180); };
$('#sort').onchange = applyFilter;
$('#reload').onclick = () => switchTab(S.tab, true);

function applyFilter(){
  const q = $('#q').value.trim().toLowerCase(), sort = $('#sort').value;
  let v = S.items.filter(i => (S.cat === 'all' || i.cat === S.cat) && (!q || i.name.toLowerCase().includes(q)));
  v.sort(sort === 'added' ? (a,b) => b.added - a.added : sort === 'rating' ? (a,b) => b.rating - a.rating : (a,b) => a.name.localeCompare(b.name));
  S.view = v; S.shown = 0; $('#grid').innerHTML = '';
  $('#count').textContent = `${v.length.toLocaleString()} ${S.tab === 'movies' ? 'movies' : 'shows'}`;
  if (!v.length) $('#grid').innerHTML = '<div class="empty" style="grid-column:1/-1">Nothing here.</div>';
  more();
}
const initials = n => esc((n.replace(/[^\p{L}\p{N} ]/gu,'').trim().split(/\s+/).slice(0,2).map(w=>w[0]).join('') || '?').toUpperCase());
function poster(src, name){ return `${initials(name)}${src ? `<img loading="lazy" src="${esc(src)}" onerror="this.remove()">` : ''}`; }
function more(){
  const slice = S.view.slice(S.shown, S.shown + PAGE);
  $('#grid').insertAdjacentHTML('beforeend', slice.map((it,k) => `<div class="tile" data-i="${S.shown+k}">
    <div class="poster">${poster(it.img, it.name)}${it.rating ? `<span class="rate">★ ${it.rating.toFixed(1)}</span>` : ''}</div>
    <div class="t" title="${esc(it.name)}">${esc(it.name)}</div></div>`).join(''));
  S.shown += slice.length;
}
new IntersectionObserver(es => { if (es[0].isIntersecting && S.shown < S.view.length) more(); }, {root:$('main'), rootMargin:'600px'}).observe($('#sentinel'));
$('#grid').onclick = e => { const t = e.target.closest('.tile'); if (t) (S.tab === 'movies' ? openMovie : openShow)(S.view[+t.dataset.i]); };

/* ---------- details ---------- */
function overlay(html, cls='modal'){ const o = $('#ov'); o.innerHTML = `<div class="${cls}"><button class="x" data-close>✕</button>${html}</div>`; o.classList.remove('hidden'); return o.firstChild; }
function closeOv(){ const o = $('#ov'); o.querySelectorAll('video').forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); }); o.classList.add('hidden'); o.innerHTML = ''; }
$('#ov').addEventListener('click', e => { if (e.target.id === 'ov' || e.target.closest('[data-close]')) closeOv(); });
document.addEventListener('keydown', e => { if (e.key !== 'Escape') return;
  const t = document.getElementById('trk'); if (t && !t.classList.contains('hidden')) { t.classList.add('hidden'); t.innerHTML = ''; } else closeOv(); });

/* download items are registered by key so buttons can carry them */
const DLREG = new Map(); let dlSeq = 0;
const regDl = items => { const k = 'd' + (++dlSeq); DLREG.set(k, items); return k; };
const extOf = (url, fb) => ((String(url).split('?')[0].match(/\.([a-z0-9]{2,5})$/i) || [])[1] || fb || 'mp4').toLowerCase();
const pad2 = n => String(n).padStart(2, '0');
const movieDl = (title, year, url, ext) => ({url, title, kind:'movie', path:[`${title}${year && !String(title).includes(year) ? ` (${year})` : ''}.${extOf(url, ext)}`]});
function epDl(show, season, e){
  const tag = `S${pad2(season)}E${pad2(e.num)}`;
  const name = `${show} ${tag}` + (e.title && !/S\d+\s*E\d+/i.test(e.title) && !/^Episode \d+$/i.test(e.title) ? ` - ${e.title}` : '');
  return {url:e.url, title:`${show} ${tag}`, kind:'tv', path:[show, `Season ${pad2(season)}`, `${name}.${extOf(e.url, e.ext)}`]};
}
const actBtns = (url, title, dlk) => `<button class="pri" data-play="${esc(url)}" data-title="${esc(title)}" data-dlk="${dlk || ''}">▶ Play</button>
  ${dlk ? `<button data-dl="${dlk}">⬇ Download</button>` : ''}
  ${dlk ? `<button data-trk="${dlk}">🎧 Audio &amp; subtitles</button>` : ''}
  <button data-vlc="${esc(url)}" data-title="${esc(title)}" data-dlk="${dlk || ''}">Open in VLC (.m3u)</button><button data-copy="${esc(url)}">Copy link</button>`;
function header(o){
  return `<div class="hero" style="${o.backdrop ? `background-image:url('${esc(o.backdrop)}')` : ''}"></div>
  <div class="mbody"><div class="pp">${o.img ? `<img src="${esc(o.img)}" onerror="this.remove()">` : ''}</div>
  <div><h2>${esc(o.title)}</h2><div class="meta">${o.meta.filter(Boolean).map(m => `<span>${esc(m)}</span>`).join('')}</div>
  <p class="plot">${esc(o.plot || '')}</p><div class="actions">${o.actions || ''}</div></div></div>`;
}
const first = v => Array.isArray(v) ? v[0] : v;

async function openMovie(it){
  const m = overlay(header({title:it.name, img:it.img, meta:['Loading details…']}));
  if (S.mode !== 'xtream') { m.innerHTML = '<button class="x" data-close>✕</button>' + header({title:it.name, img:it.img, meta:[it.cat],
      actions:actBtns(it.url, it.name, regDl([movieDl(it.name, '', it.url)]))}); return; }
  let info = {}, ext = it.ext;
  try { const d = await xt('get_vod_info', {vod_id:it.id}); info = d.info || {}; ext = (d.movie_data && d.movie_data.container_extension) || ext; } catch(e) {}
  const url = movieUrl(it.id, ext);
  const year = info.releasedate ? String(info.releasedate).slice(0,4) : '';
  m.innerHTML = '<button class="x" data-close>✕</button>' + header({title: info.name || it.name, img: info.movie_image || info.cover_big || it.img,
    backdrop: first(info.backdrop_path), plot: info.plot || info.description,
    meta: [info.releasedate ? String(info.releasedate).slice(0,4) : '', info.duration, info.genre, (info.rating || it.rating) ? '★ ' + (info.rating || it.rating) : '', (ext||'').toUpperCase(),
           info.director ? 'Dir. ' + info.director : '', info.cast ? 'Cast: ' + String(info.cast).split(',').slice(0,4).join(',') : ''],
    actions: actBtns(url, it.name, regDl([movieDl(it.name, /^\d{4}$/.test(year) ? year : '', url, ext)]))});
}

async function openShow(it){
  const m = overlay(header({title:it.name, img:it.img, meta:['Loading episodes…']}));
  let seasons = {}, info = {}, showName = it.name;
  if (S.mode === 'xtream') {
    try {
      const d = await xt('get_series_info', {series_id:it.id}); info = d.info || it.raw || {};
      let eps = d.episodes || {};
      if (Array.isArray(eps)) { const g = {}; eps.flat().forEach(e => (g[e.season] = g[e.season] || []).push(e)); eps = g; }
      for (const [s, list] of Object.entries(eps)) seasons[s] = (list||[]).map(e => ({num:+e.episode_num || 0, title:e.title || ('Episode ' + e.episode_num),
        plot:e.info && e.info.plot, dur:e.info && e.info.duration, ext:e.container_extension, url:epUrl(e.id, e.container_extension)}));
    } catch(e) { m.innerHTML = '<button class="x" data-close>✕</button>' + header({title:it.name, img:it.img, meta:["Couldn't load episodes: " + e.message]}); return; }
  } else seasons = it.seasons;
  const keys = Object.keys(seasons).sort((a,b) => (+a) - (+b));
  const total = keys.reduce((n,k) => n + seasons[k].length, 0);
  m.innerHTML = '<button class="x" data-close>✕</button>' + header({title:showName, img:info.cover || it.img, backdrop:first(info.backdrop_path), plot:info.plot,
    meta:[info.releaseDate || info.release_date ? String(info.releaseDate || info.release_date).slice(0,4) : '', info.genre, (info.rating || it.rating) ? '★ ' + (info.rating || it.rating) : '',
          `${keys.length} season${keys.length===1?'':'s'} · ${total} episodes`]})
    + `<div class="seasons"><div class="stabs">${keys.map((k,i) => `<button data-s="${esc(k)}" class="${i?'':'on'}">Season ${esc(k)}</button>`).join('')}
       ${total ? `<button class="pri" data-dl="${regDl(keys.flatMap(k => seasons[k].map(e => epDl(showName, k, e))))}">⬇ Download all ${total} episodes</button>
                  <button data-expall>Export all (.m3u)</button>` : ''}</div><div id="eps"></div></div>`;
  const show = k => {
    m.querySelectorAll('[data-s]').forEach(b => b.classList.toggle('on', b.dataset.s === k));
    const eps = seasons[k] || [];
    m.querySelector('#eps').innerHTML = (eps.length ? `<div class="actions" style="margin-bottom:10px">
        <button data-dl="${regDl(eps.map(e => epDl(showName, k, e)))}">⬇ Download season ${esc(k)}</button>
        <button data-expseason="${esc(k)}">Export season ${esc(k)} (.m3u)</button></div>` : '<div class="empty">No episodes.</div>')
      + eps.map(e => { const d = epDl(showName, k, e); return `<div class="ep"><div class="n">${e.num}</div>
        <div class="info"><div>${esc(e.title)}</div><small>${esc([e.dur, e.plot].filter(Boolean).join(' · '))}</small></div>${actBtns(e.url, d.title, regDl([d]))}</div>`; }).join('');
  };
  const epEntries = ks => ks.flatMap(k => seasons[k].map(e => ({title:`${showName} S${String(k).padStart(2,'0')}E${String(e.num).padStart(2,'0')} - ${e.title}`, url:e.url, group:showName})));
  m.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.s) show(b.dataset.s);
    if (b.dataset.expseason) downloadM3u(`${showName} - Season ${b.dataset.expseason}`, epEntries([b.dataset.expseason]));
    if ('expall' in b.dataset) downloadM3u(showName, epEntries(keys));
  });
  if (keys.length) show(keys[0]);
}

/* ---------- play / copy / export ---------- */
document.addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.play) play(b.dataset.play, b.dataset.title, b.dataset.dlk);
  else if (b.dataset.dl) queueDownloads(DLREG.get(b.dataset.dl));
  else if (b.dataset.trk) openTracks(b.dataset.trk);
  else if (b.dataset.vlc) { const it = b.dataset.dlk && (DLREG.get(b.dataset.dlk) || [])[0];
    downloadM3u(b.dataset.title, [{title:b.dataset.title, url:b.dataset.vlc, opts: it ? vlcOpts(it) : null}]); }
  else if (b.dataset.copy) navigator.clipboard.writeText(b.dataset.copy).then(() => toast('Link copied'), () => prompt('Copy this link:', b.dataset.copy));
});
function play(url, title, dlk){
  const p = overlay(`<video controls autoplay src="${esc(url)}"></video><div class="pmsg">${esc(title)}</div>`, 'player');
  p.querySelector('video').onerror = () => { p.querySelector('.pmsg').innerHTML = `${esc(title)}<br><span style="color:var(--bad)">Your browser can't play this file format (common with .mkv).</span>
     <div class="actions" style="justify-content:center;margin-top:10px">${actBtns(url, title, dlk).replace(/<button class="pri"[^]*?<\/button>/, '')}</div>`; };
}

/* ---------- downloads ---------- */
async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})});
  let d = null; try { d = await r.json(); } catch(e) {}
  if (!r.ok) throw new Error((d && d.error) || ('Error ' + r.status));
  return d;
}
async function queueDownloads(items){
  if (!items || !items.length) return;
  try { const r = await post('/api/downloads/add', {items});
    toast(r.added ? `Added ${r.added} to downloads${r.skipped ? ` (${r.skipped} already queued)` : ''}` : 'Already in the download queue');
    pollDl();
  } catch(e) { toast('Download failed: ' + e.message); }
}
const fmtB = n => { if (!n) return '0 B'; const u = ['B','KB','MB','GB','TB']; const i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024))); return (n / 1024 ** i).toFixed(i > 1 ? 1 : 0) + ' ' + u[i]; };
const fmtT = s => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`; };
const STATUS = {queued:'Waiting', downloading:'Downloading', processing:'Applying audio & subtitle choice…', done:'Finished', exists:'Already downloaded', failed:'Failed', cancelled:'Cancelled'};
let dlTimer = null, dlOpen = false;
function renderDl(d){
  const active = d.jobs.filter(j => ['queued', 'downloading', 'processing'].includes(j.status)).length;
  $('#dlBadge').textContent = active; $('#dlBadge').classList.toggle('hidden', !active);
  if (!dlOpen) return;
  $('#dlList').innerHTML = d.jobs.length ? d.jobs.map(j => {
    const pct = j.total ? Math.min(100, j.done / j.total * 100) : (j.status === 'done' || j.status === 'exists' ? 100 : 0);
    let line = STATUS[j.status];
    if (j.status === 'downloading') line = `${fmtB(j.done)}${j.total ? ' of ' + fmtB(j.total) : ''}` + (j.speed ? ` · ${fmtB(j.speed)}/s` : '') + (j.speed && j.total ? ` · ${fmtT((j.total - j.done) / j.speed)} left` : '') + (j.error ? ` · retrying (${j.error})` : '');
    else if (j.status === 'done' || j.status === 'exists') line += ' · ' + fmtB(j.total);
    else if (j.error) line += ' · ' + j.error;
    if (j.note && j.status !== 'downloading') line += ' · ' + j.note;
    const btn = j.status === 'queued' || j.status === 'downloading' ? `<button data-jc="${j.id}">Cancel</button>`
              : j.status === 'failed' || j.status === 'cancelled' ? `<button data-jr="${j.id}">${j.done ? 'Resume' : 'Retry'}</button>` : '';
    return `<div class="job ${j.status === 'exists' ? 'done' : j.status}"><div class="jt"><div title="${esc(j.title)}">${esc(j.title)}</div>${btn}</div>
      <div class="prog"><i style="width:${pct.toFixed(1)}%"></i></div><small>${esc(line)}</small><small title="${esc(j.dest)}">${esc(j.dest)}</small></div>`;
  }).join('') : '<div class="empty">No downloads yet.<br>Use ⬇ Download on any movie, episode or season.</div>';
}
async function pollDl(){
  clearTimeout(dlTimer);
  let active = false;
  try { const d = await api('/api/downloads', {}); renderDl(d); active = d.jobs.some(j => ['queued', 'downloading', 'processing'].includes(j.status));
    PREFS = d.prefs || PREFS; FF = !!d.ffmpeg; if (d.langs) LANGS = d.langs;
    if (dlOpen) fillPrefs();
    if (dlOpen) for (const [id, key] of [['#fMovies', 'movies_folder'], ['#fTv', 'tv_folder']])
      if (document.activeElement !== $(id) && !$(id).dataset.edited) $(id).value = d[key];
    if (dlOpen) { $('#dlWorkers').value = String(d.workers); $('#dlOpen').classList.toggle('hidden', !d.can_open); }
  } catch(e) {}
  dlTimer = setTimeout(pollDl, active || dlOpen ? 1000 : 5000);
}
$('#dlBtn').onclick = () => { dlOpen = true; $('#fMovies').dataset.edited = $('#fTv').dataset.edited = ''; $('#picker').classList.add('hidden'); $('#dl').classList.remove('hidden'); pollDl(); };
$('#dlClose').onclick = () => { dlOpen = false; $('#dl').classList.add('hidden'); };
$('#fMovies').oninput = () => $('#fMovies').dataset.edited = '1';
$('#fTv').oninput = () => $('#fTv').dataset.edited = '1';
$('#dlSave').onclick = async () => {
  try { await post('/api/settings', {movies_folder:$('#fMovies').value, tv_folder:$('#fTv').value});
        $('#fMovies').dataset.edited = $('#fTv').dataset.edited = ''; toast('Download folders saved'); pollDl(); }
  catch(e) { toast(e.message); }
};
/* folder picker */
const PK = {kind:null, path:''};
async function pkLoad(path, create){
  try {
    const d = create ? await post('/api/folders/create', {path:PK.path, name:create}) : await api('/api/folders', path ? {path} : {});
    PK.path = d.path; PK.parent = d.parent;
    $('#pkPath').textContent = d.path; $('#pkUp').disabled = !d.parent;
    $('#pkList').innerHTML = d.dirs.length ? d.dirs.map(n => `<button data-dir="${esc(n)}">📁 ${esc(n)}</button>`).join('')
      : '<div class="empty">No folders here. Use this one, or create a new folder below.</div>';
  } catch(e) { toast(e.message); }
}
document.querySelectorAll('[data-browse]').forEach(b => b.onclick = () => {
  PK.kind = b.dataset.browse; $('#pkTitle').textContent = 'Choose the ' + (PK.kind === 'movie' ? 'Movies' : 'TV Shows') + ' folder';
  $('#picker').classList.remove('hidden'); $('#pkNew').value = '';
  pkLoad((PK.kind === 'movie' ? $('#fMovies') : $('#fTv')).value);
});
$('#pkList').onclick = e => { const b = e.target.closest('[data-dir]'); if (b) pkLoad(PK.path.replace(/[\\/]+$/, '') + (PK.path.includes('\\') && !PK.path.includes('/') ? '\\' : '/') + b.dataset.dir); };
$('#pkUp').onclick = () => PK.parent && pkLoad(PK.parent);
$('#pkMake').onclick = () => { const n = $('#pkNew').value.trim(); if (n) { $('#pkNew').value = ''; pkLoad(null, n); } };
$('#pkNew').onkeydown = e => { if (e.key === 'Enter') $('#pkMake').click(); };
$('#pkCancel').onclick = () => $('#picker').classList.add('hidden');
$('#pkUse').onclick = async () => {
  const input = PK.kind === 'movie' ? $('#fMovies') : $('#fTv');
  input.value = PK.path; $('#picker').classList.add('hidden');
  try { await post('/api/settings', PK.kind === 'movie' ? {movies_folder:PK.path} : {tv_folder:PK.path});
        input.dataset.edited = ''; toast((PK.kind === 'movie' ? 'Movies' : 'TV Shows') + ' will save to ' + PK.path); pollDl(); }
  catch(e) { toast(e.message); }
};
$('#dlWorkers').onchange = () => post('/api/settings', {workers:+$('#dlWorkers').value}).then(pollDl);
$('#dlOpen').onclick = () => post('/api/open-folder').catch(e => toast(e.message));
$('#dlRetry').onclick = () => post('/api/downloads/retry', {id:'all'}).then(pollDl);
$('#dlCancel').onclick = () => post('/api/downloads/cancel', {id:'all'}).then(pollDl);
$('#dlClear').onclick = () => post('/api/downloads/clear').then(pollDl);
$('#dlList').onclick = e => { const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.jc) post('/api/downloads/cancel', {id:+b.dataset.jc}).then(pollDl);
  if (b.dataset.jr) post('/api/downloads/retry', {id:+b.dataset.jr}).then(pollDl); };
pollDl();
function save(name, text, type){
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], {type}));
  a.download = name.replace(/[\\/:*?"<>|]+/g, '_'); a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function downloadM3u(name, entries){
  const clean = s => String(s || '').replace(/[\r\n"]/g, ' ');
  const body = '#EXTM3U\n' + entries.map(x => `#EXTINF:-1${x.img ? ` tvg-logo="${clean(x.img)}"` : ''}${x.group ? ` group-title="${clean(x.group)}"` : ''},${clean(x.title)}\n`
    + (x.opts || vlcOpts({})).map(o => `#EXTVLCOPT:${o}\n`).join('') + x.url).join('\n') + '\n';
  save(name + '.m3u', body, 'audio/x-mpegurl'); toast(`Saved ${entries.length} item${entries.length===1?'':'s'}`);
}
const catName = id => (S.cats.find(c => c.id === id) || {}).name || id;
const itemUrl = it => S.mode === 'xtream' ? movieUrl(it.id, it.ext) : it.url;
$('#expM3u').onclick = () => { if (!S.view.length) return; downloadM3u(S.cat === 'all' ? 'Movies' : catName(S.cat), S.view.map(it => ({title:it.name, url:itemUrl(it), img:it.img, group:catName(it.cat)}))); };
$('#expCsv').onclick = () => {
  if (!S.view.length) return;
  const q = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const mv = S.tab === 'movies';
  const rows = [['Title','Category','Rating','Added', mv ? 'Stream URL' : 'Seasons'].map(q).join(',')]
    .concat(S.view.map(it => [it.name, catName(it.cat), it.rating || '', S.mode==='xtream' && it.added > 1e9 ? new Date(it.added*1000).toISOString().slice(0,10) : '',
       mv ? itemUrl(it) : (it.seasons ? Object.keys(it.seasons).length : '')].map(q).join(',')));
  save((mv ? 'Movies' : 'TV Shows') + '.csv', rows.join('\n'), 'text/csv'); toast(`Exported ${S.view.length} rows`);
};

/* ---------- auto login ---------- */
(async () => {
  const saved = load('iptv'); if (!saved) return;
  if (saved.mode === 'xtream') { $('#server').value = saved.server; $('#user').value = saved.user; $('#pass').value = saved.pass;
    doLogin(() => connectXtream(saved.server, saved.user, saved.pass, true)); }
  else if (saved.mode === 'm3u') { setMode('m'); $('#m3u').value = saved.url; doLogin(() => connectM3u(saved.url, true)); }
})();

/* ---------- audio & subtitles ---------- */
let PREFS = {audio_lang:'eng', sub_lang:'eng', keep_audio:false, srt:true}, FF = false, LANGS = [['eng', 'English']];
const langOpts = (sel, withOff) => (withOff ? `<option value="off"${sel === 'off' ? ' selected' : ''}>Off</option>` : '')
  + LANGS.map(([c, n]) => `<option value="${c}"${c === sel ? ' selected' : ''}>${esc(n)}</option>`).join('');
function fillPrefs(){
  if (document.activeElement !== $('#pAudio')) $('#pAudio').innerHTML = langOpts(PREFS.audio_lang, false);
  if (document.activeElement !== $('#pSub')) $('#pSub').innerHTML = langOpts(PREFS.sub_lang, true);
  $('#pKeep').checked = !!PREFS.keep_audio; $('#pSrt').checked = !!PREFS.srt;
  $('#ffNote').classList.toggle('hidden', FF);
}
const savePrefs = async () => {
  try { const d = await post('/api/settings', {prefs:{audio_lang:$('#pAudio').value, sub_lang:$('#pSub').value, keep_audio:$('#pKeep').checked, srt:$('#pSrt').checked}});
        PREFS = d.prefs; toast('Audio & subtitle defaults saved'); } catch(e) { toast(e.message); }
};
['#pAudio', '#pSub', '#pKeep', '#pSrt'].forEach(id => $(id).onchange = savePrefs);
const LANG2 = {eng:'en', spa:'es', fre:'fr', ger:'de', ita:'it', por:'pt', dut:'nl', pol:'pl', tur:'tr', ara:'ar', hin:'hi', rus:'ru', jpn:'ja', kor:'ko', chi:'zh', swe:'sv', nor:'no', dan:'da', fin:'fi', gre:'el', heb:'he', per:'fa', rum:'ro', hun:'hu', cze:'cs', ukr:'uk'};
function vlcOpts(it){
  const o = [];
  if (Number.isInteger(it.audio_n)) o.push(`audio-track=${it.audio_n}`);
  else o.push(`audio-language=${[PREFS.audio_lang, LANG2[PREFS.audio_lang]].filter(Boolean).join(',')},any`);
  if (it.subs === 'none' || (it.subs == null && PREFS.sub_lang === 'off')) o.push('sub-track=-1', 'sub-language=none');
  else if (Number.isInteger(it.subs_n)) o.push(`sub-track=${it.subs_n}`);
  else o.push(`sub-language=${[PREFS.sub_lang, LANG2[PREFS.sub_lang]].filter(Boolean).join(',')}`);
  return o;
}
const chLabel = c => ({1:'Mono', 2:'Stereo', 6:'5.1', 7:'6.1', 8:'7.1'})[c] || (c ? c + ' ch' : '');
const aLabel = a => [a.lang_name || 'Unknown language', a.title, [String(a.codec).toUpperCase(), chLabel(a.channels)].filter(Boolean).join(' ')].filter(Boolean).join(' · ') + (a.default ? ' (default)' : '');
const sLabel = x => [x.lang_name || 'Unknown language', x.title].filter(Boolean).join(' · ') + (x.forced ? ' (forced)' : '') + (x.text ? '' : ' · picture-based');
function prefA(audio){ const same = audio.filter(a => a.lang === PREFS.audio_lang);
  return (same.sort((a, b) => (b.default - a.default) || (b.channels - a.channels))[0]) || audio.find(a => a.default) || audio[0]; }
function prefS(subs){ if (PREFS.sub_lang === 'off') return null;
  return subs.filter(x => x.lang === PREFS.sub_lang).sort((a, b) => (a.forced - b.forced) || (b.text - a.text))[0] || null; }
function trkClose(){ $('#trk').classList.add('hidden'); $('#trk').innerHTML = ''; }
$('#trk').addEventListener('click', e => { if (e.target.id === 'trk' || e.target.closest('[data-tclose]')) trkClose(); });
async function openTracks(k){
  const it = (DLREG.get(k) || [])[0]; if (!it) return;
  const o = $('#trk');
  o.innerHTML = `<div class="modal"><button class="x" data-tclose aria-label="Close">✕</button><div class="tk"><h2>${esc(it.title)}</h2>
    <p class="note">Checking which audio and subtitle tracks this file has…</p></div></div>`;
  o.classList.remove('hidden');
  let tr;
  try { tr = await api('/api/tracks', {url:it.url}); }
  catch(e) { o.querySelector('.tk').innerHTML = `<h2>${esc(it.title)}</h2><p class="note">${esc(e.message)}</p>
      <p class="note">Downloads will still use your default languages from the Downloads panel where the file allows.</p>
      <div class="actions"><button data-tclose>Close</button></div>`; return; }
  const a0 = Number.isInteger(it.audio) ? tr.audio.find(a => a.index === it.audio) : prefA(tr.audio);
  const s0 = it.subs === 'none' ? null : Number.isInteger(it.subs) ? tr.subs.find(x => x.index === it.subs) : prefS(tr.subs);
  o.querySelector('.tk').innerHTML = `<h2>${esc(it.title)}</h2>
    <label for="tA">Audio</label>
    ${tr.audio.length ? `<select id="tA">${tr.audio.map(a => `<option value="${a.index}"${a0 && a.index === a0.index ? ' selected' : ''}>${esc(aLabel(a))}</option>`).join('')}</select>`
                      : '<p class="note">No separate audio tracks found.</p>'}
    <label for="tS">Subtitles</label>
    <select id="tS"><option value="none">None</option>${tr.subs.map(x => `<option value="${x.index}"${s0 && x.index === s0.index ? ' selected' : ''}>${esc(sLabel(x))}</option>`).join('')}</select>
    ${tr.subs.length ? '' : '<p class="note">This file has no subtitle tracks.</p>'}
    <p class="note">${tr.audio.length < 2 && !tr.subs.length ? 'This file only has one audio track and no subtitles, so there\'s nothing to choose.'
      : 'Your choice is used when you download this title and when you open it in VLC.' + (FF ? '' : ' (Downloads need FFmpeg to apply it.)')}
      Playing in the browser always uses the file's default track.</p>
    <div class="actions"><button class="pri" id="tDl">⬇ Download with these</button><button id="tVlc">Open in VLC</button><button data-tclose>Done</button></div>`;
  const keep = () => {
    const ai = o.querySelector('#tA') ? +o.querySelector('#tA').value : null, sv = o.querySelector('#tS').value;
    const a = tr.audio.find(x => x.index === ai), sb = tr.subs.find(x => x.index === +sv);
    it.audio = a ? a.index : null; it.audio_n = a ? a.n : null;
    it.subs = sv === 'none' ? 'none' : (sb ? sb.index : null); it.subs_n = sb ? sb.n : null;
  };
  o.querySelectorAll('select').forEach(x => x.onchange = keep); keep();
  o.querySelector('#tDl').onclick = () => { keep(); queueDownloads([it]); trkClose(); };
  o.querySelector('#tVlc').onclick = () => { keep(); downloadM3u(it.title, [{title:it.title, url:it.url, opts:vlcOpts(it)}]); };
}

/* ---------- updates ---------- */
let UPD = null;
async function checkUpdate(force){
  try { UPD = await api('/api/update/check', force ? {force:1} : {}); } catch(e) { UPD = {error:e.message}; }
  if (UPD.current) $('#verBtn').textContent = 'v' + UPD.current;
  $('#updBtn').classList.toggle('hidden', !UPD.available);
  if (UPD.available) $('#updBtn').textContent = '⬆ Update to v' + UPD.latest;
  return UPD;
}
async function showUpdate(){
  const m = overlay('<div class="upd"><h2>Updates</h2><p class="note">Checking GitHub for the latest version…</p></div>');
  const u = await checkUpdate(true);
  const busy = +($('#dlBadge').textContent || 0);
  let body;
  if (u.error) body = `<p class="warn">${esc(u.error)}</p>`;
  else if (!u.available) body = `<p>You're on the latest version (v${esc(u.current)}).</p>`;
  else body = `<p>Version <b>v${esc(u.latest)}</b> is available — you have v${esc(u.current)}.</p>
    ${u.notes ? `<pre>${esc(u.notes)}</pre>` : ''}
    ${busy ? `<p class="note">${busy} download${busy===1?' is':'s are'} in progress — ${busy===1?'it':'they'}'ll pause and carry on automatically after the update.</p>` : ''}
    ${u.reason ? `<p class="warn">${esc(u.reason)}</p>` : ''}`;
  m.innerHTML = `<button class="x" data-close>✕</button><div class="upd"><h2>Updates</h2>${body}
    <div class="actions">${u.can_apply ? '<button class="pri" id="updGo">Update now</button>' : ''}
    <button id="updPage">View on GitHub</button></div></div>`;
  m.querySelector('#updPage').onclick = () => window.open(u.page || 'https://github.com', '_blank', 'noopener');
  const go = m.querySelector('#updGo');
  if (go) go.onclick = async () => {
    go.disabled = true; go.textContent = 'Downloading update…';
    try { await post('/api/update/apply'); }
    catch(e) { go.disabled = false; go.textContent = 'Update now'; toast('Update failed: ' + e.message); return; }
    m.querySelector('.upd').innerHTML = `<h2>Updating to v${esc(u.latest)}…</h2><p class="note">The app is restarting. This page will reload by itself in a few seconds.</p>`;
    const until = Date.now() + 90000;
    const wait = async () => {
      try { const v = await api('/api/version', {}); if (v.version !== u.current) return location.reload(); } catch(e) {}
      if (Date.now() < until) setTimeout(wait, 1500);
      else m.querySelector('.upd').innerHTML += '<p class="warn">This is taking a while — close the app and open it again.</p>';
    };
    setTimeout(wait, 2500);
  };
}
$('#verBtn').onclick = showUpdate; $('#updBtn').onclick = showUpdate;
checkUpdate(false); setInterval(() => checkUpdate(false), 6 * 3600 * 1000);
</script></body></html>
"""

if __name__ == "__main__":
    main()
