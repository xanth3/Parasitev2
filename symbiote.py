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
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from google import genai
from google.genai import types

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


GQL_HEADERS = {"Client-ID": CLIENT_ID, "Content-Type": "application/json"}
ARCHIVE_DIR = Path("archive")


def _safe_slug(s, maxlen=40):
    s = (s or "unknown").lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s).strip("_")
    return s[:maxlen] or "unknown"


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


def tool_fetch_vod(vod, max_seconds=23766):
    """Download full chat via Twitch GraphQL and archive into a dated folder."""
    vod_id = extract_vod_id(vod)
    existing = vod_archive_dir(vod_id)
    if existing and (existing / "chat.json").exists():
        data = json.loads((existing / "chat.json").read_text(encoding="utf-8"))
        meta = {}
        mpath = existing / "meta.json"
        if mpath.exists():
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        return {
            "status": "cached",
            "vod_id": vod_id,
            "archive_dir": str(existing),
            "count": len(data.get("comments", [])),
            "duration_seconds": data.get("video", {}).get("length", 0),
            **{k: meta[k] for k in ("upload_date", "streamer", "title", "fetched_at") if k in meta},
        }

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

    return {
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
        return {"error": str(e), "path": str(png)}


SYSTEM_PROMPT = """You are Symbiote — a small, chat-obsessed agent living inside the Parasite toolkit. The Parasite is the machinery that latches onto Twitch VODs and siphons chat; you're its friendly half — the one the user actually talks to.

You have tools to fetch chat, find peaks, analyze specific timestamps, and search for patterns. USE THEM. Never fabricate timestamps, message counts, or chat content — if you don't have the data, call a tool.

Style:
- Terse and playful. You're a weird little symbiont that lives in chat logs, not a corporate assistant.
- Short answers. Bullet points when listing.
- When describing a peak, QUOTE the actual chat signal (e.g. "Cinema ×63, SAME SHIRT, ITS US BOIS"). Don't summarize abstractly — chat-speak is the product.
- No preamble ("Great question!", "I'll help you with that"). Get to the point.

Workflow:
- User drops a VOD URL → fetch_vod first. Tell them the fetch takes ~3-4 min for a 6h VOD before you call it.
- "top peaks" / "the big moments" → get_peaks.
- "what's going on at X" → analyze_window. Accepts '2:57', '2:57:00', or raw seconds.
- "when did people say Y" / "heatmap of Z" → search_chat with a regex.
- "show me the heatmap" → open_heatmap.
- Unknown VOD? Call list_vods to see what's cached, then ask the user which one.

When the user asks what was happening at a peak, give them a clip-ready answer: what was on screen (inferred from chat), the cold-open line, and a cut window like "2:56:30 → 2:57:45"."""


TOOLS = [
    {
        "name": "fetch_vod",
        "description": "Download the full chat history for a Twitch VOD. Takes 3-4 minutes for a 6-hour stream. Caches to chat_<id>.json — subsequent calls for the same VOD return instantly. Call this first when the user drops a new URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "vod": {"type": "string", "description": "VOD URL or numeric ID"},
                "max_seconds": {"type": "integer", "description": "Cap on stream duration (default 23766, ~6.5h)", "default": 23766},
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
    "fetch_vod": tool_fetch_vod,
    "list_vods": tool_list_vods,
    "get_peaks": tool_get_peaks,
    "analyze_window": tool_analyze_window,
    "search_chat": tool_search_chat,
    "open_heatmap": tool_open_heatmap,
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


def _gemini_tools():
    """Wrap our TOOLS list in a Gemini Tool object."""
    return [types.Tool(function_declarations=TOOLS)]


def _to_jsonable(v):
    """Normalize google proto MapComposite / ListComposite to plain Python."""
    if hasattr(v, "items"):
        return {k: _to_jsonable(vv) for k, vv in v.items()}
    if isinstance(v, (list, tuple)) or (hasattr(v, "__iter__") and not isinstance(v, (str, bytes))):
        try:
            return [_to_jsonable(x) for x in v]
        except TypeError:
            pass
    return v


def _retry_delay_from(err):
    """Parse Gemini 429's retryDelay field ('15s') -> seconds. Fallback 30s."""
    msg = str(err)
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", msg)
    if m:
        return min(60.0, float(m.group(1)) + 1.0)
    return 30.0


def _open_stream(client, model, contents, config, on_token):
    """generate_content_stream with polite retry on 429 rate-limit."""
    for attempt in range(3):
        try:
            return client.models.generate_content_stream(
                model=model, contents=contents, config=config,
            )
        except Exception as e:
            s = str(e)
            if "429" in s or "RESOURCE_EXHAUSTED" in s:
                delay = _retry_delay_from(e)
                on_token(f"\n  [rate-limited — waiting {delay:.0f}s then retrying]\n")
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("Exhausted retries on rate-limit")


def chat_turn(client, model, contents, on_token, spinner=None):
    """Stream one turn against Gemini, execute any function calls, loop until done.

    If `spinner` is provided, it's started while waiting for the first token of
    each stream and while a tool is executing, and stopped as soon as output
    begins flowing.
    """
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=_gemini_tools(),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    def _spin_start(label):
        if spinner is not None:
            spinner.start(label)

    def _spin_stop():
        if spinner is not None:
            spinner.stop()

    while True:
        text_agg = ""
        fn_calls = []
        _spin_start("thinking…")
        stream = _open_stream(client, model, contents, config, on_token)
        first_token = True
        for chunk in stream:
            cand_list = getattr(chunk, "candidates", None) or []
            for cand in cand_list:
                parts = getattr(getattr(cand, "content", None), "parts", None) or []
                for part in parts:
                    txt = getattr(part, "text", None)
                    if txt:
                        if first_token:
                            _spin_stop()
                            first_token = False
                        on_token(txt)
                        text_agg += txt
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", None):
                        fn_calls.append(fc)
        _spin_stop()

        # Model's turn goes into history.
        model_parts = []
        if text_agg:
            model_parts.append(types.Part(text=text_agg))
        for fc in fn_calls:
            model_parts.append(types.Part(function_call=fc))
        if model_parts:
            contents.append(types.Content(role="model", parts=model_parts))

        if not fn_calls:
            return

        # Execute tools, feed results back as a "user"-role turn of function_response parts.
        result_parts = []
        for fc in fn_calls:
            args = _to_jsonable(getattr(fc, "args", {}) or {})
            preview = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
            on_token(f"\n  · {fc.name}({preview})")
            _spin_start(f"{fc.name}…")
            result = run_tool(fc.name, args)
            _spin_stop()
            result_parts.append(types.Part.from_function_response(
                name=fc.name,
                response={"result": result},
            ))
        on_token("\n")
        contents.append(types.Content(role="user", parts=result_parts))


def main():
    ap = argparse.ArgumentParser(description="Symbiote — Twitch VOD chat agent (Parasite toolkit)")
    ap.add_argument("--pro", action="store_true",
                    help="Use Gemini 2.5 Pro (stronger; smaller free quota)")
    ap.add_argument("--model", help="Override Gemini model name")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("Symbiote needs GEMINI_API_KEY (or GOOGLE_API_KEY) in your environment.")
        print("Get a free key at https://aistudio.google.com/app/apikey, then:")
        print("  PowerShell:  setx GEMINI_API_KEY \"AIza...\"  (close+reopen terminal)")
        print("  bash:        export GEMINI_API_KEY='AIza...'")
        sys.exit(1)

    model = args.model or ("gemini-2.5-pro" if args.pro else "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)
    contents = []
    spinner = WormSpinner()

    print(f"~~ Symbiote bonded. model={model} ~~")
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

        contents.append(types.Content(role="user",
                                       parts=[types.Part(text=user_input)]))
        print("symbiote> ", end="", flush=True)
        try:
            chat_turn(client, model, contents,
                      on_token=lambda t: print(t, end="", flush=True),
                      spinner=spinner)
        except Exception as e:
            print(f"\n[API error: {type(e).__name__}: {e}]")
        except KeyboardInterrupt:
            print("\n[interrupted]")
        print()


if __name__ == "__main__":
    main()
