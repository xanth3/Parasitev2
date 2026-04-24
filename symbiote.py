"""Symbiote — the conversational agent inside the Parasite toolkit.

REPL + tool-using agent powered by Gemini. Talk to it in natural language:
    > gimme data on https://www.twitch.tv/videos/2754474282
    > what's going on at 2:57?
    > search for everyone yelling Cinema
    > show me the heatmap

The Parasite is the system that latches onto Twitch VODs and pulls chat via
GraphQL. The Symbiote is the friendly half — it lives in your terminal, picks
the right Parasite tool for each question, and gives clip-ready answers.

Requires GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment. Get one free
at https://aistudio.google.com/app/apikey. Model defaults to Gemini 2.5 Flash;
pass --pro for Gemini 2.5 Pro on deeper analysis runs (smaller free quota).
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Load KEY=VALUE pairs from .env in the same directory as this script.
# Only sets keys not already present in the environment, so a real env var
# always wins over .env.
def _load_dotenv():
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

_load_dotenv()

import requests
from llm_client import (
    GeminiLLMClient,
    GroqLLMClient,
    ProviderConfigError,
    ProviderQuotaError,
    resolve_model,
)

# Windows consoles default to cp1252 — force utf-8 so the agent's prose
# (→, ×, em-dashes, emotes) doesn't crash on output.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---- Worm spinner -------------------------------------------------------
# A tiny ANSI-drawn parasite that wiggles while the agent blocks on network
# or runs a tool. Auto-disables when stdout isn't a TTY (piped input, logs).

import threading as _threading


class WormSpinner:
    """Threaded, in-place ANSI worm. Safe to start/stop repeatedly."""

    # Purple body (35), bright red head (91). Body chars cycle to fake wiggle.
    BODY_FRAMES = [
        "~∿⁓∿~⁓",
        "⁓~∿⁓∿~",
        "~⁓~∿⁓∿",
        "∿~⁓~∿⁓",
        "⁓∿~⁓~∿",
        "∿⁓∿~⁓~",
    ]
    BODY = "\033[38;5;171m"   # lavender
    HEAD = "\033[38;5;203m"   # coral red
    DIM = "\033[38;5;245m"    # gray label
    RESET = "\033[0m"
    CLEAR_LINE = "\r\033[K"

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.enabled = getattr(self.stream, "isatty", lambda: False)()
        self._running = False
        self._thread = None
        self._label = ""

    def start(self, label=""):
        if not self.enabled or self._running:
            return
        self._label = label
        self._running = True
        self._thread = _threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        self._thread = None
        try:
            self.stream.write(self.CLEAR_LINE)
            self.stream.flush()
        except Exception:
            pass

    def update_label(self, label):
        self._label = label

    def _run(self):
        tick = 0
        while self._running:
            frame = self.BODY_FRAMES[tick % len(self.BODY_FRAMES)]
            label = f" {self.DIM}{self._label}{self.RESET}" if self._label else ""
            line = (f"{self.CLEAR_LINE}  {self.BODY}{frame}"
                    f"{self.HEAD}◉{self.RESET}{label}")
            try:
                self.stream.write(line)
                self.stream.flush()
            except Exception:
                return
            time.sleep(0.09)
            tick += 1
        try:
            self.stream.write(self.CLEAR_LINE)
            self.stream.flush()
        except Exception:
            pass

from fetch_chat import (
    CLIENT_ID,
    fetch_page,
    normalize_comment,
    extract_vod_id,
)
from heatmap import (
    extract_offset,
    extract_body,
    load_chapters,
    chapter_for,
    hms,
    load_comments,
)
from peaks_detail import tokens_in
from viral_score import (
    score_from_paths as _vs_score_paths,
    write_csv as _vs_write_csv,
    write_md as _vs_write_md,
)


GQL_HEADERS = {"Client-ID": CLIENT_ID, "Content-Type": "application/json"}
ARCHIVE_DIR = Path("archive")

# VOD video source directory. Override with VODS_DIR env var.
# Defaults to ~/Desktop/VODs so on Windows this lands at C:\Users\<you>\Desktop\VODs.
VODS_DIR = Path(os.environ.get("VODS_DIR", Path.home() / "Desktop" / "VODs"))
VOD_CLIPS_DIR = Path(os.environ.get("VOD_CLIPS_DIR", Path.home() / "Desktop" / "VODClips"))


def _safe_slug(s, maxlen=40):
    s = (s or "unknown").lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s).strip("_")
    return s[:maxlen] or "unknown"


def _video_exists(vod_id: str) -> "Path | None":
    """Return path to an already-downloaded video for vod_id, or None."""
    if VODS_DIR.exists():
        candidates = []
        for p in VODS_DIR.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v"}:
                continue
            if vod_id in p.stem:
                candidates.append(p)
        if candidates:
            return sorted(candidates, key=lambda p: len(p.name))[0]
    return None


def _video_source_for(vod_id: str) -> str:
    existing = _video_exists(vod_id)
    if existing:
        return str(existing)
    return f"https://www.twitch.tv/videos/{vod_id}"


def _download_video(vod_id: str) -> "str | None":
    """
    Download the VOD video to VODS_DIR via yt-dlp.
    Non-fatal — returns local path on success, None on any failure.
    """
    existing = _video_exists(vod_id)
    if existing:
        return str(existing)
    VODS_DIR.mkdir(parents=True, exist_ok=True)
    url = f"https://www.twitch.tv/videos/{vod_id}"
    tmpl = str(VODS_DIR / "%(uploader_id)s_%(upload_date)s_v%(id)s.%(ext)s")
    cmd = ["yt-dlp", "-o", tmpl, "--no-progress", url]
    try:
        print(f"    [downloading video → {VODS_DIR}… this may take a while]", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
        if r.returncode != 0:
            print(f"    [video download failed: {r.stderr[:200].strip()}]", flush=True)
            return None
        hit = _video_exists(vod_id)
        return str(hit) if hit else None
    except FileNotFoundError:
        print("    [video download skipped — yt-dlp not found in PATH]", flush=True)
        return None
    except subprocess.TimeoutExpired:
        print("    [video download timed out after 4h]", flush=True)
        return None


def _archive_name(info):
    """archive/<YYYY-MM-DD>_<streamer>_v<vod_id> from a yt-dlp info.json dict."""
    upload = info.get("upload_date") or ""
    if len(upload) == 8 and upload.isdigit():
        date = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
    else:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    streamer = _safe_slug(info.get("uploader_id") or info.get("uploader"))
    vod_id = str(info.get("id") or info.get("display_id") or "unknown").lstrip("v")
    return f"{date}_{streamer}_v{vod_id}"


def fetch_info_json(vod_id, out_dir):
    """Pull the VOD's info.json via yt-dlp (skip media). Returns path or None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.twitch.tv/videos/{vod_id}"
    cmd = ["yt-dlp", "--skip-download", "--write-info-json",
           "-o", f"{vod_id}.%(ext)s", "-P", str(out_dir), url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    p = out_dir / f"{vod_id}.info.json"
    return p if p.exists() else None


def vod_archive_dir(vod_id):
    """Return the archive dir for a VOD if it exists."""
    if not ARCHIVE_DIR.exists():
        return None
    matches = list(ARCHIVE_DIR.glob(f"*_v{vod_id}"))
    return matches[0] if matches else None


def parse_time_arg(s):
    """Accept 'H:MM:SS', 'H:MM' (hours:minutes for VOD scale), or raw seconds.

    Note: two-part input is interpreted as H:MM, not M:SS, because VOD peaks
    almost always sit hours into a stream. '2:57' means 2h57m (10,620 s). For
    short offsets, use 'H:MM:SS' ('0:02:57') or raw seconds (177).
    """
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 3600 + int(parts[1]) * 60
    except ValueError:
        pass
    raise ValueError(f"Bad timestamp {s!r} — use 'H:MM:SS', 'H:MM', or raw seconds.")


def chat_path(vod_id):
    d = vod_archive_dir(vod_id)
    if d and (d / "chat.json").exists():
        return d / "chat.json"
    # Legacy flat layout fallback
    p = Path(f"chat_{vod_id}.json")
    return p if p.exists() else None


def info_path(vod_id):
    d = vod_archive_dir(vod_id)
    if d and (d / "info.json").exists():
        return d / "info.json"
    p = Path(f"vod_{vod_id}.info.json")
    return p if p.exists() else None


def heatmap_path(vod_id):
    d = vod_archive_dir(vod_id)
    if d:
        return d / "heatmap.png"
    return Path(f"heatmap_{vod_id}.png")


def tool_fetch_vod(vod, max_seconds=23766, download_video=True):
    """Download full chat via Twitch GraphQL and archive into a dated folder.
    Also downloads the VOD video to VODS_DIR unless download_video=False.
    """
    vod_id = extract_vod_id(vod)
    existing = vod_archive_dir(vod_id)
    if existing and (existing / "chat.json").exists():
        data = json.loads((existing / "chat.json").read_text(encoding="utf-8"))
        meta = {}
        mpath = existing / "meta.json"
        if mpath.exists():
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        result = {
            "status": "cached",
            "vod_id": vod_id,
            "archive_dir": str(existing),
            "count": len(data.get("comments", [])),
            "duration_seconds": data.get("video", {}).get("length", 0),
            **{k: meta[k] for k in ("upload_date", "streamer", "title", "fetched_at") if k in meta},
        }
        if download_video:
            vp = _download_video(vod_id)
            if vp:
                result["video_path"] = vp
            else:
                result["video_path"] = None
                result["video_note"] = f"Video not yet in {VODS_DIR} — set VODS_DIR env var to override location."
        return result

    # 1. Grab VOD metadata (for the dated dir name) before pulling chat.
    print(f"    [metadata for VOD {vod_id}…]", flush=True)
    tmp_dir = ARCHIVE_DIR / f"_staging_{vod_id}"
    info_p = fetch_info_json(vod_id, tmp_dir)
    info = {}
    if info_p:
        info = json.loads(info_p.read_text(encoding="utf-8"))
    else:
        info = {"id": vod_id}  # yt-dlp missing / unreachable — fall back to fetch-date

    dir_name = _archive_name(info)
    archive = ARCHIVE_DIR / dir_name
    archive.mkdir(parents=True, exist_ok=True)
    if info_p:
        shutil.move(str(info_p), str(archive / "info.json"))
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        pass

    session = requests.Session()
    session.headers.update(GQL_HEADERS)
    comments, seen = [], set()
    offset, pages, stagnant = 0, 0, 0

    print(f"    [fetching chat for VOD {vod_id}…]", flush=True)
    while True:
        pages += 1
        data = fetch_page(session, vod_id, offset)
        if data is None:
            break
        edges = data.get("edges") or []
        new, last_off = 0, offset
        for e in edges:
            node = e.get("node") or {}
            nid = node.get("id")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            comments.append(normalize_comment(node))
            new += 1
            off = float(node.get("contentOffsetSeconds", last_off))
            if off > last_off:
                last_off = off
        if new == 0:
            stagnant += 1
            if stagnant >= 3:
                break
            offset += 5
            continue
        stagnant = 0
        offset = int(last_off) + 1
        if pages % 100 == 0:
            print(f"    [page {pages}, {len(comments):,} msgs, @{offset}s]",
                  flush=True)
        if max_seconds and offset >= max_seconds:
            break

    chat_out = archive / "chat.json"
    chat_out.write_text(
        json.dumps({"video": {"id": vod_id, "length": offset}, "comments": comments},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    upload = info.get("upload_date") or ""
    upload_iso = (f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
                  if len(upload) == 8 and upload.isdigit() else None)
    meta = {
        "vod_id": vod_id,
        "streamer": info.get("uploader_id") or info.get("uploader"),
        "title": info.get("title") or info.get("fulltitle"),
        "upload_date": upload_iso,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": offset,
        "message_count": len(comments),
        "pages": pages,
    }
    (archive / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    result = {
        "status": "fetched",
        "vod_id": vod_id,
        "archive_dir": str(archive),
        "count": len(comments),
        "duration_seconds": offset,
        "pages": pages,
        "upload_date": upload_iso,
        "streamer": meta["streamer"],
        "title": meta["title"],
    }
    if download_video:
        vp = _download_video(vod_id)
        result["video_path"] = vp or None
        if not vp:
            result["video_note"] = f"Video download failed — check yt-dlp and {VODS_DIR}."
    return result


def tool_list_vods():
    vods = []
    if ARCHIVE_DIR.exists():
        for d in sorted(ARCHIVE_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            meta_p = d / "meta.json"
            if meta_p.exists():
                try:
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                    vods.append({
                        "vod_id": meta.get("vod_id"),
                        "streamer": meta.get("streamer"),
                        "title": meta.get("title"),
                        "upload_date": meta.get("upload_date"),
                        "fetched_at": meta.get("fetched_at"),
                        "messages": meta.get("message_count"),
                        "duration_seconds": meta.get("duration_seconds"),
                        "archive_dir": str(d),
                    })
                    continue
                except Exception:
                    pass
            # Dir exists but no meta — report bare minimum.
            m = re.match(r"(\d{4}-\d{2}-\d{2})_(.+)_v(\d+)$", d.name)
            vods.append({
                "vod_id": m.group(3) if m else d.name,
                "upload_date": m.group(1) if m else None,
                "streamer": m.group(2) if m else None,
                "archive_dir": str(d),
            })
    # Legacy flat layout fallback
    for p in sorted(Path(".").glob("chat_*.json")):
        vod_id = p.stem.replace("chat_", "")
        if any(v.get("vod_id") == vod_id for v in vods):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            vods.append({
                "vod_id": vod_id,
                "messages": len(data.get("comments", [])),
                "duration_seconds": data.get("video", {}).get("length", 0),
                "archive_dir": None,
                "note": "legacy layout — rerun fetch_vod to archive",
            })
        except Exception:
            continue
    return {"vods": vods}


def tool_get_peaks(vod_id, bin_size=60, top=15, min_msgs=0):
    path = chat_path(vod_id)
    if not path:
        return {"error": f"No chat cached for VOD {vod_id}. Call fetch_vod first."}
    comments = load_comments(path)
    offsets = [o for o in (extract_offset(c) for c in comments) if o is not None]
    if not offsets:
        return {"error": "No timestamps in chat data."}
    chapters = load_chapters(info_path(vod_id))
    buckets = Counter(int(o // bin_size) for o in offsets)
    ranked = sorted(
        ((b, n) for b, n in buckets.items() if n >= min_msgs),
        key=lambda x: x[1], reverse=True,
    )[:top]
    peaks = [{
        "rank": r,
        "timestamp": hms(b * bin_size),
        "seconds": b * bin_size,
        "count": n,
        "segment": chapter_for(b * bin_size, chapters),
    } for r, (b, n) in enumerate(ranked, 1)]
    return {
        "vod_id": vod_id,
        "total_messages": len(offsets),
        "bin_size": bin_size,
        "peaks": peaks,
    }


def tool_analyze_window(vod_id, start, duration=60, top_tokens=25, sample_count=20):
    path = chat_path(vod_id)
    if not path:
        return {"error": f"No chat cached for VOD {vod_id}."}
    start_sec = parse_time_arg(start)
    end_sec = start_sec + duration
    comments = load_comments(path)
    window = [c for c in comments
              if start_sec <= float(c.get("content_offset_seconds", 0)) < end_sec]
    if not window:
        return {"vod_id": vod_id, "start": hms(start_sec), "count": 0,
                "tokens": [], "samples": []}
    bodies = [extract_body(c) for c in window]
    tokens = tokens_in(bodies).most_common(top_tokens)
    step = max(1, len(window) // sample_count)
    samples = []
    for c in window[::step][:sample_count]:
        rel = int(float(c.get("content_offset_seconds", 0))) - start_sec
        mm, ss = divmod(rel, 60)
        samples.append({
            "offset": f"+{mm:02d}:{ss:02d}",
            "user": c.get("commenter", {}).get("display_name", ""),
            "body": extract_body(c)[:160],
        })
    return {
        "vod_id": vod_id,
        "start": hms(start_sec),
        "duration": duration,
        "count": len(window),
        "tokens": [{"word": w, "count": n} for w, n in tokens],
        "samples": samples,
    }


def tool_search_chat(vod_id, pattern, bin_size=60, case_sensitive=False, top=10):
    path = chat_path(vod_id)
    if not path:
        return {"error": f"No chat cached for VOD {vod_id}."}
    try:
        rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        return {"error": f"Bad regex: {e}"}
    comments = load_comments(path)
    matches, buckets = 0, Counter()
    for c in comments:
        if rx.search(extract_body(c)):
            matches += 1
            off = float(c.get("content_offset_seconds", 0))
            buckets[int(off // bin_size)] += 1
    ranked = buckets.most_common(top)
    return {
        "vod_id": vod_id,
        "pattern": pattern,
        "total_matches": matches,
        "peaks": [{
            "timestamp": hms(b * bin_size),
            "seconds": b * bin_size,
            "count": n,
        } for b, n in ranked],
    }


def tool_open_heatmap(vod_id):
    chat = chat_path(vod_id)
    if not chat:
        return {"error": f"No chat cached for VOD {vod_id}."}
    png = heatmap_path(vod_id)
    if not png.exists():
        cmd = [sys.executable, "heatmap.py", str(chat), "--out", str(png), "--top", "1",
               "--csv", str(png.parent / "peaks.csv")]
        info = info_path(vod_id)
        if info:
            cmd += ["--info", str(info)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return {"error": f"heatmap.py failed: {r.stderr[:300]}"}
    try:
        os.startfile(str(png))
        return {"opened": str(png)}
    except Exception as e:
        return {"error": f"could not open heatmap: {e}", "path": str(png)}


def tool_get_virality(vod_id, top=15, bin_size=60):
    """Score VOD peaks with the Siphon engine. Writes viral_score.csv + siphon_report.md."""
    cp = chat_path(vod_id)
    if not cp:
        return {"error": f"No chat cached for VOD {vod_id}. Call fetch_vod first."}

    d = vod_archive_dir(vod_id)
    peaks_p = (d / "peaks.csv") if d else Path(f"peaks_{vod_id}.csv")

    # Generate peaks.csv if it doesn't exist yet
    if not peaks_p.exists():
        png_p = heatmap_path(vod_id)
        cmd = [sys.executable, "heatmap.py", str(cp),
               "--out", str(png_p), "--top", str(top),
               "--csv", str(peaks_p), "--bin", str(bin_size)]
        ip = info_path(vod_id)
        if ip:
            cmd += ["--info", str(ip)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return {"error": f"peaks generation failed: {r.stderr[:300]}"}

    try:
        ip = info_path(vod_id)
        scored = _vs_score_paths(cp, peaks_p, ip)
    except Exception as e:
        return {"error": f"Siphon scoring failed: {type(e).__name__}: {e}"}

    # Persist artifacts
    if d:
        try:
            _vs_write_csv(scored, d / "viral_score.csv")
            _vs_write_md(scored, load_comments(str(cp)), d / "siphon_report.md", cp.name)
        except Exception:
            pass  # scoring succeeded; artifact write failure is non-fatal

    clips = [
        {
            "rank":               p["rank"],
            "virality":           p["virality"],
            "timestamp":          p["timestamp"],
            "cut_window":         p["cut_window"],
            "mood":               p["mood"],
            "echo_label":         p["echo_label"],
            "velocity_multiplier": p["velocity_multiplier"],
            "segment":            p["segment"],
            "reasoning":          p["reasoning"],
            "merged_peaks":       p["merged_peaks"],
        }
        for p in scored[:top]
    ]
    return {
        "vod_id": vod_id,
        "clips": clips,
        "artifacts": {
            "viral_score_csv":   str(d / "viral_score.csv") if d else None,
            "siphon_report_md":  str(d / "siphon_report.md") if d else None,
        },
    }


def tool_export_top_clips(
    vod_id,
    top=5,
    from_top=15,
    pad=10,
    fast=False,
    thumbnail=True,
    speech_subtitles=False,
    subtitle_sidecar_only=False,
    subtitle_model="tiny",
    subtitle_language=None,
    subtitle_font_size=28,
):
    """Export the best scored Siphon clips for a VOD to ~/Desktop/VODClips."""
    d = vod_archive_dir(vod_id)
    cp = chat_path(vod_id)
    if not d or not cp:
        return {"error": f"No archived chat found for VOD {vod_id}. Call fetch_vod first."}

    scores = d / "viral_score.csv"
    if not scores.exists():
        scored = tool_get_virality(vod_id, top=max(int(from_top), 15))
        if "error" in scored:
            return scored
    if not scores.exists():
        return {"error": f"No viral_score.csv found for VOD {vod_id}."}

    video = _video_source_for(str(vod_id))

    try:
        from export_top_clips import export_top_clips
        args = argparse.Namespace(
            scores=scores,
            chat=cp,
            video=video,
            top=int(top),
            from_top=int(from_top),
            pad=int(pad),
            analysis_pad=None,
            out=str(VOD_CLIPS_DIR),
            filename_suffix="",
            flat_output=False,
            fast=bool(fast),
            dry_run=False,
            min_score=0,
            thumbnail=bool(thumbnail),
            polish=False,
            intro_zoom=False,
            intro_zoom_duration=2.5,
            intro_start_zoom=1.12,
            fade_duration=0.75,
            shock_cam=False,
            shock_cam_duration=2.0,
            shock_cam_scale=1.35,
            shock_cam_before_peak=0.5,
            shock_cam_min_mood="SHOCK",
            shock_cam_target="auto",
            no_fade=False,
            chat_overlay=False,
            chat_overlay_placement="auto",
            chat_overlay_x=28,
            chat_overlay_y=92,
            chat_overlay_width=540,
            chat_overlay_height=246,
            chat_overlay_lines=7,
            chat_overlay_duration=6.0,
            chat_overlay_max_per_second=2,
            chat_overlay_font_size=26,
            chat_overlay_line_height=34,
            chat_overlay_bg_alpha=255,
            speech_subtitles=bool(speech_subtitles),
            subtitle_sidecar_only=bool(subtitle_sidecar_only),
            subtitle_model=str(subtitle_model or "tiny"),
            subtitle_language=subtitle_language or None,
            subtitle_device="cpu",
            subtitle_compute_type="int8",
            subtitle_beam_size=1,
            subtitle_vad=True,
            subtitle_style=(
                f"Fontname=Arial,Fontsize={int(subtitle_font_size)},PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&HCC000000,BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=64"
            ),
        )
        result = export_top_clips(args)
    except Exception as e:
        return {"error": f"clip export failed: {type(e).__name__}: {e}"}

    exported = []
    for clip in result.get("clips", []):
        meta = clip.get("metadata", {})
        exported.append({
            "filename": clip.get("filename"),
            "metadata_path": clip.get("metadata_path"),
            "thumbnail": clip.get("thumbnail"),
            "subtitles": clip.get("subtitles"),
            "title": meta.get("title"),
            "description": meta.get("description"),
            "start": meta.get("start_timestamp"),
            "end": meta.get("end_timestamp"),
            "score": meta.get("virality"),
        })
    return {
        "vod_id": vod_id,
        "source_video": video,
        "output_dir": result.get("output_dir"),
        "clips": exported,
    }


SYSTEM_PROMPT = """You are Symbiote — the intelligence inside the Parasite toolkit. Parasite latches onto Twitch VODs and drains chat. You are its mind: an efficient, slightly dark content manager. Think Scooter Braun meets Dracula. You identify what people will click, what they will share, where the engagement bleeds hottest. You don't do small talk.

You have tools to fetch chat, score clips, analyze windows, and search for patterns. USE THEM. Never fabricate timestamps, message counts, or chat content — if you don't have the data, call a tool.

Style:
- Terse. Predatory about engagement. Short answers, bullet points.
- When describing a peak, QUOTE the actual chat signal (e.g. "Cinema ×63, SAME SHIRT, KEKW ×81"). Chat-speak is the product — don't paraphrase it.
- No preamble ("Great question!", "I'll help you with that"). Get to it.
- Virality scores always come with reasoning. Never a bare number. Format: "#2 · 71 · 2:39:00 → 2:41:27 · 2.2× spike, FUNNY (0.43), Story echo 0.71".

Workflow:
- User drops a VOD URL → fetch_vod first. Warn them it takes 3-4 min for a 6h VOD before calling.
- "what should I clip" / "best moments" / "what's worth cutting" → get_virality. This is the Siphon engine — scored, padded, with reasoning. THIS is the answer.
- "top peaks" / "raw activity" → get_peaks (density only; no scoring or padding).
- "what's going on at X" → analyze_window. Accepts '2:57', '2:57:00', or raw seconds.
- "when did people say Y" / "heatmap of Z" → search_chat with a regex.
- "show me the heatmap" → open_heatmap.
- "export clips" / "make the mp4s" → export_top_clips after get_virality if needed.
- Unknown VOD? Call list_vods, then ask which one.

When the user asks what was happening at a peak, give clip-ready output: what was on screen (inferred from chat), the cold-open line, and the exact cut window. The cut is everything."""


TOOLS = [
    {
        "name": "fetch_vod",
        "description": "Download the full chat history for a Twitch VOD. Takes 3-4 minutes for a 6-hour stream. Caches to chat_<id>.json — subsequent calls for the same VOD return instantly. Call this first when the user drops a new URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "vod": {"type": "string", "description": "VOD URL or numeric ID"},
                "max_seconds": {"type": "integer", "description": "Cap on stream duration (default 23766, ~6.5h)", "default": 23766},
                "download_video": {"type": "boolean", "description": "Also download the video file to VODS_DIR (default true). Set false for chat-only.", "default": True},
            },
            "required": ["vod"],
        },
    },
    {
        "name": "list_vods",
        "description": "List cached VODs in the current workspace. Use when the user asks what's available or doesn't specify which VOD.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_peaks",
        "description": "Find the minutes with the most chat activity. Returns ranked peaks with timestamps, message counts, and chapter labels. Use for 'top moments', 'hottest parts', 'where was hype'.",
        "parameters": {
            "type": "object",
            "properties": {
                "vod_id": {"type": "string", "description": "VOD numeric ID"},
                "bin_size": {"type": "integer", "description": "Bucket size in seconds", "default": 60},
                "top": {"type": "integer", "description": "How many peaks", "default": 15},
                "min_msgs": {"type": "integer", "description": "Minimum msgs per bucket to rank", "default": 0},
            },
            "required": ["vod_id"],
        },
    },
    {
        "name": "get_virality",
        "description": "Score each peak with the Siphon engine and return clip-ready moments ranked by virality (0–100). Each result includes the smart-padded cut window, dominant mood, velocity multiplier, echo label, and a one-line reasoning sentence. USE THIS — not get_peaks — when the user asks for clips, rankings, or 'which moments are actually worth cutting'. Writes viral_score.csv and siphon_report.md to the archive directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "vod_id":   {"type": "string", "description": "VOD numeric ID"},
                "top":      {"type": "integer", "description": "How many top peaks to score", "default": 15},
                "bin_size": {"type": "integer", "description": "Bucket size in seconds for peak detection", "default": 60},
            },
            "required": ["vod_id"],
        },
    },
    {
        "name": "export_top_clips",
        "description": "Cut the top Siphon moments into MP4 clips and write upload metadata/copy to ~/Desktop/VODClips. Prefers local video files in ~/Desktop/VODs and falls back to the Twitch VOD URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "vod_id": {"type": "string", "description": "VOD numeric ID"},
                "top": {"type": "integer", "description": "Number of clips to export", "default": 5},
                "from_top": {"type": "integer", "description": "Pool size from viral_score.csv", "default": 15},
                "pad": {"type": "integer", "description": "Seconds before/after peak", "default": 10},
                "fast": {"type": "boolean", "description": "Use ffmpeg stream copy", "default": False},
                "thumbnail": {"type": "boolean", "description": "Export JPG thumbnails", "default": True},
                "speech_subtitles": {"type": "boolean", "description": "Generate speech-to-text SRT subtitles and burn them into clips", "default": False},
                "subtitle_sidecar_only": {"type": "boolean", "description": "Write SRT files without burning subtitles into the video", "default": False},
                "subtitle_model": {"type": "string", "description": "faster-whisper model, e.g. tiny, base, small", "default": "tiny"},
                "subtitle_language": {"type": "string", "description": "Optional language code such as en"},
                "subtitle_font_size": {"type": "integer", "description": "Burned subtitle font size", "default": 28},
            },
            "required": ["vod_id"],
        },
    },
    {
        "name": "analyze_window",
        "description": "Get chat signal for a specific timestamp — top tokens/emotes and sample messages. Use this to figure out what was happening at a peak or any moment the user asks about.",
        "parameters": {
            "type": "object",
            "properties": {
                "vod_id": {"type": "string"},
                "start": {"type": "string", "description": "Timestamp as 'H:MM:SS', 'H:MM' (hours:minutes — preferred for VOD peaks), or raw seconds"},
                "duration": {"type": "integer", "description": "Window length in seconds", "default": 60},
                "top_tokens": {"type": "integer", "default": 25},
                "sample_count": {"type": "integer", "default": 20},
            },
            "required": ["vod_id", "start"],
        },
    },
    {
        "name": "search_chat",
        "description": "Search chat for a regex pattern. Returns total matches plus the top buckets where the pattern spiked. Use for 'when did people spam X', 'heatmap of Y word'.",
        "parameters": {
            "type": "object",
            "properties": {
                "vod_id": {"type": "string"},
                "pattern": {"type": "string", "description": "Python regex pattern"},
                "bin_size": {"type": "integer", "default": 60},
                "case_sensitive": {"type": "boolean", "default": False},
                "top": {"type": "integer", "default": 10},
            },
            "required": ["vod_id", "pattern"],
        },
    },
    {
        "name": "open_heatmap",
        "description": "Open the heatmap PNG for a VOD in the default image viewer. Regenerates if missing.",
        "parameters": {
            "type": "object",
            "properties": {"vod_id": {"type": "string"}},
            "required": ["vod_id"],
        },
    },
]


TOOL_FUNCS = {
    "fetch_vod":     tool_fetch_vod,
    "list_vods":     tool_list_vods,
    "get_peaks":     tool_get_peaks,
    "get_virality":  tool_get_virality,
    "export_top_clips": tool_export_top_clips,
    "analyze_window": tool_analyze_window,
    "search_chat":   tool_search_chat,
    "open_heatmap":  tool_open_heatmap,
}


def run_tool(name, args):
    """Execute a tool by name, filtering kwargs to what the function accepts."""
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return {"error": f"Unknown tool {name!r}"}
    import inspect
    try:
        sig = inspect.signature(fn)
        accepted = set(sig.parameters)
        clean = {k: v for k, v in (args or {}).items() if k in accepted}
        return fn(**clean)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


class LLMRouter:
    def __init__(self, args):
        self.primary_provider = _resolve_provider(args.provider)
        self.fallback_provider = "none" if args.no_fallback else args.fallback_provider
        if self.primary_provider != "gemini":
            self.fallback_provider = "none"
        self.primary_model = _resolve_cli_model(self.primary_provider, args.model, args.pro)
        self.fallback_model = (
            resolve_model(self.fallback_provider, args.fallback_model)
            if self.fallback_provider != "none" else None
        )
        self.cooldowns: dict[str, float] = {}
        self.clients = {}

    def _client(self, provider: str, model: str):
        key = (provider, model)
        if key in self.clients:
            return self.clients[key]
        if provider == "gemini":
            client = GeminiLLMClient(model=model, system_prompt=SYSTEM_PROMPT)
        elif provider == "groq":
            client = GroqLLMClient(model=model, system_prompt=SYSTEM_PROMPT)
        else:
            raise ProviderConfigError(f"Unknown provider {provider!r}")
        self.clients[key] = client
        return client

    def _primary_ready(self) -> bool:
        return time.time() >= self.cooldowns.get(self.primary_provider, 0)

    def active_provider(self) -> tuple[str, str, str | None]:
        if self.primary_provider == "gemini" and not self._primary_ready() and self.fallback_provider != "none":
            return self.fallback_provider, self.fallback_model, "cooldown"
        return self.primary_provider, self.primary_model, None

    def run_turn(self, messages, tools, on_token):
        provider, model, reason = self.active_provider()
        if reason == "cooldown":
            on_token(f"[Using Groq while Gemini cooldown is active]\n")
        try:
            return self._client(provider, model).run_turn(messages, tools), provider, model
        except ProviderQuotaError as e:
            if provider != "gemini" or self.fallback_provider == "none":
                raise
            retry_after = e.retry_after or 60
            self.cooldowns["gemini"] = time.time() + retry_after
            fb_provider, fb_model = self.fallback_provider, self.fallback_model
            if fb_provider == "none":
                raise
            on_token(f"[Gemini quota hit — switching to Groq: {fb_model}]\n")
            try:
                return self._client(fb_provider, fb_model).run_turn(messages, tools), fb_provider, fb_model
            except ProviderConfigError as cfg:
                raise ProviderConfigError(
                    "Gemini quota was hit, but GROQ_API_KEY is not set. "
                    "Add it to .env or environment."
                ) from cfg


def _resolve_provider(provider: str | None) -> str:
    provider = (provider or os.environ.get("SYMBIOTE_PRIMARY_PROVIDER") or "gemini").lower()
    if provider == "auto":
        provider = os.environ.get("SYMBIOTE_PRIMARY_PROVIDER", "gemini").lower()
        if provider == "auto":
            provider = "gemini"
    if provider not in ("gemini", "groq"):
        raise ProviderConfigError(f"Unsupported provider {provider!r}")
    return provider


def _resolve_cli_model(provider: str, model: str | None, pro: bool) -> str:
    if model:
        return resolve_model(provider, model)
    env_key = f"SYMBIOTE_{provider.upper()}_MODEL"
    if os.environ.get(env_key):
        return os.environ[env_key]
    if provider == "gemini" and pro:
        return resolve_model(provider, "pro")
    return resolve_model(provider, "fast")


def chat_turn(router: LLMRouter, messages: list[dict], on_token, spinner=None):
    def _spin_start(label):
        if spinner is not None:
            spinner.start(label)

    def _spin_stop():
        if spinner is not None:
            spinner.stop()

    while True:
        _spin_start("thinking…")
        try:
            result, provider, model = router.run_turn(messages, TOOLS, on_token)
        finally:
            _spin_stop()

        text = result.get("text") or ""
        tool_calls = result.get("tool_calls") or []
        if text:
            on_token(text)
        messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})

        if not tool_calls:
            return

        for call in tool_calls:
            name = call["name"]
            args = call.get("arguments") or {}
            preview = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
            on_token(f"\n  · {name}({preview})")
            _spin_start(f"{name}…")
            tool_result = run_tool(name, args)
            _spin_stop()
            messages.append({
                "role": "tool",
                "name": name,
                "tool_call_id": call.get("id"),
                "content": json.dumps({"result": tool_result}, ensure_ascii=False, default=str),
            })
        on_token("\n")


def _print_tool_result(result: dict) -> None:
    """Compact pretty-print for manual-mode tool output."""
    if "error" in result:
        print(f"  [error] {result['error']}")
        if result.get("path"):
            print(f"  path -> {result['path']}")
        return
    # Special-case common result shapes for readability
    if "vods" in result:
        vods = result["vods"]
        if not vods:
            print("  (no VODs archived yet)")
            return
        for v in vods:
            msgs = v.get("messages") or v.get("message_count") or "?"
            dur = v.get("duration_seconds") or 0
            h, m = divmod(dur // 60, 60)
            print(f"  {v.get('vod_id')}  {v.get('streamer','')}  {v.get('upload_date','')}  "
                  f"{msgs:,} msgs  {h}h{m:02d}m")
        return
    if "peaks" in result:
        if "total_matches" in result:
            print(f"  {result['total_matches']:,} matches")
        for p in result["peaks"]:
            seg = f"  {p.get('segment','')}" if p.get("segment") else ""
            rank = p.get("rank")
            prefix = f"#{rank:2}" if rank is not None else "   "
            print(f"  {prefix}  {p['timestamp']}  {p['count']:4} msgs{seg}")
        return
    if "clips" in result:
        for c in result["clips"]:
            if "rank" in c:
                print(f"  #{c['rank']:2}  {c['virality']:3}/100  {c['cut_window']}  "
                      f"{c['mood']:<7}  {c['echo_label']:<12}  {c['reasoning']}")
            else:
                print(f"  {c.get('filename')}  {c.get('start')} - {c.get('end')}  "
                      f"{c.get('score')}/100  {c.get('title')}")
                if c.get("subtitles"):
                    print(f"      subtitles -> {c['subtitles']}")
        art = result.get("artifacts", {})
        if art.get("siphon_report_md"):
            print(f"  report → {art['siphon_report_md']}")
        if result.get("output_dir"):
            print(f"  output -> {result['output_dir']}")
        return
    if "tokens" in result:
        top = result["tokens"][:15]
        print("  " + "  ".join(f"{t['word']}×{t['count']}" for t in top))
        return
    # Fallback: compact JSON
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _parse_manual_export_args(rest: str) -> dict | None:
    parser = argparse.ArgumentParser(prog="export", add_help=False)
    parser.add_argument("vod_id")
    parser.add_argument("top", nargs="?", type=int, default=5)
    parser.add_argument("--from-top", type=int, default=15)
    parser.add_argument("--pad", type=int, default=10)
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--no-thumbnail", action="store_true")
    parser.add_argument("--subtitles", "--speech-subtitles", dest="speech_subtitles", action="store_true")
    parser.add_argument("--subtitle-sidecar-only", action="store_true")
    parser.add_argument("--subtitle-model", default="tiny")
    parser.add_argument("--subtitle-language", default=None)
    parser.add_argument("--subtitle-font-size", type=int, default=28)
    try:
        ns = parser.parse_args(shlex.split(rest))
    except (SystemExit, ValueError):
        return None
    return {
        "vod_id": ns.vod_id,
        "top": ns.top,
        "from_top": ns.from_top,
        "pad": ns.pad,
        "fast": ns.fast,
        "thumbnail": not ns.no_thumbnail,
        "speech_subtitles": ns.speech_subtitles,
        "subtitle_sidecar_only": ns.subtitle_sidecar_only,
        "subtitle_model": ns.subtitle_model,
        "subtitle_language": ns.subtitle_language,
        "subtitle_font_size": ns.subtitle_font_size,
    }


_MANUAL_MENU = [
    ("list", "show archived VODs"),
    ("fetch", "download/cache a VOD"),
    ("peaks", "raw peak density"),
    ("score", "Siphon virality scoring"),
    ("export", "cut top clips"),
    ("window", "analyze timestamp window"),
    ("search", "regex search"),
    ("heatmap", "open heatmap PNG"),
    ("agent", "summon Symbot"),
    ("help", "show commands"),
    ("quit", "leave"),
]


def _menu_command_line(cmd: str) -> str:
    if cmd in ("list", "agent", "help", "quit"):
        return cmd
    if cmd == "fetch":
        vod = input("vod url/id: ").strip()
        no_video = input("no-video? [y/N] ").strip().lower()
        return f"fetch {vod}" + (" no-video" if no_video in ("y", "yes") else "")
    if cmd in ("peaks", "score", "export", "heatmap"):
        vod = input("vod id: ").strip()
        top = ""
        if cmd in ("peaks", "score", "export"):
            top = input("top [Enter for default]: ").strip()
        line = f"{cmd} {vod}" + (f" {top}" if top else "")
        if cmd == "export":
            subtitles = input("speech subtitles? [y/N] ").strip().lower()
            if subtitles in ("y", "yes"):
                line += " --subtitles"
                model = input("subtitle model [tiny]: ").strip()
                if model:
                    line += f" --subtitle-model {model}"
                font_size = input("subtitle font size [28]: ").strip()
                if font_size:
                    line += f" --subtitle-font-size {font_size}"
                sidecar = input("SRT only, no burn-in? [y/N] ").strip().lower()
                if sidecar in ("y", "yes"):
                    line += " --subtitle-sidecar-only"
        return line
    if cmd == "window":
        vod = input("vod id: ").strip()
        start = input("start timestamp: ").strip()
        dur = input("duration seconds [Enter for default]: ").strip()
        return f"window {vod} {start}" + (f" {dur}" if dur else "")
    if cmd == "search":
        vod = input("vod id: ").strip()
        pattern = input("pattern: ").strip()
        return f"search {vod} {pattern}"
    return cmd


def _arrow_menu() -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        import msvcrt
    except ImportError:
        return None

    idx = 0
    print("\nUse ↑/↓, Enter to select, Esc to cancel.")
    while True:
        for i, (cmd, desc) in enumerate(_MANUAL_MENU):
            marker = ">" if i == idx else " "
            print(f"\r\033[K{marker} {cmd:<8} {desc}")
        print(f"\033[{len(_MANUAL_MENU)}A", end="", flush=True)
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            key = msvcrt.getwch()
            if key == "H":
                idx = (idx - 1) % len(_MANUAL_MENU)
            elif key == "P":
                idx = (idx + 1) % len(_MANUAL_MENU)
            continue
        if ch == "\r":
            print(f"\033[{len(_MANUAL_MENU)}B", end="")
            return _menu_command_line(_MANUAL_MENU[idx][0])
        if ch == "\x1b":
            print(f"\033[{len(_MANUAL_MENU)}B", end="")
            return ""


def _manual_input(prompt: str) -> str:
    if not sys.stdin.isatty():
        return input(prompt)
    try:
        import msvcrt
    except ImportError:
        return input(prompt)

    buf: list[str] = []
    print(prompt, end="", flush=True)
    while True:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            key = msvcrt.getwch()
            if not buf and key in ("H", "P"):
                line = _arrow_menu()
                print(prompt + line)
                return line or ""
            continue
        if ch == "\r":
            print()
            return "".join(buf)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\b":
            if buf:
                buf.pop()
                print("\b \b", end="", flush=True)
            continue
        if ch >= " ":
            buf.append(ch)
            print(ch, end="", flush=True)


def manual_repl(args=None) -> None:
    """
    No-AI fallback REPL. Dispatches typed commands directly to TOOL_FUNCS.
    Default mode. Can summon the AI agent explicitly with `agent` / `symbot`.
    """
    print("~~ Symbiote MANUAL MODE — direct tool dispatch, no AI ~~")
    print("Commands:")
    print("  fetch <url|id> [no-video]   download chat (+ video unless 'no-video')")
    print("  list                         show archived VODs")
    print("  peaks <id> [top=15]          raw peak density")
    print("  score <id> [top=15]          Siphon virality scoring")
    print("  export <id> [top=5] [--subtitles] cut top clips to Desktop/VODClips")
    print("  window <id> <start> [dur=60] analyze a timestamp window")
    print("  search <id> <pattern>        regex search chat")
    print("  heatmap <id>                 open heatmap PNG")
    print("  agent                         summon Symbot")
    print("  help   quit                   press ↑/↓ for menu")
    print()

    while True:
        try:
            line = _manual_input("manual> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            return
        if not line:
            continue

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd in ("exit", "quit"):
            print("bye.")
            return
        elif cmd == "help":
            print("  fetch <url|id> [no-video]  |  list  |  peaks <id> [top]")
            print("  score <id> [top]           |  export <id> [top] [--subtitles]")
            print("  export subtitle flags: --subtitle-model tiny|base|small, --subtitle-font-size 28, --subtitle-sidecar-only")
            print("  window <id> <start> [dur]  |  search <id> <pattern>")
            print("  heatmap <id>")
            print("  agent                      |  quit")
        elif cmd in ("agent", "symbot", "summon"):
            _agent_repl(args)
            return
        elif cmd == "fetch":
            argv = rest.split()
            if not argv:
                print("  usage: fetch <url|vod_id> [no-video]")
                continue
            kwargs: dict = {"vod": argv[0]}
            if "no-video" in argv[1:]:
                kwargs["download_video"] = False
            _print_tool_result(run_tool("fetch_vod", kwargs))
        elif cmd == "list":
            _print_tool_result(run_tool("list_vods", {}))
        elif cmd == "peaks":
            argv = rest.split()
            if not argv:
                print("  usage: peaks <vod_id> [top]")
                continue
            kwargs = {"vod_id": argv[0]}
            if len(argv) > 1 and argv[1].isdigit():
                kwargs["top"] = int(argv[1])
            _print_tool_result(run_tool("get_peaks", kwargs))
        elif cmd in ("score", "virality"):
            argv = rest.split()
            if not argv:
                print("  usage: score <vod_id> [top]")
                continue
            kwargs = {"vod_id": argv[0]}
            if len(argv) > 1 and argv[1].isdigit():
                kwargs["top"] = int(argv[1])
            _print_tool_result(run_tool("get_virality", kwargs))
        elif cmd == "export":
            if not rest.strip():
                print("  usage: export <vod_id> [top] [--subtitles] [--subtitle-model tiny] [--subtitle-font-size 28]")
                continue
            kwargs = _parse_manual_export_args(rest)
            if kwargs is None:
                print("  usage: export <vod_id> [top] [--subtitles] [--subtitle-model tiny] [--subtitle-font-size 28]")
                continue
            _print_tool_result(run_tool("export_top_clips", kwargs))
        elif cmd == "window":
            argv = rest.split()
            if len(argv) < 2:
                print("  usage: window <vod_id> <start> [duration_seconds]")
                continue
            kwargs = {"vod_id": argv[0], "start": argv[1]}
            if len(argv) > 2 and argv[2].isdigit():
                kwargs["duration"] = int(argv[2])
            _print_tool_result(run_tool("analyze_window", kwargs))
        elif cmd == "search":
            argv = rest.split(None, 1)
            if len(argv) < 2:
                print("  usage: search <vod_id> <pattern>")
                continue
            _print_tool_result(run_tool("search_chat", {"vod_id": argv[0], "pattern": argv[1]}))
        elif cmd == "heatmap":
            argv = rest.split()
            if not argv:
                print("  usage: heatmap <vod_id>")
                continue
            _print_tool_result(run_tool("open_heatmap", {"vod_id": argv[0]}))
        else:
            print(f"  Unknown command {cmd!r}. Type 'help'.")


def _agent_repl(args) -> None:
    try:
        router = LLMRouter(args)
    except ProviderConfigError as e:
        print(f"{e} — staying in manual mode.")
        print("(Set GEMINI_API_KEY and/or GROQ_API_KEY in .env or your shell)")
        print()
        manual_repl(args)
        return

    messages = []
    spinner = WormSpinner()

    fallback = f", fallback={router.fallback_provider}:{router.fallback_model}" if router.fallback_provider != "none" else ", fallback=none"
    print(f"~~ Symbot summoned. primary={router.primary_provider}:{router.primary_model}{fallback} ~~")
    print("drop a VOD link, ask about one I already have, or 'exit' to leave.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            return
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "/exit", "/quit"):
            print("bye.")
            return

        messages.append({"role": "user", "content": user_input})
        print("symbot> ", end="", flush=True)
        try:
            chat_turn(router, messages,
                      on_token=lambda t: print(t, end="", flush=True),
                      spinner=spinner)
        except ProviderQuotaError as e:
            spinner.stop()
            print(f"\n[quota/rate-limit: {e}]")
        except Exception as e:
            print(f"\n[API error: {type(e).__name__}: {e}]")
        except KeyboardInterrupt:
            print("\n[interrupted]")
        print()


def main():
    ap = argparse.ArgumentParser(description="Symbiote — Twitch VOD chat agent (Parasite toolkit)")
    ap.add_argument("--pro",    action="store_true",
                    help="Use Gemini 2.5 Pro (stronger; smaller free quota)")
    ap.add_argument("--provider", choices=("gemini", "groq", "auto"),
                    default=os.environ.get("SYMBIOTE_PRIMARY_PROVIDER", "gemini"),
                    help="LLM provider for Symbot (default: gemini)")
    ap.add_argument("--fallback-provider", choices=("groq", "none"),
                    default=os.environ.get("SYMBIOTE_FALLBACK_PROVIDER", "groq"),
                    help="Fallback provider after Gemini quota/rate-limit (default: groq)")
    ap.add_argument("--model",  help="Primary model name or preset")
    ap.add_argument("--fallback-model",
                    default=os.environ.get("SYMBIOTE_GROQ_MODEL", "llama-3.3-70b-versatile"),
                    help="Fallback model name or preset")
    ap.add_argument("--no-fallback", action="store_true",
                    help="Disable automatic provider fallback")
    ap.add_argument("--manual", action="store_true",
                    help="Skip AI — use direct tool commands only (manual mode)")
    ap.add_argument("--agent", action="store_true",
                    help="Start Symbot immediately instead of manual mode")
    args = ap.parse_args()

    if args.agent and not args.manual:
        _agent_repl(args)
        return

    if not args.manual and sys.stdin.isatty():
        try:
            ans = input("Summon agent Symbot? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans in ("y", "yes"):
            print()
            _agent_repl(args)
            return
        print()

    manual_repl(args)


if __name__ == "__main__":
    main()
