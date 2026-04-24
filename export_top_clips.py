"""Export top Siphon clips from a VOD video.

Reads viral_score.csv, analyzes the nearby chat signal, cuts short clips with
ffmpeg, and writes upload copy plus JSON metadata sidecars.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from heatmap import extract_body, extract_offset, hms, load_comments


HYPE = {
    "W", "WW", "WWW", "POG", "POGU", "POGGERS", "LETSGO", "LETSGOO",
    "INSANE", "CLEAN", "GOAT", "PEAK", "CINEMA", "AURA", "COOKING",
}

FUNNY = {
    "LUL", "LULW", "OMEGALUL", "KEKW", "KEK", "LOL", "LMAO", "LMFAO",
    "XD", "ICANT", "DEAD", "BALD", "BALDY", "BALDIMORT",
}

SHOCK = {
    "WTF", "WTH", "OMG", "OMFG", "HUH", "WHAT", "NO WAY", "NOWAY",
    "NOSHOT", "NAH", "NAHH", "WAIT", "BRO", "BRUH", "???",
}

CLIP_INTENT = {
    "CLIP", "CLIP IT", "CLIP THAT", "CLIPPED", "SOMEONE CLIP",
    "THIS IS A CLIP", "THATS A CLIP",
}

COMMUNITY_MEMES = {
    "BALD", "BALDY", "BALDIMORT", "CINEMA", "AURA", "COOKED",
    "COOKING", "FARMING", "LOOKING", "DR PEPPER",
}

NOISE = {
    "ASSEMBLE", "SCATTER", "BRB", "BATHROOM", "PISS", "PEE", "AFK",
    "YO", "YOO", "YOOOO", "HI", "HELLO", "LIVE", "WE BACK",
    "!DROPS", "!PRIME", "!DISCORD", "SUBSCRIBED", "FOLLOWED",
    "HYPE TRAIN", "RAID",
}

BAD_COPY_TERMS = {"CHAT", "ASMONGOLD"}
BREAK_MARKERS = {"ASSEMBLE", "SCATTER"}
BAD_TITLE_PHRASES = {
    "YO", "YOO", "YOOOO", "HELLO", "ASSEMBLE", "SCATTER", "BRB", "HI",
    "HE", "SHE", "HIM", "HER", "HIS", "THEY", "THEM", "YOU", "I", "ME",
}
SAFE_TAGS = "shorts,twitch,streamer,funny,hype,viral"
DEFAULT_OUT_DIR = Path.home() / "Desktop" / "VODClips"
MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v"}
MICRO_SCAN_SECONDS = 60
MICRO_WINDOW_SECONDS = 5


@dataclass(frozen=True)
class OutputLayout:
    root: Path
    videos: Path
    json: Path
    thumbnails: Path
    copy: Path
    subtitles: Path
STOPWORDS = {
    "A", "AN", "AND", "ARE", "AS", "AT", "BE", "BUT", "BY", "CAN", "DO",
    "FOR", "FROM", "GET", "GO", "GOT", "HAD", "HAS", "HAVE", "HE", "HER",
    "HIM", "HIS", "HOW", "I", "IF", "IN", "IS", "IT", "ITS", "JUST",
    "LIKE", "ME", "MY", "NO", "NOT", "OF", "ON", "OR", "OUT", "SO",
    "THAT", "THE", "THEIR", "THEM", "THEN", "THERE", "THEY", "THIS",
    "TO", "TOO", "UP", "WAS", "WE", "WHAT", "WHEN", "WHERE", "WHO",
    "WHY", "WITH", "YOU", "YOUR", "SHE", "SHES", "HES", "IM", "IVE",
}


def parse_time(value: str) -> int:
    value = str(value or "").strip()
    if not value:
        raise ValueError("empty timestamp")
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return int(float(value))
    parts = value.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"bad timestamp {value!r}")
    nums = [int(float(p)) for p in parts]
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    else:
        h, m, s = 0, 0, nums[0]
    return h * 3600 + m * 60 + s


def row_peak_seconds(row: dict) -> int:
    for key in ("seconds", "peak_seconds"):
        if row.get(key) not in (None, ""):
            return int(float(row[key]))
    if row.get("timestamp"):
        return parse_time(row["timestamp"])
    raise ValueError("missing seconds/peak_seconds/timestamp")


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_score_rows(path: Path, from_top: int, min_score: float) -> tuple[list[dict], list[str]]:
    warnings = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if rows and "virality" in rows[0]:
        rows.sort(key=lambda r: _num(r.get("virality"), -1), reverse=True)
    else:
        rows.sort(key=lambda r: int(_num(r.get("rank"), 999999)))

    valid = []
    for row in rows:
        if _num(row.get("virality"), 0) < min_score:
            continue
        try:
            peak = row_peak_seconds(row)
        except ValueError as e:
            warnings.append(f"skipping row {row.get('rank') or '?'}: {e}")
            continue
        row = dict(row)
        row["_peak_seconds"] = peak
        valid.append(row)
    return valid[:from_top], warnings


def _comment_user(comment: dict, fallback: int) -> str:
    commenter = comment.get("commenter")
    if isinstance(commenter, dict):
        for key in ("display_name", "name", "login", "_id", "id"):
            if commenter.get(key):
                return str(commenter[key])
    return f"anon-{fallback}"


def normalize_body(body: str) -> str:
    body = re.sub(r"https?://\S+", " ", body or "", flags=re.I)
    body = re.sub(r"@\w+", " ", body)
    body = body.upper()
    body = re.sub(r"[^A-Z0-9?!+ ]+", " ", body)
    body = re.sub(r"(.)\1{2,}", r"\1\1", body)
    fixes = {
        "YOO": "YOO",
        "WAYY": "WAY",
        "LMAOO": "LMAO",
        "LMFAOO": "LMFAO",
        "NOO": "NO",
        "NAHH": "NAH",
    }
    toks = [fixes.get(t, t) for t in body.split()]
    return " ".join(toks)


def _phrase_noise(phrase: str) -> bool:
    toks = phrase.split()
    if phrase in NOISE or phrase in BAD_TITLE_PHRASES or len(phrase) < 2:
        return True
    if all(tok in STOPWORDS for tok in toks):
        return True
    if len(toks) == 1 and toks[0] in STOPWORDS:
        return True
    return False


def _extract_phrases(norm: str) -> list[str]:
    toks = norm.split()
    phrases = []
    for n in (1, 2, 3):
        for i in range(0, max(0, len(toks) - n + 1)):
            phrase = " ".join(toks[i:i + n])
            if not _phrase_noise(phrase):
                phrases.append(phrase)
    return phrases


def _contains_any(norm: str, lexicon: set[str]) -> int:
    hits = 0
    padded = f" {norm} "
    for item in lexicon:
        if " " in item:
            if item in norm:
                hits += 1
        elif re.search(rf"(?<![A-Z0-9+]){re.escape(item)}(?![A-Z0-9+])", padded):
            hits += 1
    return hits


def analyze_window(comments: list[dict], start: int, end: int) -> dict:
    phrase_users: dict[str, set[str]] = defaultdict(set)
    token_users: dict[str, set[str]] = defaultdict(set)
    mood_counts = Counter()
    clip_intent_hits = 0
    community_counts = Counter()
    noise_hits = 0
    break_marker = ""
    sample_messages = []
    users = set()

    for idx, comment in enumerate(comments):
        off = extract_offset(comment)
        if off is None or not (start <= off <= end):
            continue
        body = extract_body(comment)
        norm = normalize_body(body)
        user = _comment_user(comment, idx)
        users.add(user)
        if len(sample_messages) < 12 and body.strip():
            sample_messages.append({"offset": hms(int(off)), "user": user, "body": body[:180]})

        for phrase in set(_extract_phrases(norm)):
            phrase_users[phrase].add(user)
        for tok in set(norm.split()):
            if not _phrase_noise(tok):
                token_users[tok].add(user)

        for name, lexicon in (("HYPE", HYPE), ("FUNNY", FUNNY), ("SHOCK", SHOCK)):
            mood_counts[name] += _contains_any(norm, lexicon)
        clip_intent_hits += _contains_any(norm, CLIP_INTENT)
        noise_hits += _contains_any(norm, NOISE)
        for marker in BREAK_MARKERS:
            if _contains_any(norm, {marker}):
                break_marker = marker
        for meme in COMMUNITY_MEMES:
            if _contains_any(norm, {meme}):
                community_counts[meme] += 1

    def phrase_score(item):
        phrase, users_for_phrase = item
        count = len(users_for_phrase)
        lex_bonus = 3 if phrase in (SHOCK | FUNNY | HYPE | COMMUNITY_MEMES) else 0
        len_bonus = 2 if 1 < len(phrase.split()) <= 3 else 0
        return (count * 10 + lex_bonus + len_bonus, count, len(phrase.split()))

    phrase_counts = [(p, len(u)) for p, u in phrase_users.items()]
    phrase_counts.sort(key=lambda x: phrase_score((x[0], phrase_users[x[0]])), reverse=True)
    top_phrase, top_phrase_count = (phrase_counts[0] if phrase_counts else ("", 0))
    if top_phrase in BAD_TITLE_PHRASES:
        top_phrase, top_phrase_count = "", 0

    token_counts = [(t, len(u)) for t, u in token_users.items()]
    token_counts.sort(key=lambda x: x[1], reverse=True)
    top_token = token_counts[0][0] if token_counts else ""
    dominant_mood = mood_counts.most_common(1)[0][0] if mood_counts and mood_counts.most_common(1)[0][1] else "MIXED"
    community_meme = community_counts.most_common(1)[0][0] if community_counts else ""
    noise_reason = "break/bathroom signal" if break_marker else ""

    return {
        "top_phrase": top_phrase,
        "top_phrase_count": top_phrase_count,
        "top_token": top_token,
        "dominant_mood": dominant_mood,
        "clip_intent_hits": clip_intent_hits,
        "community_meme": community_meme,
        "noise_hits": noise_hits,
        "noise_reason": noise_reason,
        "break_marker": break_marker,
        "unique_chatters": len(users),
        "sample_messages": sample_messages,
        "_mood_counts": dict(mood_counts),
    }


def _window_comments(comments: list[dict], start: int, end: int) -> list[dict]:
    hits = []
    for comment in comments:
        off = extract_offset(comment)
        if off is not None and start <= off <= end:
            hits.append(comment)
    return hits


def _density_count(comments: list[dict], start: int, end: int) -> int:
    return len(_window_comments(comments, start, end))


def _strong_signal_score(signals: dict) -> float:
    mood_counts = signals.get("_mood_counts", {}) or {}
    mood_hits = max(mood_counts.values() or [0])
    phrase = signals.get("top_phrase") or ""
    phrase_quality = 0.5 if phrase in BAD_TITLE_PHRASES else 1.0
    score = 0.0
    score += min(30.0, float(signals.get("top_phrase_count", 0) or 0) * 1.8 * phrase_quality)
    score += min(25.0, float(signals.get("unique_chatters", 0) or 0) / 4.0)
    score += min(18.0, float(mood_hits) * 1.4)
    score += min(20.0, float(signals.get("clip_intent_hits", 0) or 0) * 8.0)
    if signals.get("community_meme"):
        score += 8.0
    if signals.get("dominant_mood") in {"SHOCK", "FUNNY", "HYPE"}:
        score += 5.0
    score -= min(25.0, float(signals.get("noise_hits", 0) or 0) * 3.0)
    if signals.get("break_marker"):
        score -= 100.0
    return score


def refine_peak_seconds(row: dict, comments: list[dict]) -> tuple[int, dict]:
    """
    Heatmap peaks are minute buckets. Scan inside the hot minute and center on
    the strongest short burst so exports start before the actual beat, not just
    before the bucket boundary.
    """
    bucket_start = int(row["_peak_seconds"])
    best = {
        "refined_peak_seconds": bucket_start,
        "source_peak_seconds": bucket_start,
        "micro_window_start": bucket_start,
        "micro_window_end": bucket_start + MICRO_WINDOW_SECONDS,
        "micro_density": 0,
        "micro_score": -1_000_000.0,
        "micro_signals": {},
    }
    scan_end = bucket_start + MICRO_SCAN_SECONDS
    last_center = max(bucket_start, scan_end - MICRO_WINDOW_SECONDS)
    for center in range(bucket_start, last_center + 1):
        start = max(0, center - MICRO_WINDOW_SECONDS // 2)
        end = start + MICRO_WINDOW_SECONDS
        signals = analyze_window(comments, start, end)
        if signals.get("break_marker"):
            continue
        density = _density_count(comments, start, end)
        score = density * 1.5 + _strong_signal_score(signals)
        if signals.get("noise_hits", 0) and score < 35:
            score -= 15
        if score > best["micro_score"]:
            best = {
                "refined_peak_seconds": int((start + end) / 2),
                "source_peak_seconds": bucket_start,
                "micro_window_start": start,
                "micro_window_end": end,
                "micro_density": density,
                "micro_score": round(score, 3),
                "micro_signals": {k: v for k, v in signals.items() if not k.startswith("_")},
            }
    return int(best["refined_peak_seconds"]), best


def editor_quality_score(row: dict, signals: dict, micro: dict, density: int) -> float:
    base = _row_float(row, "virality")
    score = base * 1.2
    score += min(35.0, density / 2.0)
    score += _strong_signal_score(signals)
    score += min(25.0, float(micro.get("micro_density", 0) or 0) * 1.2)
    score += min(20.0, float(micro.get("micro_score", 0) or 0) / 5.0)
    if str(row.get("echo_label", "")).lower() == "story":
        score += 8.0
    if str(row.get("mood", "")).upper() in {"SHOCK", "FUNNY", "HYPE"}:
        score += 6.0
    if signals.get("break_marker"):
        score -= 250.0
    if signals.get("noise_hits", 0) and not (
        signals.get("clip_intent_hits", 0) >= 2
        or signals.get("top_phrase_count", 0) >= 8
        or max(signals.get("_mood_counts", {}).values() or [0]) >= 6
    ):
        score -= 35.0
    return round(score, 3)


def _title_case(s: str) -> str:
    words = s.split()
    out = []
    for i, word in enumerate(words):
        core = word.strip('"')
        titled = core if core.isupper() and len(core) <= 6 else core.capitalize()
        if word.startswith('"') and word.endswith('"'):
            titled = f'"{titled}"'
        out.append(titled)
    return " ".join(out)


def _usable_copy_phrase(phrase: str, count: int) -> bool:
    if not phrase or count < 4 or phrase in BAD_TITLE_PHRASES:
        return False
    if phrase in (SHOCK | FUNNY | HYPE | COMMUNITY_MEMES):
        return True
    toks = phrase.split()
    return 1 < len(toks) <= 3 and not all(tok in STOPWORDS for tok in toks)


def _sanitize_copy(text: str, forbidden_terms: set[str]) -> str:
    out = text
    for term in forbidden_terms | BAD_COPY_TERMS:
        if term:
            out = re.sub(re.escape(term), "", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def generate_title(signals: dict, row: dict, used_titles: set[str], forbidden_terms: set[str] | None = None) -> str:
    forbidden_terms = forbidden_terms or set()
    phrase = signals.get("top_phrase") or ""
    count = int(signals.get("top_phrase_count") or 0)
    mood = signals.get("dominant_mood")

    if signals.get("clip_intent_hits", 0) >= 2:
        title = "This Had To Be Clipped"
    elif _usable_copy_phrase(phrase, count):
        if phrase in SHOCK:
            title = f'This Turns Into A "{phrase}" Moment'
        elif phrase in COMMUNITY_MEMES:
            title = f'This Somehow Becomes "{phrase}"'
        else:
            title = f'This Turns Into A "{phrase}" Moment'
    elif mood == "SHOCK":
        title = "He Was Not Ready For This"
    elif mood == "FUNNY":
        title = "This Instantly Turns Into A Mess"
    elif mood == "HYPE":
        title = "This Moment Goes Crazy"
    else:
        title = "Things Get Out Of Control Fast"

    title = _title_case(_sanitize_copy(title, forbidden_terms))[:70].rstrip()
    base = title
    n = 2
    while title.lower() in used_titles:
        suffix = f" {n}"
        title = (base[:70 - len(suffix)] + suffix).rstrip()
        n += 1
    used_titles.add(title.lower())
    return title


def generate_description(signals: dict, forbidden_terms: set[str] | None = None) -> str:
    forbidden_terms = forbidden_terms or set()
    phrase = signals.get("top_phrase") or ""
    count = int(signals.get("top_phrase_count") or 0)
    meme = signals.get("community_meme") or ""
    mood = signals.get("dominant_mood")
    if signals.get("clip_intent_hits", 0) >= 2:
        desc = "This was bound to happen."
    elif _usable_copy_phrase(phrase, count):
        desc = f'It immediately turns into a "{phrase}" moment.'
    elif meme:
        desc = f'This somehow becomes a "{meme}" situation.'
    elif mood == "SHOCK":
        desc = "He did not expect this at all."
    elif mood == "FUNNY":
        desc = "This instantly spirals."
    elif mood == "HYPE":
        desc = "Everything escalates right here."
    else:
        desc = "Things go off the rails fast."
    return _sanitize_copy(desc, forbidden_terms)


def filename_slug(title: str) -> str:
    title = title.replace('"', "").replace("'", "")
    title = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
    return title or "clip"


def suffix_slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_")
    return value


def build_output_layout(out_dir: Path, flat: bool = False) -> OutputLayout:
    out_dir = Path(out_dir).expanduser()
    if flat:
        return OutputLayout(out_dir, out_dir, out_dir, out_dir, out_dir, out_dir)
    return OutputLayout(
        root=out_dir,
        videos=out_dir / "videos",
        json=out_dir / "json",
        thumbnails=out_dir / "thumbnails",
        copy=out_dir / "copy",
        subtitles=out_dir / "subtitles",
    )


def ensure_output_layout(layout: OutputLayout, thumbnail: bool = False, subtitles: bool = False) -> None:
    for path in {layout.root, layout.videos, layout.json, layout.copy}:
        path.mkdir(parents=True, exist_ok=True)
    if thumbnail:
        layout.thumbnails.mkdir(parents=True, exist_ok=True)
    if subtitles:
        layout.subtitles.mkdir(parents=True, exist_ok=True)


def clip_file_paths(layout: OutputLayout, base: str) -> dict[str, Path]:
    return {
        "video": layout.videos / f"{base}.mp4",
        "metadata": layout.json / f"{base}.json",
        "thumbnail": layout.thumbnails / f"{base}.jpg",
        "subtitles": layout.subtitles / f"{base}.srt",
    }


def _row_int(row: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(row.get(key, default)))
    except (TypeError, ValueError):
        return default


def _row_float(row: dict, key: str, default: float = 0.0) -> float:
    return _num(row.get(key), default)


def is_url(value: str | Path) -> bool:
    return str(value).lower().startswith(("http://", "https://"))


def infer_vod_id(*values) -> str:
    for value in values:
        s = str(value or "")
        m = re.search(r"(?:videos/|_v|^v?)(\d{6,})", s)
        if m:
            return m.group(1)
    return ""


def twitch_vod_url(vod_id: str) -> str:
    return f"https://www.twitch.tv/videos/{vod_id}"


def resolve_stream_url(url: str) -> str:
    """Resolve a Twitch page URL into a direct media URL ffmpeg can read."""
    commands = [
        ["yt-dlp", "-g", url],
        [sys.executable, "-m", "yt_dlp", "-g", url],
    ]
    output = ""
    for cmd in commands:
        try:
            output = subprocess.check_output(
                cmd,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            break
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as e:
            output = str(e.output or "")
            continue
    else:
        raise RuntimeError("yt-dlp is required to process Twitch URLs when no local VOD file exists")
    if not output:
        raise RuntimeError(f"yt-dlp did not return output for {url}")
    lines = [line.strip() for line in output.splitlines() if line.strip().startswith(("http://", "https://"))]
    if not lines:
        raise RuntimeError(f"yt-dlp did not return a playable media URL for {url}")
    return lines[0]


def resolve_video_source(video_arg: str | Path, scores_path: Path) -> tuple[str, bool]:
    """Return (ffmpeg input, is_local_file). Falls back to Twitch VOD URL."""
    raw = str(video_arg or "").strip()
    if raw and is_url(raw):
        return raw, False
    if raw:
        p = Path(raw).expanduser()
        if p.exists():
            return str(p), True
    vod_id = infer_vod_id(raw, scores_path, scores_path.parent)
    if not vod_id:
        raise FileNotFoundError(f"video not found and could not infer VOD ID: {raw}")
    return twitch_vod_url(vod_id), False


def _forbidden_terms(scores_path: Path, chat_path: Path, video_path: str | Path) -> set[str]:
    terms = set(BAD_COPY_TERMS)
    for p in (scores_path, chat_path):
        for parent in [p.parent, p.parent.parent]:
            meta = parent / "meta.json"
            info = parent / "info.json"
            for candidate in (meta, info):
                if candidate.exists():
                    try:
                        data = json.loads(candidate.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    for key in ("streamer", "uploader", "uploader_id", "channel", "creator"):
                        if data.get(key):
                            terms.add(str(data[key]).upper())
    m = re.search(r"\d{4}-\d{2}-\d{2}_([^_]+)_v\d+", str(scores_path.parent))
    if m:
        terms.add(m.group(1).upper())
    if not is_url(video_path):
        stem = Path(video_path).stem
        m = re.search(r"(.+?)_\d{8}_v\d+", stem)
        if m:
            terms.add(m.group(1).upper())
    return {t for t in terms if t}


def assert_clean_copy(title: str, description: str, forbidden_terms: set[str]) -> None:
    hay = f"{title} {description}".upper()
    for term in forbidden_terms | BAD_COPY_TERMS:
        if term and term.upper() in hay:
            raise AssertionError(f"forbidden term {term!r} in generated copy")


def get_video_resolution(video_path: str | Path) -> tuple[int, int]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(video_path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    stream = (data.get("streams") or [{}])[0]
    return int(stream["width"]), int(stream["height"])


def get_video_duration(video_path: str | Path) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(video_path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    return float(data.get("format", {}).get("duration", 0.0))


def extract_frame(video_path: str | Path, timestamp_seconds: float, out_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-ss", f"{timestamp_seconds:.3f}", "-i", str(video_path),
        "-vframes", "1", str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:110]


def _ass_color_for_user(user: str) -> str:
    palette = [
        "&H00FFB000", "&H0000D7FF", "&H0065FF65", "&H00FF66FF",
        "&H004D8CFF", "&H00E6E6E6", "&H0000FFFF", "&H00B07CFF",
    ]
    return palette[sum(ord(c) for c in user) % len(palette)]


def _chat_overlay_message_text(message: dict, font_size: int, timestamp_size: int) -> str:
    return (
        rf"{{\fs{timestamp_size}\c&H00CFCFCF&}}{hms(int(message['absolute_second']))} "
        rf"{{\fs{font_size}\c{message['color']}}}{message['user']}: "
        rf"{{\c&H00FFFFFF&}}{message['body']}"
    )


def _chat_overlay_box(args, plan: dict | None = None) -> dict:
    x = int(getattr(args, "chat_overlay_x", 28))
    y = int(getattr(args, "chat_overlay_y", 92))
    width = int(getattr(args, "chat_overlay_width", 540))
    height = int(getattr(args, "chat_overlay_height", 246))
    placement = str(getattr(args, "chat_overlay_placement", "auto")).lower()
    camera = (plan or {}).get("camera_box") or {}
    gap = 24

    if placement == "below-camera" or (
        placement == "auto"
        and camera
        and camera.get("x", 9999) < 720
        and camera.get("y", 9999) < 260
    ):
        y = min(980 - height, int(camera.get("y", 0) + camera.get("h", 360) + gap))
    elif placement == "above-camera" and camera:
        y = max(28, int(camera.get("y", 0) - height - gap))
    elif placement == "auto" and camera and camera.get("x", 9999) < 720:
        camera_top = int(camera.get("y", 1080))
        if y < camera_top:
            height = max(1, min(height, camera_top - y - gap))
            width = max(240, min(width, int(camera.get("x", 0) + camera.get("w", width) - x)))

    return {"x": x, "y": y, "w": width, "h": height, "placement": placement}


def write_chat_overlay_ass(comments: list[dict], start: int, end: int, out_path: Path, args, plan: dict | None = None) -> dict:
    """
    Transparent chatlog-style overlay.

    The overlay is rendered as rolling stack snapshots instead of independent
    message fades. That keeps one active subtitle line per visible row, so
    dense moments scroll naturally without text piling on top of itself.
    """
    duration = max(0.1, end - start)
    box = _chat_overlay_box(args, plan)
    x = box["x"]
    y = box["y"]
    width = box["w"]
    height = box["h"]
    font_size = int(getattr(args, "chat_overlay_font_size", 26))
    line_h = max(font_size + 8, int(getattr(args, "chat_overlay_line_height", font_size + 8)))
    max_fit_lines = max(1, height // line_h)
    max_lines = max(1, min(int(getattr(args, "chat_overlay_lines", 7)), max_fit_lines))
    max_per_second = max(1, int(getattr(args, "chat_overlay_max_per_second", 2)))
    hold = max(2.0, float(getattr(args, "chat_overlay_duration", 6.0)))
    bg_alpha = max(0, min(255, int(getattr(args, "chat_overlay_bg_alpha", 165))))
    clip_right = x + width
    clip_bottom = y + height
    timestamp_size = max(16, font_size - 8)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: ChatLog,Arial,{font_size},&H00FFFFFF,&H000000FF,&HCC000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,3,1,7,0,0,0,1"
        ),
        (
            "Style: ChatPanel,Arial,10,&H00000000,&H000000FF,&H00000000,"
            "&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if bg_alpha < 255:
        panel = (
            rf"{{\pos({x - 8},{y - 8})\p1\c&H000000&\alpha&H{bg_alpha:02X}&}}"
            f"m 0 0 l {width + 16} 0 l {width + 16} {height + 16} l 0 {height + 16}"
        )
        lines.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration)},ChatPanel,,0,0,0,,{panel}")

    feed: list[dict] = []
    per_second: dict[int, int] = defaultdict(int)
    for idx, comment in enumerate(comments):
        off = extract_offset(comment)
        if off is None or off < start or off > end:
            continue
        sec = int(off)
        if per_second[sec] >= max_per_second:
            continue
        body = _ass_escape(extract_body(comment))
        if not body:
            continue
        user = _ass_escape(_comment_user(comment, idx))
        rel = max(0.0, float(off - start))
        rel_end = min(duration, rel + hold)
        if rel_end <= rel:
            continue
        feed.append({
            "rel": rel,
            "end": rel_end,
            "absolute_second": sec,
            "user": user,
            "body": body,
            "color": _ass_color_for_user(user),
        })
        per_second[sec] += 1

    feed.sort(key=lambda item: item["rel"])
    if feed:
        breakpoints = {0.0, duration}
        for message in feed:
            breakpoints.add(round(float(message["rel"]), 3))
            breakpoints.add(round(float(message["end"]), 3))
        ordered_breakpoints = sorted(bp for bp in breakpoints if 0.0 <= bp <= duration)

        for interval_start, interval_end in zip(ordered_breakpoints, ordered_breakpoints[1:]):
            if interval_end - interval_start < 0.01:
                continue
            active = [
                message for message in feed
                if message["rel"] <= interval_start < message["end"]
            ]
            visible = active[-max_lines:]
            for row, message in enumerate(visible):
                row_y = y + row * line_h
                text = (
                    rf"{{\clip({x},{y},{clip_right},{clip_bottom})\pos({x},{row_y})}}"
                    + _chat_overlay_message_text(message, font_size, timestamp_size)
                )
                lines.append(
                    f"Dialogue: 3,{_ass_time(interval_start)},{_ass_time(interval_end)},"
                    f"ChatLog,,0,0,0,,{text}"
                )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return box


def _ffmpeg_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis = 0
    if secs >= 60:
        minutes += 1
        secs = 0
    if minutes >= 60:
        hours += 1
        minutes = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _subtitle_text(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("\n", " ")[:180]


def extract_clip_audio(video: str | Path, start: int, end: int, out_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def transcribe_clip_to_srt(
    video: str | Path,
    start: int,
    end: int,
    srt_path: Path,
    tmp_dir: Path,
    args,
) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "Speech subtitles require faster-whisper. Install dependencies with: pip install -r requirements.txt"
        ) from e

    audio_path = tmp_dir / f"{srt_path.stem}_audio.wav"
    extract_clip_audio(video, start, end, audio_path)
    device = str(getattr(args, "subtitle_device", "cpu"))
    if device == "auto":
        device = "cpu"
    model = WhisperModel(
        str(getattr(args, "subtitle_model", "tiny")),
        device=device,
        compute_type=str(getattr(args, "subtitle_compute_type", "int8")),
    )
    language = getattr(args, "subtitle_language", None) or None
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=bool(getattr(args, "subtitle_vad", True)),
        beam_size=int(getattr(args, "subtitle_beam_size", 1)),
    )

    entries = []
    for segment in segments:
        text = _subtitle_text(segment.text)
        if not text:
            continue
        entries.append({
            "start": max(0.0, float(segment.start)),
            "end": min(float(end - start), max(float(segment.end), float(segment.start) + 0.25)),
            "text": text,
        })

    with srt_path.open("w", encoding="utf-8") as f:
        for idx, entry in enumerate(entries, 1):
            f.write(f"{idx}\n")
            f.write(f"{_srt_time(entry['start'])} --> {_srt_time(entry['end'])}\n")
            f.write(f"{entry['text']}\n\n")

    return {
        "path": str(srt_path),
        "format": "srt",
        "model": str(getattr(args, "subtitle_model", "tiny")),
        "language": getattr(info, "language", language or ""),
        "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        "segment_count": len(entries),
    }


def detect_faces(frame_path: Path) -> list[dict]:
    try:
        import cv2
    except ImportError:
        return []
    image = cv2.imread(str(frame_path))
    if image is None:
        return []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        return []
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
    found = []
    min_side = max(70, int(min(image.shape[:2]) * 0.055))
    for x, y, w, h in faces:
        aspect = float(w) / float(h or 1)
        if w < min_side or h < min_side or not 0.70 <= aspect <= 1.35:
            continue
        found.append({
            "x": int(x), "y": int(y), "w": int(w), "h": int(h),
            "cx": float(x + w / 2), "cy": float(y + h / 2),
            "area": int(w) * int(h),
        })
    found.sort(key=lambda f: f["area"], reverse=True)
    return found


def detect_largest_face(frame_path: Path) -> dict | None:
    faces = detect_faces(frame_path)
    return faces[0] if faces else None


def _even(value: float, minimum: int = 2) -> int:
    n = max(minimum, int(round(value)))
    return n if n % 2 == 0 else n - 1


def compute_punch_crop(width: int, height: int, face_box: dict | None, scale: float) -> dict:
    scale = max(1.01, float(scale))
    if face_box:
        cx, cy = face_box["cx"], face_box["cy"]
        crop_w = min(width, max(width / scale, face_box["w"] * 3.0))
        crop_h = crop_w * height / width
        if crop_h > height:
            crop_h = min(height, max(height / scale, face_box["h"] * 3.0))
            crop_w = crop_h * width / height
    else:
        cx, cy = width / 2, height / 2
        crop_w = width / scale
        crop_h = height / scale

    crop_w = min(width, _even(crop_w))
    crop_h = min(height, _even(crop_h))
    crop_x = _even(max(0, min(width - crop_w, cx - crop_w / 2)), 0)
    crop_y = _even(max(0, min(height - crop_h, cy - crop_h / 2)), 0)
    return {"x": crop_x, "y": crop_y, "w": crop_w, "h": crop_h}


def compute_face_focus_crop(width: int, height: int, face_box: dict | None, coverage: float, fallback_scale: float) -> dict:
    """Crop around a detected face so it occupies a target share of frame height."""
    if not face_box:
        return compute_center_crop(width, height, width / 2, height / 2, fallback_scale)
    coverage = max(0.20, min(0.95, float(coverage)))
    cx, cy = face_box["cx"], face_box["cy"]
    crop_h = max(face_box["h"] / coverage, face_box["h"] * 1.35)
    crop_w = crop_h * width / height
    if crop_w < face_box["w"] * 1.6:
        crop_w = face_box["w"] * 1.6
        crop_h = crop_w * height / width
    if crop_w > width or crop_h > height:
        return compute_punch_crop(width, height, face_box, fallback_scale)
    crop_w = min(width, _even(crop_w))
    crop_h = min(height, _even(crop_h))
    crop_x = _even(max(0, min(width - crop_w, cx - crop_w / 2)), 0)
    crop_y = _even(max(0, min(height - crop_h, cy - crop_h / 2)), 0)
    return {"x": crop_x, "y": crop_y, "w": crop_w, "h": crop_h}


def compute_center_crop(width: int, height: int, cx: float, cy: float, scale: float) -> dict:
    scale = max(1.01, float(scale))
    crop_w = min(width, _even(width / scale))
    crop_h = min(height, _even(crop_w * height / width))
    if crop_h > height:
        crop_h = min(height, _even(height / scale))
        crop_w = min(width, _even(crop_h * width / height))
    crop_x = _even(max(0, min(width - crop_w, cx - crop_w / 2)), 0)
    crop_y = _even(max(0, min(height - crop_h, cy - crop_h / 2)), 0)
    return {"x": crop_x, "y": crop_y, "w": crop_w, "h": crop_h}


def _region_box(width: int, height: int, name: str) -> dict:
    if name == "streamer":
        # Common streamer webcam zone: left side, above lower controls.
        x, y, w, h = 0, 0, width * 0.42, height * 0.56
    elif name == "content":
        # Main content target that avoids the webcam-heavy left strip.
        x, y, w, h = width * 0.30, 0, width * 0.70, height
    else:
        x, y, w, h = 0, 0, width, height
    return {
        "x": int(x), "y": int(y), "w": int(w), "h": int(h),
        "cx": float(x + w / 2), "cy": float(y + h / 2),
    }


def _face_in_region(face: dict, region: dict) -> bool:
    return (
        region["x"] <= face["cx"] <= region["x"] + region["w"]
        and region["y"] <= face["cy"] <= region["y"] + region["h"]
    )


def choose_punch_target(width: int, height: int, faces: list[dict], signals: dict, requested: str) -> dict:
    requested = (requested or "auto").lower()
    if requested not in {"auto", "streamer", "content", "center", "face"}:
        requested = "auto"

    streamer_region = _region_box(width, height, "streamer")
    content_region = _region_box(width, height, "content")
    streamer_faces = [f for f in faces if _face_in_region(f, streamer_region)]
    content_faces = [f for f in faces if _face_in_region(f, content_region)]

    if requested == "face":
        box = faces[0] if faces else None
        return {"resolved": "face" if box else "center", "box": box, "face": box, "region": None, "kind": "face" if box else "center"}
    if requested == "streamer":
        box = streamer_faces[0] if streamer_faces else streamer_region
        return {
            "resolved": "streamer_face" if streamer_faces else "streamer_region",
            "box": box,
            "face": streamer_faces[0] if streamer_faces else None,
            "region": streamer_region,
            "kind": "face" if streamer_faces else "region",
        }
    if requested == "content":
        return {
            "resolved": "content_region",
            "box": content_region,
            "face": content_faces[0] if content_faces else None,
            "region": content_region,
            "kind": "region",
        }
    if requested == "center":
        return {"resolved": "center", "box": None, "face": None, "region": None, "kind": "center"}

    mood = str(signals.get("dominant_mood", "")).upper()
    top_phrase = str(signals.get("top_phrase", "")).upper()
    if mood == "SHOCK" or top_phrase in SHOCK:
        return {
            "resolved": "auto_content_region",
            "box": content_region,
            "face": content_faces[0] if content_faces else None,
            "region": content_region,
            "kind": "region",
        }
    box = streamer_faces[0] if streamer_faces else (faces[0] if faces else None)
    return {
        "resolved": "auto_streamer_face" if streamer_faces else "auto_face" if box else "center",
        "box": box,
        "face": box,
        "region": streamer_region if streamer_faces else None,
        "kind": "face" if box else "center",
    }


def detect_streamer_camera_box(video: str | Path, timestamp: int, tmp_dir: Path) -> dict | None:
    frame_path = tmp_dir / f"parasite_camera_probe_{timestamp}.jpg"
    try:
        extract_frame(video, timestamp, frame_path)
    except Exception:
        return None
    faces = detect_faces(frame_path)
    left_faces = [f for f in faces if f["cx"] < 760]
    if not left_faces:
        return None
    face = left_faces[0]
    x = max(0, int(face["cx"] - 360))
    y = max(0, int(face["cy"] - 260))
    w = min(760, 1920 - x)
    h = min(460, 1080 - y)
    return {"x": x, "y": y, "w": w, "h": h, "face": face}


def should_apply_shock_cam(signals: dict, min_mood: str = "SHOCK", allow_noise: bool = False) -> bool:
    requested = str(min_mood or "SHOCK").upper()
    if requested in {"ANY", "ALL"}:
        mood_ok = bool(str(signals.get("dominant_mood", "")).strip())
    else:
        mood_ok = str(signals.get("dominant_mood", "")).upper() == requested
    return (
        mood_ok
        and (allow_noise or int(signals.get("noise_hits", 0) or 0) == 0)
        and not signals.get("break_marker")
    )


def effects_enabled(args) -> bool:
    return bool(
        args.polish
        or args.intro_zoom
        or args.shock_cam
        or getattr(args, "chat_overlay", False)
        or (getattr(args, "speech_subtitles", False) and not getattr(args, "subtitle_sidecar_only", False))
    )


def build_effects_metadata(args, plan: dict, video_exists: bool) -> dict:
    signals = plan["signals"]
    shock_requested = bool(args.shock_cam and should_apply_shock_cam(
        signals,
        args.shock_cam_min_mood,
        bool(getattr(args, "shock_cam_allow_noise", False)),
    ))
    fade_enabled = bool((args.polish or args.intro_zoom or shock_requested) and not getattr(args, "no_fade", False))
    return {
        "polish": bool(
            args.polish
            or args.intro_zoom
            or shock_requested
            or getattr(args, "chat_overlay", False)
            or (getattr(args, "speech_subtitles", False) and not getattr(args, "subtitle_sidecar_only", False))
        ),
        "fade_out": fade_enabled,
        "fade_duration": float(args.fade_duration),
        "intro_zoom": bool(args.intro_zoom and not shock_requested),
        "intro_zoom_duration": float(args.intro_zoom_duration),
        "intro_start_zoom": float(args.intro_start_zoom),
        "shock_cam": shock_requested,
        "shock_cam_target": str(getattr(args, "shock_cam_target", "auto")),
        "shock_cam_resolved_target": "",
        "shock_cam_duration": float(args.shock_cam_duration),
        "shock_cam_scale": float(args.shock_cam_scale),
        "shock_cam_full_scale": float(getattr(args, "shock_cam_full_scale", 2.15)),
        "shock_cam_full_face_coverage": float(getattr(args, "shock_cam_full_face_coverage", 0.82)),
        "shock_cam_medium_face_coverage": float(getattr(args, "shock_cam_medium_face_coverage", 0.62)),
        "shock_cam_variant": str((plan or {}).get("shock_cam_variant") or ""),
        "shock_cam_reason": "focused around the refined peak moment" if shock_requested else "",
        "face_count": 0,
        "face_detected": False,
        "selected_face": None,
        "target_region": None,
        "crop": None,
        "intro_zoom_skipped_reason": "shock_cam active" if args.intro_zoom and shock_requested else "",
        "video_probe_skipped": not video_exists,
        "chat_overlay": bool(getattr(args, "chat_overlay", False)),
        "chat_overlay_box": (plan or {}).get("chat_overlay_box") or _chat_overlay_box(args, plan),
        "camera_box": (plan or {}).get("camera_box"),
        "chat_overlay_bg_alpha": int(getattr(args, "chat_overlay_bg_alpha", 165)),
        "chat_overlay_path": "",
        "speech_subtitles": bool(getattr(args, "speech_subtitles", False)),
        "speech_subtitle_path": "",
        "speech_subtitle_burned": bool(getattr(args, "speech_subtitles", False) and not getattr(args, "subtitle_sidecar_only", False)),
        "speech_subtitle_style": str(getattr(args, "subtitle_style", "")),
    }


def _zoom_filter(width: int, height: int, duration: float, start_zoom: float) -> str:
    expr = f"if(lt(t\\,{duration:.3f})\\,{start_zoom:.5f}-({start_zoom:.5f}-1)*t/{duration:.3f}\\,1)"
    return (
        f"scale=w='ceil({width}*({expr})/2)*2':"
        f"h='ceil({height}*({expr})/2)*2':eval=frame,"
        f"crop={width}:{height}:(in_w-{width})/2:(in_h-{height})/2,setsar=1"
    )


def build_polish_filter(args, clip_duration: float, width: int, height: int, effects: dict) -> tuple[str, str]:
    fade_dur = max(0.01, min(float(args.fade_duration), clip_duration))
    fade_start = max(0.0, clip_duration - fade_dur)
    audio_filter = f"afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if effects.get("fade_out") else ""

    if effects["shock_cam"]:
        peak_rel = effects["_peak_rel"]
        shock_start = max(0.0, peak_rel - float(args.shock_cam_before_peak))
        shock_end = min(clip_duration, shock_start + float(args.shock_cam_duration))
        if shock_end <= shock_start:
            shock_start, shock_end = 0.0, min(clip_duration, float(args.shock_cam_duration))
        crop = effects["crop"]
        specs = []
        labels = []
        idx = 0
        if shock_start > 0.01:
            specs.append(f"[0:v]trim=start=0:end={shock_start:.3f},setpts=PTS-STARTPTS[v{idx}]")
            labels.append(f"[v{idx}]")
            idx += 1
        specs.append(
            f"[0:v]trim=start={shock_start:.3f}:end={shock_end:.3f},setpts=PTS-STARTPTS,"
            f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']},scale={width}:{height},setsar=1[v{idx}]"
        )
        labels.append(f"[v{idx}]")
        idx += 1
        if shock_end < clip_duration - 0.01:
            specs.append(f"[0:v]trim=start={shock_end:.3f}:end={clip_duration:.3f},setpts=PTS-STARTPTS[v{idx}]")
            labels.append(f"[v{idx}]")
        if len(labels) > 1:
            tail = f",fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if effects.get("fade_out") else ""
            specs.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0{tail}[vout]")
        else:
            tail = f"fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if effects.get("fade_out") else "null"
            specs.append(f"{labels[0]}{tail}[vout]")
        return ";".join(specs), audio_filter

    filters = []
    if effects["intro_zoom"]:
        filters.append(_zoom_filter(width, height, float(args.intro_zoom_duration), float(args.intro_start_zoom)))
    if effects.get("fade_out"):
        filters.append(f"fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}")
    if not filters:
        filters.append("null")
    return f"[0:v]{','.join(filters)}[vout]", audio_filter


def _append_chat_overlay(filtergraph: str, effects: dict) -> str:
    if not effects.get("chat_overlay_path"):
        return filtergraph
    sub = f"subtitles='{_ffmpeg_filter_path(Path(effects['chat_overlay_path']))}'"
    if filtergraph.endswith("[vout]"):
        return filtergraph[:-6] + f",{sub}[vout]"
    return f"[0:v]{sub}[vout]"


def _append_speech_subtitles(filtergraph: str, effects: dict) -> str:
    if not effects.get("speech_subtitle_burned") or not effects.get("speech_subtitle_path"):
        return filtergraph
    style = str(effects.get("speech_subtitle_style") or "")
    sub = f"subtitles='{_ffmpeg_filter_path(Path(effects['speech_subtitle_path']))}'"
    if style:
        escaped_style = style.replace("\\", r"\\").replace("'", r"\'")
        sub += f":force_style='{escaped_style}'"
    if filtergraph.endswith("[vout]"):
        return filtergraph[:-6] + f",{sub}[vout]"
    return f"[0:v]{sub}[vout]"


def prepare_effects(args, plan: dict, tmp_dir: Path | None = None) -> dict:
    video_local = bool(getattr(args, "video_is_local", False))
    video_probe_ok = video_local or (not args.dry_run and is_url(args.video))
    effects = build_effects_metadata(args, plan, video_probe_ok)
    if plan.get("chat_overlay_path"):
        effects["chat_overlay_path"] = str(plan["chat_overlay_path"])
    if plan.get("speech_subtitle_path"):
        effects["speech_subtitle_path"] = str(plan["speech_subtitle_path"])
    if getattr(args, "speech_subtitles", False) and not getattr(args, "subtitle_sidecar_only", False):
        effects["polish"] = True
    if not effects["polish"]:
        return effects
    if not video_probe_ok:
        return effects

    width, height = get_video_resolution(args.video)
    effects["video_width"] = width
    effects["video_height"] = height
    if effects["shock_cam"]:
        frame_path = (tmp_dir or Path(tempfile.gettempdir())) / f"parasite_frame_{plan['peak_seconds']}.jpg"
        extract_frame(args.video, plan["peak_seconds"], frame_path)
        faces = detect_faces(frame_path)
        target = choose_punch_target(
            width,
            height,
            faces,
            plan.get("signals", {}),
            str(getattr(args, "shock_cam_target", "auto")),
        )
        effects["face_count"] = len(faces)
        effects["face_detected"] = target.get("face") is not None
        effects["face_box"] = target.get("face")
        effects["selected_face"] = target.get("face")
        effects["target_region"] = target.get("region")
        effects["shock_cam_resolved_target"] = target["resolved"]
        box = target.get("box")
        variant = str(effects.get("shock_cam_variant") or "full").lower()
        if target.get("kind") == "region" and box:
            scale = (
                float(getattr(args, "shock_cam_full_scale", 2.15))
                if variant == "full"
                else float(args.shock_cam_scale)
            )
            effects["crop"] = compute_center_crop(width, height, box["cx"], box["cy"], scale)
        elif target.get("kind") == "center" or not box:
            scale = (
                float(getattr(args, "shock_cam_full_scale", 2.15))
                if variant == "full"
                else float(args.shock_cam_scale)
            )
            effects["crop"] = compute_center_crop(width, height, width / 2, height / 2, scale)
        else:
            coverage = (
                float(getattr(args, "shock_cam_full_face_coverage", 0.82))
                if variant == "full"
                else float(getattr(args, "shock_cam_medium_face_coverage", 0.62))
            )
            fallback_scale = (
                float(getattr(args, "shock_cam_full_scale", 2.15))
                if variant == "full"
                else float(args.shock_cam_scale)
            )
            effects["crop"] = compute_face_focus_crop(
                width,
                height,
                box,
                coverage,
                fallback_scale,
            )
    effects["_peak_rel"] = float(plan["peak_seconds"] - plan["start_seconds"])
    effects["filter_summary"] = ""
    if effects["polish"]:
        filtergraph, audio_filter = build_polish_filter(
            args,
            float(plan["end_seconds"] - plan["start_seconds"]),
            width,
            height,
            effects,
        )
        filtergraph = _append_chat_overlay(filtergraph, effects)
        filtergraph = _append_speech_subtitles(filtergraph, effects)
        effects["filter_summary"] = filtergraph
        effects["audio_filter"] = audio_filter
    return effects


def build_ffmpeg_clip_cmd(video: str | Path, out: Path, start: int, end: int, fast: bool, args, effects: dict) -> list[str]:
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", str(video)]
    if effects.get("polish"):
        cmd += [
            "-filter_complex", effects["filter_summary"],
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
        ]
        if effects.get("audio_filter"):
            cmd += ["-af", effects["audio_filter"]]
        cmd.append(str(out))
        return cmd
    if fast:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-c:a", "aac"]
    cmd.append(str(out))
    return cmd


def ffmpeg_clip(video: str | Path, out: Path, start: int, end: int, fast: bool, args=None, effects: dict | None = None) -> None:
    cmd = build_ffmpeg_clip_cmd(video, out, start, end, fast, args, effects or {})
    subprocess.run(cmd, check=True)


def ffmpeg_thumbnail(video: str | Path, out: Path, peak: int) -> None:
    cmd = ["ffmpeg", "-y", "-ss", str(peak), "-i", str(video), "-frames:v", "1", "-q:v", "2", "-update", "1", str(out)]
    subprocess.run(cmd, check=True)


def select_clip_plans(
    rows: list[dict],
    comments: list[dict],
    top: int,
    pad: int,
    forbidden_terms: set[str],
    analysis_pad: int | None = None,
) -> list[dict]:
    candidates = []
    deferred = []
    for row in rows:
        peak, micro = refine_peak_seconds(row, comments)
        start = max(0, peak - pad)
        end = peak + pad
        signal_pad = pad if analysis_pad is None else analysis_pad
        analysis_start = max(0, peak - signal_pad)
        analysis_end = peak + signal_pad
        signals = analyze_window(comments, analysis_start, analysis_end)
        density = _density_count(comments, analysis_start, analysis_end)
        quality = editor_quality_score(row, signals, micro, density)
        non_noise = (
            signals["clip_intent_hits"] >= 2
            or signals["top_phrase_count"] >= 8
            or max(signals.get("_mood_counts", {}).values() or [0]) >= 6
            or bool(signals["community_meme"])
        )
        skip_reason = ""
        if signals.get("break_marker"):
            continue
        if signals["noise_hits"] > 0 and not non_noise:
            skip_reason = "noise-only window"
        elif signals["unique_chatters"] < 5:
            skip_reason = "fewer than 5 unique chatters"

        plan = {
            "row": row,
            "signals": {k: v for k, v in signals.items() if not k.startswith("_")},
            "peak_seconds": peak,
            "source_peak_seconds": int(row["_peak_seconds"]),
            "start_seconds": start,
            "end_seconds": end,
            "analysis_start_seconds": analysis_start,
            "analysis_end_seconds": analysis_end,
            "skip_reason": skip_reason,
            "editor_quality": quality,
            "density": density,
            "micro_timing": micro,
        }
        if skip_reason:
            deferred.append(plan)
        else:
            candidates.append(plan)

    candidates.sort(
        key=lambda p: (
            p["editor_quality"],
            _row_float(p["row"], "virality"),
            p["density"],
        ),
        reverse=True,
    )
    selected = candidates[:top]
    for plan in deferred:
        if len(selected) >= top:
            break
        plan["included_despite"] = plan.pop("skip_reason")
        selected.append(plan)
    used_titles = set()
    for plan in selected:
        title = generate_title(plan["signals"], plan["row"], used_titles, forbidden_terms)
        description = generate_description(plan["signals"], forbidden_terms)
        assert_clean_copy(title, description, forbidden_terms)
        plan["title"] = title
        plan["description"] = description
    return selected[:top]


def write_outputs(plans: list[dict], args, layout: OutputLayout, forbidden_terms: set[str], comments: list[dict]) -> list[dict]:
    if not args.dry_run:
        ensure_output_layout(layout, thumbnail=args.thumbnail, subtitles=bool(getattr(args, "speech_subtitles", False)))
    clips = []
    shock_occurrence = 0
    with tempfile.TemporaryDirectory(prefix="parasite_export_") as td:
        tmp_dir = Path(td)
        for idx, plan in enumerate(plans, 1):
            row = plan["row"]
            slug = filename_slug(plan["title"])
            suffix = suffix_slug(getattr(args, "filename_suffix", ""))
            base = f"{idx:02d}_{slug}{'_' + suffix if suffix else ''}"
            paths = clip_file_paths(layout, base)
            mp4 = paths["video"]
            meta_path = paths["metadata"]
            thumb_path = paths["thumbnail"]
            subtitle_path = paths["subtitles"]
            overlay_path = tmp_dir / f"{base}_chat.ass"
            if getattr(args, "chat_overlay", False):
                if str(getattr(args, "chat_overlay_placement", "auto")).lower() == "auto":
                    plan["camera_box"] = detect_streamer_camera_box(args.video, plan["peak_seconds"], tmp_dir)
                box = write_chat_overlay_ass(comments, plan["start_seconds"], plan["end_seconds"], overlay_path, args, plan)
                plan["chat_overlay_box"] = box
                plan["chat_overlay_path"] = str(overlay_path)
            subtitle_info = None
            if getattr(args, "speech_subtitles", False):
                plan["speech_subtitle_path"] = str(subtitle_path)
                if not args.dry_run:
                    subtitle_info = transcribe_clip_to_srt(
                        args.video,
                        plan["start_seconds"],
                        plan["end_seconds"],
                        subtitle_path,
                        tmp_dir,
                        args,
                    )
                else:
                    subtitle_info = {
                        "path": str(subtitle_path),
                        "format": "srt",
                        "model": str(getattr(args, "subtitle_model", "tiny")),
                        "language": getattr(args, "subtitle_language", "") or "",
                        "segment_count": None,
                        "dry_run": True,
                    }
            if getattr(args, "shock_cam", False) and should_apply_shock_cam(
                plan["signals"],
                getattr(args, "shock_cam_min_mood", "SHOCK"),
                bool(getattr(args, "shock_cam_allow_noise", False)),
            ):
                shock_occurrence += 1
                plan["shock_cam_occurrence"] = shock_occurrence
                plan["shock_cam_variant"] = "full" if shock_occurrence % 2 == 1 else "three_quarter"
            effects = prepare_effects(args, plan, tmp_dir)
            effects_public = {
                k: v for k, v in effects.items()
                if not k.startswith("_") and k not in ("face_box", "filter_summary", "audio_filter", "chat_overlay_path")
            }
            if effects.get("filter_summary"):
                effects_public["filter_summary"] = effects["filter_summary"]
                effects_public["audio_filter"] = effects["audio_filter"]

            ffmpeg_cmd = None
            if not effects.get("polish") or effects.get("filter_summary"):
                ffmpeg_cmd = build_ffmpeg_clip_cmd(
                    args.video, mp4, plan["start_seconds"], plan["end_seconds"],
                    args.fast and not effects.get("polish"), args, effects,
                )

            if not args.dry_run:
                if not ffmpeg_cmd:
                    raise RuntimeError("Could not build ffmpeg command for polished export")
                subprocess.run(ffmpeg_cmd, check=True)
                if args.thumbnail:
                    ffmpeg_thumbnail(args.video, thumb_path, plan["peak_seconds"])

            meta = {
                "title": plan["title"],
                "description": plan["description"],
                "source_video": str(args.video),
                "source_chat": str(args.chat),
                "rank": _row_int(row, "rank"),
                "virality": _row_float(row, "virality"),
                "editor_quality": plan.get("editor_quality"),
                "source_peak_seconds": plan.get("source_peak_seconds", plan["peak_seconds"]),
                "source_peak_timestamp": hms(int(plan.get("source_peak_seconds", plan["peak_seconds"]))),
                "peak_seconds": plan["peak_seconds"],
                "peak_timestamp": hms(plan["peak_seconds"]),
                "start_seconds": plan["start_seconds"],
                "end_seconds": plan["end_seconds"],
                "start_timestamp": hms(plan["start_seconds"]),
                "end_timestamp": hms(plan["end_seconds"]),
                "analysis_start_seconds": plan.get("analysis_start_seconds"),
                "analysis_end_seconds": plan.get("analysis_end_seconds"),
                "analysis_start_timestamp": hms(int(plan.get("analysis_start_seconds", plan["start_seconds"]))),
                "analysis_end_timestamp": hms(int(plan.get("analysis_end_seconds", plan["end_seconds"]))),
                "reasoning": row.get("reasoning", ""),
                "density": plan.get("density"),
                "micro_timing": plan.get("micro_timing"),
                "signals": plan["signals"],
                "effects": effects_public,
            }
            if subtitle_info:
                meta["subtitles"] = subtitle_info
            if plan.get("included_despite"):
                meta["included_despite"] = plan["included_despite"]
            assert_clean_copy(meta["title"], meta["description"], forbidden_terms)

            if not args.dry_run:
                meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

            clip = {
                "filename": mp4.name,
                "metadata": meta,
                "metadata_path": str(meta_path),
                "thumbnail": str(thumb_path) if args.thumbnail else None,
                "subtitles": str(subtitle_path) if getattr(args, "speech_subtitles", False) else None,
                "ffmpeg_command": ffmpeg_cmd,
            }
            clips.append(clip)
    return clips


def write_manifest_files(clips: list[dict], args, layout: OutputLayout) -> None:
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_video": str(args.video),
        "requested_video": str(getattr(args, "requested_video", args.video)),
        "source_chat": str(args.chat),
        "source_scores": str(args.scores),
        "clip_count": len(clips),
        "clips": clips,
    }
    (layout.json / "export_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    with (layout.copy / "descriptions.txt").open("w", encoding="utf-8") as f:
        for clip in clips:
            meta = clip["metadata"]
            f.write(f"{clip['filename']}\n")
            f.write(f"Title: {meta['title']}\n")
            f.write(f"Description: {meta['description']}\n")
            f.write(f"Start/End: {meta['start_timestamp']} - {meta['end_timestamp']}\n")
            f.write(f"Score: {meta['virality']}\n\n")

    with (layout.copy / "upload_copy.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "title", "description", "tags", "start", "end", "score"])
        w.writeheader()
        for clip in clips:
            meta = clip["metadata"]
            w.writerow({
                "filename": clip["filename"],
                "title": meta["title"],
                "description": meta["description"],
                "tags": SAFE_TAGS,
                "start": meta["start_timestamp"],
                "end": meta["end_timestamp"],
                "score": meta["virality"],
            })


def export_top_clips(args) -> dict:
    args.scores = Path(args.scores)
    args.chat = Path(args.chat)
    args.video, args.video_is_local = resolve_video_source(args.video, args.scores)
    original_video_source = args.video
    args.requested_video = original_video_source
    out_dir = Path(args.out).expanduser()
    layout = build_output_layout(out_dir, flat=getattr(args, "flat_output", False))
    if args.top <= 0 or args.from_top <= 0 or args.pad < 0:
        raise ValueError("--top and --from-top must be positive; --pad must be >= 0")
    warnings = []
    if not args.video_is_local:
        warnings.append(f"local video not found; using Twitch VOD URL: {args.video}")
        if not args.dry_run:
            args.video = resolve_stream_url(args.video)
            warnings.append("resolved Twitch URL to direct media stream with yt-dlp")
    if args.fast and effects_enabled(args):
        warnings.append("--fast ignored because polish/effects require re-encode")
        args.fast = False

    rows, row_warnings = load_score_rows(args.scores, args.from_top, args.min_score)
    warnings.extend(row_warnings)
    comments = load_comments(args.chat)
    forbidden_terms = _forbidden_terms(args.scores, args.chat, args.video)
    analysis_pad = args.analysis_pad if args.analysis_pad is not None else None
    plans = select_clip_plans(rows, comments, args.top, args.pad, forbidden_terms, analysis_pad)

    clips = write_outputs(plans, args, layout, forbidden_terms, comments)
    if not args.dry_run:
        write_manifest_files(clips, args, layout)

    return {
        "output_dir": str(out_dir),
        "layout": {
            "videos": str(layout.videos),
            "json": str(layout.json),
            "thumbnails": str(layout.thumbnails),
            "copy": str(layout.copy),
            "subtitles": str(layout.subtitles),
        },
        "dry_run": args.dry_run,
        "source_video": str(args.video),
        "requested_video": str(original_video_source),
        "warnings": warnings,
        "clip_count": len(clips),
        "clips": clips,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export top Siphon clips from viral_score.csv")
    p.add_argument("scores", help="Path to viral_score.csv")
    p.add_argument("--chat", required=True, help="Path to chat.json")
    p.add_argument("--video", required=True,
                   help="Path to source VOD video, or Twitch URL. Missing local paths fall back to https://www.twitch.tv/videos/<id> when the VOD ID can be inferred.")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--from-top", type=int, default=15)
    p.add_argument("--pad", type=int, default=10)
    p.add_argument("--analysis-pad", type=int, default=None,
                   help="Use this pad for scoring/title signals while --pad controls exported clip length")
    p.add_argument("--out", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--filename-suffix", default="", help="Append a safe suffix before each file extension, e.g. v2")
    p.add_argument("--flat-output", action="store_true",
                   help="Write files directly to --out instead of videos/json/thumbnails/copy subfolders")
    p.add_argument("--fast", action="store_true", help="Use ffmpeg stream copy")
    p.add_argument("--dry-run", action="store_true", help="Print plans without calling ffmpeg or writing files")
    p.add_argument("--min-score", type=float, default=0)
    p.add_argument("--thumbnail", action="store_true", help="Export JPG at the peak timestamp")
    p.add_argument("--polish", action="store_true", help="Enable fade-out polish and force re-encode")
    p.add_argument("--intro-zoom", action="store_true", help="Slow zoom-out during clip intro")
    p.add_argument("--intro-zoom-duration", type=float, default=2.5)
    p.add_argument("--intro-start-zoom", type=float, default=1.12)
    p.add_argument("--fade-duration", type=float, default=0.75)
    p.add_argument("--no-fade", action="store_true", help="Disable video/audio fade while keeping other effects")
    p.add_argument("--shock-cam", action="store_true", help="Punch in on SHOCK windows")
    p.add_argument("--shock-cam-target", choices=["auto", "streamer", "content", "center", "face"], default="auto",
                   help="Target for punch-in zoom: auto, streamer webcam, main content, center, or largest detected face")
    p.add_argument("--shock-cam-duration", type=float, default=2.0)
    p.add_argument("--shock-cam-scale", type=float, default=1.35)
    p.add_argument("--shock-cam-full-scale", type=float, default=2.15,
                   help="Tighter fallback crop scale used for the first high-intensity punch-in")
    p.add_argument("--shock-cam-full-face-coverage", type=float, default=0.82,
                   help="When a face is detected, target face height share for the first punch-in")
    p.add_argument("--shock-cam-medium-face-coverage", type=float, default=0.62,
                   help="When a face is detected, target face height share for the second punch-in")
    p.add_argument("--shock-cam-before-peak", type=float, default=0.5)
    p.add_argument("--shock-cam-min-mood", default="SHOCK",
                   help="Dominant mood required for punch-in. Use ANY to focus every selected non-noise clip.")
    p.add_argument("--shock-cam-allow-noise", action="store_true",
                   help="Allow focus punch-in on noisy but still selected clips. Break markers still block it.")
    p.add_argument("--chat-overlay", action="store_true", help="Burn a transparent chatlog overlay above the camera area")
    p.add_argument("--chat-overlay-placement", choices=["auto", "fixed", "below-camera", "above-camera"], default="auto",
                   help="auto moves the overlay below a detected upper-left camera; fixed uses x/y directly")
    p.add_argument("--chat-overlay-x", type=int, default=28)
    p.add_argument("--chat-overlay-y", type=int, default=92)
    p.add_argument("--chat-overlay-width", type=int, default=540)
    p.add_argument("--chat-overlay-height", type=int, default=246)
    p.add_argument("--chat-overlay-lines", type=int, default=7)
    p.add_argument("--chat-overlay-duration", type=float, default=6.0)
    p.add_argument("--chat-overlay-max-per-second", type=int, default=2)
    p.add_argument("--chat-overlay-font-size", type=int, default=26)
    p.add_argument("--chat-overlay-line-height", type=int, default=34)
    p.add_argument("--chat-overlay-bg-alpha", type=int, default=255,
                   help="ASS alpha for overlay backing: 0 opaque, 255 invisible")
    p.add_argument("--speech-subtitles", action="store_true",
                   help="Generate speech-to-text SRT captions with faster-whisper and burn them into the clip")
    p.add_argument("--subtitle-sidecar-only", action="store_true",
                   help="Write .srt files without burning captions into the video")
    p.add_argument("--subtitle-model", default="tiny",
                   help="faster-whisper model name or local model path, e.g. tiny, base, small")
    p.add_argument("--subtitle-language", default=None,
                   help="Optional language code such as en. Leave empty for auto-detect")
    p.add_argument("--subtitle-device", default="cpu", choices=["cpu", "cuda", "auto"])
    p.add_argument("--subtitle-compute-type", default="int8",
                   help="faster-whisper compute type, e.g. int8, float16, int8_float16")
    p.add_argument("--subtitle-beam-size", type=int, default=1)
    p.add_argument("--subtitle-vad", action=argparse.BooleanOptionalAction, default=True,
                   help="Use voice activity detection while transcribing")
    p.add_argument(
        "--subtitle-style",
        default=(
            "Fontname=Arial,Fontsize=28,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&HCC000000,BorderStyle=1,Outline=3,Shadow=1,"
            "Alignment=2,MarginV=64"
        ),
        help="ASS force_style used when burning speech subtitles",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = export_top_clips(args)
    except Exception as e:
        print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    for warning in result["warnings"]:
        print(f"[warning] {warning}", file=sys.stderr)

    label = "DRY RUN" if result["dry_run"] else "EXPORTED"
    print(f"{label}: {result['clip_count']} clip(s) -> {result['output_dir']}")
    layout = result.get("layout") or {}
    if layout:
        print(
            "Layout: "
            f"videos={layout.get('videos')} | json={layout.get('json')} | "
            f"thumbnails={layout.get('thumbnails')} | copy={layout.get('copy')} | "
            f"subtitles={layout.get('subtitles')}"
        )
    for clip in result["clips"]:
        meta = clip["metadata"]
        signals = meta["signals"]
        print(f"\n{clip['filename']}")
        print(f"  Title: {meta['title']}")
        print(f"  Description: {meta['description']}")
        print(f"  Window: {meta['start_timestamp']} - {meta['end_timestamp']} ({meta['end_seconds'] - meta['start_seconds']}s)")
        print(
            f"  Score: {meta['virality']} | editor={meta.get('editor_quality')} "
            f"| source_peak={meta.get('source_peak_timestamp')} -> refined={meta.get('peak_timestamp')}"
        )
        if meta.get("micro_timing"):
            micro = meta["micro_timing"]
            print(
                "  Micro: "
                f"{hms(int(micro.get('micro_window_start', 0)))} - {hms(int(micro.get('micro_window_end', 0)))} "
                f"density={micro.get('micro_density')} score={micro.get('micro_score')}"
            )
        print(
            "  Signals: "
            f"phrase={signals.get('top_phrase')!r} x{signals.get('top_phrase_count')} "
            f"token={signals.get('top_token')!r} mood={signals.get('dominant_mood')} "
            f"intent={signals.get('clip_intent_hits')} noise={signals.get('noise_hits')} "
            f"users={signals.get('unique_chatters')}"
        )
        effects = meta.get("effects", {})
        if effects:
            print(
                "  Effects: "
                f"polish={effects.get('polish')} fade={effects.get('fade_out')} "
                f"intro_zoom={effects.get('intro_zoom')} shock_cam={effects.get('shock_cam')} "
                f"target={effects.get('shock_cam_resolved_target') or effects.get('shock_cam_target')} "
                f"face={effects.get('face_detected')} crop={effects.get('crop')}"
            )
            if effects.get("intro_zoom_skipped_reason"):
                print(f"  Intro zoom skipped: {effects['intro_zoom_skipped_reason']}")
            if effects.get("speech_subtitles"):
                print(
                    "  Speech subtitles: "
                    f"burned={effects.get('speech_subtitle_burned')} "
                    f"path={effects.get('speech_subtitle_path')}"
                )
            if effects.get("video_probe_skipped") and effects.get("polish"):
                print("  Filter summary: skipped because source video is unavailable")
            elif effects.get("filter_summary"):
                print(f"  Filter summary: {effects['filter_summary']}")
                print(f"  Audio filter: {effects.get('audio_filter')}")
        if clip.get("ffmpeg_command"):
            print("  FFmpeg: " + " ".join(str(x) for x in clip["ffmpeg_command"]))
        if meta.get("included_despite"):
            print(f"  Included despite: {meta['included_despite']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
