"""Codex content manager — Asmongold-style clip post-processing.

Layers visual effects onto Siphon-scored clips:
  - Drama Zoom: face punch-in on SHOCK/DRAMA peaks (score > threshold)
  - Thumerian Chat Crawl: crimson/silver scrolling chat during HYPE/FUNNY peaks
  - Speech Subtitles: centered Whisper captions
  - Logo/Branding: optional watermark overlay
  - Vertical Secondary: optional 9:16 split-screen (content top, cam bottom)

Primary output preserves the source video's native aspect ratio.
Vertical 9:16 is an optional secondary produced alongside via --vertical.
Pass --raw for clean cuts with zero post-processing.

Usage:
    python clipper.py archive/<dir>/viral_score.csv \\
        --chat archive/<dir>/chat.json \\
        --video path/to/vod.mp4 \\
        --top 5 --drama-zoom --chat-crawl --dry-run

    python clipper.py archive/<dir>/viral_score.csv \\
        --chat archive/<dir>/chat.json \\
        --video path/to/vod.mp4 --raw
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import threading as _threading
import time as _time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Windows cp1252 safety
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from heatmap import extract_body, extract_offset, hms, load_comments

from export_top_clips import (
    DEFAULT_OUT_DIR,
    SAFE_TAGS,
    _ass_color_for_user,
    _ass_escape,
    _ass_time,
    _even,
    _ffmpeg_filter_path,
    _num,
    _row_float,
    _row_int,
    _srt_time,
    _subtitle_text,
    analyze_window,
    assert_clean_copy,
    build_output_layout,
    clip_file_paths,
    compute_face_focus_crop,
    compute_punch_crop,
    detect_faces,
    detect_streamer_camera_box,
    ensure_output_layout,
    extract_frame,
    generate_description,
    generate_title,
    get_video_resolution,
    infer_vod_folder,
    is_url,
    load_score_rows,
    resolve_video_source,
    select_clip_plans,
    transcribe_clip_to_srt,
    write_manifest_files,
    _comment_user,
    _forbidden_terms,
)


# ---------------------------------------------------------------------------
# ANSI theme — Thumerian / vampiric
# ---------------------------------------------------------------------------

_IS_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

# Color codes (256-color)
_BLOOD = "\033[38;5;124m"      # deep blood red
_CRIMSON = "\033[38;5;196m"    # bright crimson
_EMBER = "\033[38;5;208m"      # ember orange
_SILVER = "\033[38;5;250m"     # silver/pale
_DIM = "\033[38;5;239m"        # ash gray
_BONE = "\033[38;5;230m"       # pale bone white
_PURPLE = "\033[38;5;99m"      # dark purple
_FANG = "\033[38;5;255m"       # bright white (fangs)
_BOLD = "\033[1m"
_RESET = "\033[0m"
_CLEAR = "\r\033[K"

BANNER = rf"""
{_BLOOD}     ___________   ___   ____  ______  __
{_CRIMSON}    / ____/ __ \ / _ \ / ___\| ___\ \/ /
{_CRIMSON}   / /   / / / // // // ___/|  |_  \  /
{_BLOOD}  / /___/ /_/ // __ / /__/  |  __/  / /
{_CRIMSON}  \____/\____//_/ \_\____/  |_|    /_/
{_DIM}  ~~~ the blood of engagement, distilled ~~~{_RESET}
"""

MOOD_ICONS = {
    "SHOCK": f"{_CRIMSON}!!{_RESET}",
    "DRAMA": f"{_PURPLE}**{_RESET}",
    "HYPE":  f"{_EMBER}>>>{_RESET}",
    "FUNNY": f"{_BONE}lol{_RESET}",
    "MIXED": f"{_DIM}~{_RESET}",
}


def _c(color: str, text: str) -> str:
    """Wrap text in ANSI color, only when stdout is a TTY."""
    if not _IS_TTY:
        return str(text)
    return f"{color}{text}{_RESET}"


# ---------------------------------------------------------------------------
# Blood-drip spinner — vampiric processing indicator
# ---------------------------------------------------------------------------

class BloodDrip:
    """Threaded ANSI spinner with dripping blood aesthetic."""

    FRAMES = [
        "  .o",
        " .oO",
        ".oOo",
        "oOo.",
        "Oo. ",
        "o.  ",
    ]
    DRIP_FRAMES = [
        f"{_BLOOD}.::.{_CRIMSON}",
        f"{_CRIMSON}:..:{_BLOOD}",
        f"{_BLOOD}.:::{_CRIMSON}",
        f"{_CRIMSON}::::{_BLOOD}",
        f"{_BLOOD}::..{_CRIMSON}",
        f"{_CRIMSON}..:{_BLOOD}.",
    ]

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.enabled = _IS_TTY
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
            self.stream.write(_CLEAR)
            self.stream.flush()
        except Exception:
            pass

    def _run(self):
        tick = 0
        while self._running:
            drip = self.DRIP_FRAMES[tick % len(self.DRIP_FRAMES)]
            label = f" {_DIM}{self._label}{_RESET}" if self._label else ""
            line = f"{_CLEAR}  {drip}{_BLOOD} ~ {_RESET}{label}"
            try:
                self.stream.write(line)
                self.stream.flush()
            except Exception:
                break
            tick += 1
            _time.sleep(0.15)


_spinner = BloodDrip()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERTICAL_W, VERTICAL_H = 1080, 1920
CONTENT_RATIO_DEFAULT = 0.60  # top portion of vertical frame

DRAMA_MOODS = {"SHOCK", "DRAMA"}
DRAMA_MIN_SCORE = 85
DRAMA_ZOOM_DURATION = 2.0
DRAMA_ZOOM_BEFORE_PEAK = 0.5
DRAMA_ZOOM_FACE_COVERAGE = 0.78
DRAMA_ZOOM_FALLBACK_SCALE = 2.0

CRAWL_MOODS = {"HYPE", "FUNNY"}
# ASS uses BGR order (not RGB)
CRIMSON_PRIMARY = "&H003232FF"   # deep crimson
SILVER_OUTLINE = "&H00C0C0C0"   # silver glow

CODEX_OUT_DIR = DEFAULT_OUT_DIR


# ---------------------------------------------------------------------------
# Drama Zoom — face punch-in on high-scoring SHOCK/DRAMA peaks
# ---------------------------------------------------------------------------

def should_apply_drama_zoom(
    signals: dict,
    row: dict,
    min_score: float = DRAMA_MIN_SCORE,
    moods: set[str] | None = None,
) -> bool:
    moods = moods or DRAMA_MOODS
    mood = str(signals.get("dominant_mood", "")).upper()
    if mood not in moods:
        return False
    virality = _num(row.get("virality"), 0)
    if virality < min_score:
        return False
    if signals.get("break_marker"):
        return False
    if int(signals.get("noise_hits", 0) or 0) > 0:
        return False
    return True


def _build_drama_crop(
    video: str | Path,
    timestamp: int,
    width: int,
    height: int,
    tmp_dir: Path,
    coverage: float = DRAMA_ZOOM_FACE_COVERAGE,
    fallback_scale: float = DRAMA_ZOOM_FALLBACK_SCALE,
) -> dict:
    """Extract a frame, detect faces, and compute a drama-zoom crop box."""
    frame_path = tmp_dir / f"codex_drama_{timestamp}.jpg"
    try:
        extract_frame(video, timestamp, frame_path)
    except Exception:
        return compute_punch_crop(width, height, None, fallback_scale)
    faces = detect_faces(frame_path)
    face = faces[0] if faces else None
    return compute_face_focus_crop(width, height, face, coverage, fallback_scale)


def build_drama_zoom_filter(
    width: int,
    height: int,
    crop: dict,
    peak_rel: float,
    clip_duration: float,
    zoom_duration: float = DRAMA_ZOOM_DURATION,
    before_peak: float = DRAMA_ZOOM_BEFORE_PEAK,
    fade_out: bool = True,
    fade_duration: float = 0.75,
) -> tuple[str, str]:
    """Build a trim/crop/scale/concat filtergraph for the drama zoom effect.

    Same segmentation pattern as export_top_clips shock_cam.
    Returns (video_filter, audio_filter).
    """
    fade_dur = max(0.01, min(fade_duration, clip_duration))
    fade_start = max(0.0, clip_duration - fade_dur)
    audio_filter = f"afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if fade_out else ""

    zoom_start = max(0.0, peak_rel - before_peak)
    zoom_end = min(clip_duration, zoom_start + zoom_duration)
    if zoom_end <= zoom_start:
        zoom_start, zoom_end = 0.0, min(clip_duration, zoom_duration)

    specs: list[str] = []
    labels: list[str] = []
    idx = 0

    # Segment before zoom
    if zoom_start > 0.01:
        specs.append(f"[0:v]trim=start=0:end={zoom_start:.3f},setpts=PTS-STARTPTS[v{idx}]")
        labels.append(f"[v{idx}]")
        idx += 1

    # Zoomed segment
    specs.append(
        f"[0:v]trim=start={zoom_start:.3f}:end={zoom_end:.3f},setpts=PTS-STARTPTS,"
        f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']},"
        f"scale={width}:{height},setsar=1[v{idx}]"
    )
    labels.append(f"[v{idx}]")
    idx += 1

    # Segment after zoom
    if zoom_end < clip_duration - 0.01:
        specs.append(f"[0:v]trim=start={zoom_end:.3f}:end={clip_duration:.3f},setpts=PTS-STARTPTS[v{idx}]")
        labels.append(f"[v{idx}]")

    if len(labels) > 1:
        tail = f",fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if fade_out else ""
        specs.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0{tail}[vout]")
    else:
        tail = f"fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if fade_out else "null"
        specs.append(f"{labels[0]}{tail}[vout]")

    return ";".join(specs), audio_filter


def _build_simple_filter(clip_duration: float, fade_out: bool, fade_duration: float) -> tuple[str, str]:
    """Null passthrough with optional fade."""
    fade_dur = max(0.01, min(fade_duration, clip_duration))
    fade_start = max(0.0, clip_duration - fade_dur)
    audio_filter = f"afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if fade_out else ""
    if fade_out:
        vf = f"[0:v]fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}[vout]"
    else:
        vf = "[0:v]null[vout]"
    return vf, audio_filter


# ---------------------------------------------------------------------------
# Thumerian Chat Crawl — crimson/silver scrolling overlay
# ---------------------------------------------------------------------------

def write_chat_crawl_ass(
    comments: list[dict],
    start: int,
    end: int,
    out_path: Path,
    args,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> Path:
    """Rolling chat stack overlay — messages stack upward like Twitch chat.

    New messages appear at the bottom of the stack and push older ones up.
    Uses snapshot-interval rendering (same approach as export_top_clips chat
    overlay) with \\pos() for static positioning. Crimson primary with silver
    glow outline.
    """
    duration = max(0.1, end - start)
    font_size = int(getattr(args, "chat_crawl_font_size", 38))
    hold = max(2.0, float(getattr(args, "chat_crawl_speed", 6.0)))
    max_lines = max(1, int(getattr(args, "chat_crawl_max_concurrent", 8)))
    max_per_second = max(1, int(getattr(args, "chat_crawl_max_per_second", 3)))
    line_h = font_size + 10

    # Stack zone: top-left area of the frame (like Twitch chat)
    stack_x = int(play_res_x * 0.01)
    stack_y_top = int(play_res_y * 0.02)
    stack_height = max_lines * line_h
    clip_bottom = stack_y_top + stack_height

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
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
            f"Style: Crawl,Arial,{font_size},{CRIMSON_PRIMARY},&H000000FF,"
            f"{SILVER_OUTLINE},&H00000000,-1,0,0,0,100,100,0,0,1,3,1,"
            "7,0,0,0,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # Collect messages in the window
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
        if not body or len(body) < 2:
            continue
        user = _ass_escape(_comment_user(comment, idx))
        rel = max(0.0, float(off - start))
        rel_end = min(duration, rel + hold)
        if rel_end <= rel:
            continue
        feed.append({
            "rel": rel,
            "end": rel_end,
            "user": user,
            "body": body,
            "color": _ass_color_for_user(user),
        })
        per_second[sec] += 1

    feed.sort(key=lambda m: m["rel"])

    # Snapshot-interval rendering: at each breakpoint, render the visible
    # stack with newest at the bottom, older messages pushed upward.
    if feed:
        breakpoints = {0.0, duration}
        for msg in feed:
            breakpoints.add(round(float(msg["rel"]), 3))
            breakpoints.add(round(float(msg["end"]), 3))
        ordered = sorted(bp for bp in breakpoints if 0.0 <= bp <= duration)

        for iv_start, iv_end in zip(ordered, ordered[1:]):
            if iv_end - iv_start < 0.01:
                continue
            active = [
                m for m in feed
                if m["rel"] <= iv_start < m["end"]
            ]
            visible = active[-max_lines:]
            for row, msg in enumerate(visible):
                row_y = stack_y_top + row * line_h
                text = (
                    rf"{{\clip({stack_x},{stack_y_top},{play_res_x},{clip_bottom})"
                    rf"\pos({stack_x},{row_y})"
                    rf"\c{msg['color']}}}{msg['user']} "
                    rf"{{\c{CRIMSON_PRIMARY}}}{msg['body']}"
                )
                lines.append(
                    f"Dialogue: 5,{_ass_time(iv_start)},{_ass_time(iv_end)},"
                    f"Crawl,,0,0,0,,{text}"
                )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Logo / Branding overlay
# ---------------------------------------------------------------------------

def _append_logo_filter(filtergraph: str, position: str, size: int) -> str:
    """Append a logo overlay assuming [1:v] is the logo input."""
    if position == "top-left":
        pos = "overlay=20:20"
    else:
        pos = "overlay=W-w-20:20"
    if filtergraph.endswith("[vout]"):
        return (
            filtergraph[:-6] + "[_pre_logo];"
            f"[1:v]scale={size}:-1[_logo];"
            f"[_pre_logo][_logo]{pos}[vout]"
        )
    return filtergraph


# ---------------------------------------------------------------------------
# Subtitle filter append
# ---------------------------------------------------------------------------

def _append_subtitles(filtergraph: str, srt_path: Path, style: str) -> str:
    sub = f"subtitles='{_ffmpeg_filter_path(srt_path)}'"
    if style:
        escaped = style.replace("\\", r"\\").replace("'", r"\'")
        sub += f":force_style='{escaped}'"
    if filtergraph.endswith("[vout]"):
        return filtergraph[:-6] + f",{sub}[vout]"
    return f"[0:v]{sub}[vout]"


def _append_crawl_overlay(filtergraph: str, ass_path: Path) -> str:
    sub = f"subtitles='{_ffmpeg_filter_path(ass_path)}'"
    if filtergraph.endswith("[vout]"):
        return filtergraph[:-6] + f",{sub}[vout]"
    return f"[0:v]{sub}[vout]"


# ---------------------------------------------------------------------------
# Vertical 9:16 secondary output
# ---------------------------------------------------------------------------

def detect_vertical_regions(
    video: str | Path,
    timestamp: int,
    width: int,
    height: int,
    tmp_dir: Path,
) -> dict:
    """Probe source to find camera and content crop regions."""
    camera_box = detect_streamer_camera_box(video, timestamp, tmp_dir)
    if camera_box:
        cam = {
            "x": camera_box["x"],
            "y": camera_box["y"],
            "w": camera_box["w"],
            "h": camera_box["h"],
        }
        cx = min(width, cam["x"] + cam["w"])
        content = {
            "x": _even(cx, 0),
            "y": 0,
            "w": _even(max(200, width - cx), 2),
            "h": height,
        }
    else:
        cam = {"x": 0, "y": 0, "w": _even(width * 0.42), "h": _even(height * 0.56)}
        content = {
            "x": _even(width * 0.30, 0),
            "y": 0,
            "w": _even(width * 0.70),
            "h": height,
        }
    return {"camera": cam, "content": content, "source_w": width, "source_h": height}


def build_vertical_filter(
    regions: dict,
    clip_duration: float,
    content_ratio: float = CONTENT_RATIO_DEFAULT,
    fade_out: bool = True,
    fade_duration: float = 0.75,
) -> tuple[str, str]:
    """Build filter_complex for 9:16 split-screen: content top, camera bottom."""
    content_h = _even(VERTICAL_H * content_ratio)
    cam_h = VERTICAL_H - content_h
    cam = regions["camera"]
    content = regions["content"]

    fade_dur = max(0.01, min(fade_duration, clip_duration))
    fade_start = max(0.0, clip_duration - fade_dur)
    audio_filter = f"afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if fade_out else ""

    fade_tail = f",fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}" if fade_out else ""

    vf = (
        f"[0:v]split=2[_top_in][_bot_in];"
        f"[_top_in]crop={content['w']}:{content['h']}:{content['x']}:{content['y']},"
        f"scale={VERTICAL_W}:{content_h}:force_original_aspect_ratio=decrease,"
        f"pad={VERTICAL_W}:{content_h}:(ow-iw)/2:(oh-ih)/2[_top];"
        f"[_bot_in]crop={cam['w']}:{cam['h']}:{cam['x']}:{cam['y']},"
        f"scale={VERTICAL_W}:{cam_h}:force_original_aspect_ratio=decrease,"
        f"pad={VERTICAL_W}:{cam_h}:(ow-iw)/2:(oh-ih)/2[_bot];"
        f"[_top][_bot]vstack=inputs=2{fade_tail}[vout]"
    )
    return vf, audio_filter


# ---------------------------------------------------------------------------
# FFmpeg command builders
# ---------------------------------------------------------------------------

def _build_filter_ffmpeg_cmd(
    video: str | Path,
    out: Path,
    start: int,
    end: int,
    filtergraph: str,
    audio_filter: str,
    extra_inputs: list[str] | None = None,
) -> list[str]:
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", str(video)]
    for inp in (extra_inputs or []):
        cmd += ["-i", str(inp)]
    cmd += [
        "-filter_complex", filtergraph,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    ]
    if audio_filter:
        cmd += ["-af", audio_filter]
    cmd.append(str(out))
    return cmd


def _build_raw_ffmpeg_cmd(
    video: str | Path,
    out: Path,
    start: int,
    end: int,
) -> list[str]:
    """Stream-copy cut with no filters — fastest possible export."""
    return [
        "ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", str(video),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ]


# ---------------------------------------------------------------------------
# CLI output helpers — vampiric theme
# ---------------------------------------------------------------------------

def _print_banner():
    if _IS_TTY:
        print(BANNER)


def _print_phase(label: str):
    if _IS_TTY:
        print(f"\n  {_BLOOD}---{_CRIMSON} {label} {_BLOOD}---{_RESET}")
    else:
        print(f"\n--- {label} ---")


def _mood_badge(mood: str) -> str:
    icon = MOOD_ICONS.get(mood, _c(_DIM, "~"))
    return f"{icon} {_c(_SILVER, mood)}"


def _score_color(score: float) -> str:
    if score >= 85:
        return _c(_CRIMSON, f"{score:.0f}")
    if score >= 60:
        return _c(_EMBER, f"{score:.0f}")
    return _c(_DIM, f"{score:.0f}")


def _effect_tag(name: str, active: bool) -> str:
    if active:
        return _c(_CRIMSON, name)
    return _c(_DIM, name)


def _print_clip_card(idx: int, clip: dict, raw_mode: bool):
    meta = clip["metadata"]
    signals = meta.get("signals", {})
    effects = meta.get("codex_effects", {})
    mood = str(signals.get("dominant_mood", "MIXED")).upper()
    score = float(meta.get("virality", 0))
    duration = int(meta.get("end_seconds", 0)) - int(meta.get("start_seconds", 0))

    # Header bar
    print()
    if _IS_TTY:
        print(f"  {_BLOOD}[{_CRIMSON}{idx:02d}{_BLOOD}]{_RESET} {_FANG}{meta['title']}{_RESET}")
    else:
        print(f"  [{idx:02d}] {meta['title']}")

    # Score + mood line
    print(
        f"       {_mood_badge(mood)}  "
        f"score {_score_color(score)}  "
        f"{_c(_DIM, meta['start_timestamp'])} {_c(_SILVER, '-')} "
        f"{_c(_DIM, meta['end_timestamp'])} "
        f"{_c(_DIM, f'({duration}s)')}"
    )

    # Phrase signal
    phrase = signals.get("top_phrase", "")
    phrase_n = signals.get("top_phrase_count", 0)
    if phrase:
        print(f"       {_c(_SILVER, 'signal')} {_c(_BONE, repr(phrase))} x{phrase_n}")

    if raw_mode:
        print(f"       {_c(_DIM, 'mode')} {_c(_SILVER, 'raw (no effects)')}")
    else:
        # Effect badges
        tags = []
        if effects.get("drama_zoom"):
            tags.append(_effect_tag("DRAMA-ZOOM", True))
        if effects.get("chat_crawl"):
            tags.append(_effect_tag("CHAT-CRAWL", True))
        if effects.get("speech_subtitles"):
            tags.append(_effect_tag("SUBTITLES", True))
        if effects.get("logo"):
            tags.append(_effect_tag("LOGO", True))
        if effects.get("vertical"):
            tags.append(_effect_tag("VERTICAL", True))
        if tags:
            print(f"       {_c(_DIM, 'fx')} {' '.join(tags)}")

    if clip.get("vertical"):
        print(f"       {_c(_DIM, 'vert')} {_c(_PURPLE, Path(clip['vertical']).name)}")


def _print_summary(result: dict, raw_mode: bool):
    count = result["clip_count"]
    out = result["output_dir"]
    is_dry = result["dry_run"]

    if _IS_TTY:
        label = _c(_DIM, "DRY RUN") if is_dry else _c(_CRIMSON, "SIPHONED")
        mode = _c(_SILVER, "raw") if raw_mode else _c(_CRIMSON, "codex")
        print(f"\n  {_BLOOD}{'=' * 44}{_RESET}")
        print(f"  {label} {_c(_FANG, str(count))} clip(s) {_c(_DIM, '-')} mode: {mode}")
        print(f"  {_c(_DIM, 'video')} {_c(_SILVER, result.get('video_source_kind') or 'unknown')} {_c(_DIM, result.get('source_video') or '')}")
        print(f"  {_c(_DIM, out)}")
        print(f"  {_BLOOD}{'=' * 44}{_RESET}\n")
    else:
        label = "DRY RUN" if is_dry else "SIPHONED"
        mode = "raw" if raw_mode else "codex"
        print(f"\n{'=' * 44}")
        print(f"{label}: {count} clip(s) - mode: {mode}")
        print(f"video: {result.get('video_source_kind') or 'unknown'} {result.get('source_video') or ''}")
        print(out)
        print(f"{'=' * 44}\n")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def export_codex_clips(args) -> dict:
    """Cut and process clips with Codex visual style."""
    args.scores = Path(args.scores)
    args.chat = Path(args.chat)
    args.video, args.video_is_local, args.video_source_kind = resolve_video_source(
        args.video,
        args.scores,
        require_local=bool(getattr(args, "require_local_video", False)),
        allow_twitch_fallback=bool(getattr(args, "allow_twitch_fallback", True)),
    )
    original_video = args.video
    out_dir = Path(args.out).expanduser()
    vod_folder = infer_vod_folder(args.scores)
    if vod_folder:
        out_dir = out_dir / vod_folder
    layout = build_output_layout(out_dir, flat=getattr(args, "flat_output", False))

    raw_mode = bool(getattr(args, "raw", False))

    # --raw forces all effects off
    if raw_mode:
        args.drama_zoom = False
        args.chat_crawl = False
        args.speech_subtitles = False
        args.no_fade = True
        args.logo = None
        args.vertical = False

    warnings: list[str] = []
    if not args.video_is_local:
        warnings.append(f"local video not found; using Twitch VOD URL: {args.video}")
        if not args.dry_run:
            from export_top_clips import resolve_stream_url
            args.video = resolve_stream_url(args.video)
            warnings.append("resolved Twitch URL to direct media stream")

    _spinner.start("loading scores + chat")
    rows, row_warnings = load_score_rows(args.scores, args.from_top, args.min_score)
    warnings.extend(row_warnings)
    comments = load_comments(args.chat)
    forbidden_terms = _forbidden_terms(args.scores, args.chat, args.video)
    analysis_pad = getattr(args, "analysis_pad", None)
    plans = select_clip_plans(rows, comments, args.top, args.pad, forbidden_terms, analysis_pad)
    _spinner.stop()

    if not args.dry_run:
        ensure_output_layout(layout, thumbnail=args.thumbnail, subtitles=bool(getattr(args, "speech_subtitles", False)))

    # Probe video dimensions once (skip for raw — not needed)
    width, height = 0, 0
    if not raw_mode and (args.video_is_local or not args.dry_run):
        try:
            width, height = get_video_resolution(args.video)
        except Exception:
            if not args.dry_run:
                raise

    drama_moods = set(getattr(args, "drama_zoom_moods", "SHOCK,DRAMA").upper().split(","))
    drama_min = float(getattr(args, "drama_zoom_min_score", DRAMA_MIN_SCORE))

    clips: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="codex_export_") as td:
        tmp_dir = Path(td)
        for idx, plan in enumerate(plans, 1):
            row = plan["row"]
            signals = plan["signals"]
            slug = plan.get("title", f"clip_{idx}")
            from export_top_clips import filename_slug, suffix_slug
            base_slug = filename_slug(slug)
            suffix = suffix_slug(getattr(args, "filename_suffix", ""))
            base = f"{idx:02d}_{base_slug}{'_' + suffix if suffix else ''}"
            paths = clip_file_paths(layout, base)
            mp4 = paths["video"]
            meta_path = paths["metadata"]

            peak_rel = float(plan["peak_seconds"] - plan["start_seconds"])
            clip_duration = float(plan["end_seconds"] - plan["start_seconds"])

            _spinner.start(f"clip {idx}/{len(plans)}: {slug[:40]}")

            # --- Raw mode: stream copy, skip everything ---
            if raw_mode:
                ffmpeg_cmd = _build_raw_ffmpeg_cmd(
                    args.video, mp4,
                    plan["start_seconds"], plan["end_seconds"],
                )
                if not args.dry_run:
                    subprocess.run(ffmpeg_cmd, check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                _spinner.stop()
                meta = {
                    "title": plan.get("title", ""),
                    "description": plan.get("description", ""),
                    "source_video": str(args.video),
                    "source_chat": str(args.chat),
                    "rank": _row_int(row, "rank"),
                    "virality": _row_float(row, "virality"),
                    "peak_seconds": plan["peak_seconds"],
                    "peak_timestamp": hms(plan["peak_seconds"]),
                    "start_seconds": plan["start_seconds"],
                    "end_seconds": plan["end_seconds"],
                    "start_timestamp": hms(plan["start_seconds"]),
                    "end_timestamp": hms(plan["end_seconds"]),
                    "reasoning": row.get("reasoning", ""),
                    "codex_effects": {
                        "drama_zoom": False, "chat_crawl": False,
                        "speech_subtitles": False, "logo": False,
                        "vertical": False, "raw": True,
                    },
                    "signals": {k: v for k, v in signals.items() if not k.startswith("_")},
                }
                if not args.dry_run:
                    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                clips.append({
                    "filename": mp4.name,
                    "metadata": meta,
                    "metadata_path": str(meta_path),
                    "thumbnail": None,
                    "subtitles": None,
                    "vertical": None,
                    "ffmpeg_command": ffmpeg_cmd,
                    "vertical_command": None,
                })
                continue

            # --- Drama Zoom ---
            drama_active = bool(
                getattr(args, "drama_zoom", True)
                and should_apply_drama_zoom(signals, row, drama_min, drama_moods)
                and width > 0
            )
            crop = None
            if drama_active and not args.dry_run:
                crop = _build_drama_crop(
                    args.video,
                    plan["peak_seconds"],
                    width,
                    height,
                    tmp_dir,
                    coverage=float(getattr(args, "drama_zoom_face_coverage", DRAMA_ZOOM_FACE_COVERAGE)),
                    fallback_scale=DRAMA_ZOOM_FALLBACK_SCALE,
                )

            if drama_active and crop:
                filtergraph, audio_filter = build_drama_zoom_filter(
                    width, height, crop, peak_rel, clip_duration,
                    zoom_duration=float(getattr(args, "drama_zoom_duration", DRAMA_ZOOM_DURATION)),
                    before_peak=DRAMA_ZOOM_BEFORE_PEAK,
                    fade_out=not getattr(args, "no_fade", False),
                    fade_duration=float(getattr(args, "fade_duration", 0.75)),
                )
            else:
                filtergraph, audio_filter = _build_simple_filter(
                    clip_duration,
                    fade_out=not getattr(args, "no_fade", False),
                    fade_duration=float(getattr(args, "fade_duration", 0.75)),
                )

            # --- Chat Crawl ---
            crawl_path = None
            crawl_active = bool(
                getattr(args, "chat_crawl", True)
                and str(signals.get("dominant_mood", "")).upper() in CRAWL_MOODS
            )
            if crawl_active:
                crawl_path = tmp_dir / f"{base}_crawl.ass"
                res_x = width if width > 0 else 1920
                res_y = height if height > 0 else 1080
                write_chat_crawl_ass(
                    comments, plan["start_seconds"], plan["end_seconds"],
                    crawl_path, args,
                    play_res_x=res_x, play_res_y=res_y,
                )
                filtergraph = _append_crawl_overlay(filtergraph, crawl_path)

            # --- Speech Subtitles ---
            subtitle_info = None
            srt_path = None
            if getattr(args, "speech_subtitles", False):
                srt_path = paths["subtitles"]
                if not args.dry_run:
                    subtitle_info = transcribe_clip_to_srt(
                        args.video,
                        plan["start_seconds"],
                        plan["end_seconds"],
                        srt_path,
                        tmp_dir,
                        args,
                    )
                else:
                    subtitle_info = {
                        "path": str(srt_path),
                        "format": "srt",
                        "model": str(getattr(args, "subtitle_model", "tiny")),
                        "dry_run": True,
                    }
                if not getattr(args, "subtitle_sidecar_only", False) and srt_path:
                    style = str(getattr(args, "subtitle_style", ""))
                    filtergraph = _append_subtitles(filtergraph, srt_path, style)

            # --- Logo ---
            extra_inputs: list[str] = []
            logo_path = getattr(args, "logo", None)
            if logo_path and Path(logo_path).exists():
                extra_inputs.append(str(logo_path))
                filtergraph = _append_logo_filter(
                    filtergraph,
                    position=str(getattr(args, "logo_position", "top-right")),
                    size=int(getattr(args, "logo_size", 120)),
                )

            # --- Build + run primary ffmpeg ---
            ffmpeg_cmd = _build_filter_ffmpeg_cmd(
                args.video, mp4,
                plan["start_seconds"], plan["end_seconds"],
                filtergraph, audio_filter,
                extra_inputs=extra_inputs,
            )

            if not args.dry_run:
                subprocess.run(ffmpeg_cmd, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # Thumbnail
                if args.thumbnail:
                    from export_top_clips import ffmpeg_thumbnail
                    ffmpeg_thumbnail(args.video, paths["thumbnail"], plan["peak_seconds"])

            # --- Vertical secondary ---
            vertical_path = None
            vertical_cmd = None
            if getattr(args, "vertical", False) and width > 0:
                vert_base = f"{base}_vertical"
                vert_mp4 = layout.videos / f"{vert_base}.mp4"
                regions = detect_vertical_regions(
                    args.video, plan["peak_seconds"], width, height, tmp_dir,
                )
                content_ratio = float(getattr(args, "content_ratio", CONTENT_RATIO_DEFAULT))
                vert_fg, vert_af = build_vertical_filter(
                    regions, clip_duration,
                    content_ratio=content_ratio,
                    fade_out=not getattr(args, "no_fade", False),
                    fade_duration=float(getattr(args, "fade_duration", 0.75)),
                )
                if crawl_active:
                    vert_crawl = tmp_dir / f"{base}_crawl_vert.ass"
                    write_chat_crawl_ass(
                        comments, plan["start_seconds"], plan["end_seconds"],
                        vert_crawl, args,
                        play_res_x=VERTICAL_W, play_res_y=VERTICAL_H,
                    )
                    vert_fg = _append_crawl_overlay(vert_fg, vert_crawl)

                if srt_path and not getattr(args, "subtitle_sidecar_only", False):
                    vert_style = (
                        f"Fontname=Arial,Fontsize=32,PrimaryColour=&H00FFFFFF,"
                        f"OutlineColour=&HCC000000,BorderStyle=1,Outline=3,Shadow=1,"
                        f"Alignment=2,MarginV={int(VERTICAL_H * 0.25)}"
                    )
                    vert_fg = _append_subtitles(vert_fg, srt_path, vert_style)

                vertical_cmd = _build_filter_ffmpeg_cmd(
                    args.video, vert_mp4,
                    plan["start_seconds"], plan["end_seconds"],
                    vert_fg, vert_af,
                )

                if not args.dry_run:
                    subprocess.run(vertical_cmd, check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                vertical_path = str(vert_mp4)

            _spinner.stop()

            # --- Metadata ---
            meta = {
                "title": plan.get("title", ""),
                "description": plan.get("description", ""),
                "source_video": str(args.video),
                "source_chat": str(args.chat),
                "rank": _row_int(row, "rank"),
                "virality": _row_float(row, "virality"),
                "editor_quality": plan.get("editor_quality"),
                "peak_seconds": plan["peak_seconds"],
                "peak_timestamp": hms(plan["peak_seconds"]),
                "start_seconds": plan["start_seconds"],
                "end_seconds": plan["end_seconds"],
                "start_timestamp": hms(plan["start_seconds"]),
                "end_timestamp": hms(plan["end_seconds"]),
                "reasoning": row.get("reasoning", ""),
                "codex_effects": {
                    "drama_zoom": drama_active,
                    "drama_zoom_crop": crop,
                    "chat_crawl": crawl_active,
                    "speech_subtitles": bool(subtitle_info),
                    "logo": bool(logo_path),
                    "vertical": vertical_path is not None,
                    "raw": False,
                },
                "signals": {k: v for k, v in signals.items() if not k.startswith("_")},
            }
            if subtitle_info:
                meta["subtitles"] = subtitle_info
            if plan.get("included_despite"):
                meta["included_despite"] = plan["included_despite"]

            if not args.dry_run:
                meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

            clip_entry = {
                "filename": mp4.name,
                "metadata": meta,
                "metadata_path": str(meta_path),
                "thumbnail": str(paths["thumbnail"]) if args.thumbnail else None,
                "subtitles": str(srt_path) if srt_path else None,
                "vertical": vertical_path,
                "ffmpeg_command": ffmpeg_cmd,
                "vertical_command": vertical_cmd,
            }
            clips.append(clip_entry)

    # Manifest
    if not args.dry_run and clips:
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
        "raw": raw_mode,
        "source_video": str(original_video),
        "video_source_kind": str(getattr(args, "video_source_kind", "")),
        "warnings": warnings,
        "clip_count": len(clips),
        "clips": clips,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Codex content manager -- vampiric clip post-processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "modes:\n"
            "  default    all effects active (drama zoom, chat crawl, fade)\n"
            "  --raw      clean stream-copy cuts, zero post-processing\n"
        ),
    )
    p.add_argument("scores", help="Path to viral_score.csv")
    p.add_argument("--chat", required=True, help="Path to chat.json")
    p.add_argument("--video", default="", help="Path to source VOD video, Twitch URL, or empty to infer/search Desktop/VODs")
    p.add_argument("--require-local-video", action="store_true",
                   help="Fail if the raw VOD is not found in ~/Desktop/VODs or VODS_DIR")
    p.add_argument("--allow-twitch-fallback", action=argparse.BooleanOptionalAction, default=True,
                   help="Allow Twitch URL fallback when no local raw VOD is found")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--from-top", type=int, default=20)
    p.add_argument("--pad", type=int, default=10)
    p.add_argument("--analysis-pad", type=int, default=None)
    p.add_argument("--out", default=str(CODEX_OUT_DIR))
    p.add_argument("--filename-suffix", default="")
    p.add_argument("--flat-output", action="store_true")
    p.add_argument("--fast", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--min-score", type=float, default=0)
    p.add_argument("--thumbnail", action="store_true")

    # Raw mode
    p.add_argument("--raw", action="store_true",
                   help="Clean cuts with zero effects — stream copy, no filters, no fade")

    # Fade
    p.add_argument("--fade-duration", type=float, default=0.75)
    p.add_argument("--no-fade", action="store_true")

    # Drama Zoom
    p.add_argument("--drama-zoom", action=argparse.BooleanOptionalAction, default=True,
                   help="Face punch-in on SHOCK/DRAMA peaks (default on)")
    p.add_argument("--drama-zoom-min-score", type=float, default=DRAMA_MIN_SCORE)
    p.add_argument("--drama-zoom-moods", default="SHOCK,DRAMA")
    p.add_argument("--drama-zoom-duration", type=float, default=DRAMA_ZOOM_DURATION)
    p.add_argument("--drama-zoom-face-coverage", type=float, default=DRAMA_ZOOM_FACE_COVERAGE)

    # Chat Crawl
    p.add_argument("--chat-crawl", action=argparse.BooleanOptionalAction, default=True,
                   help="Thumerian chat crawl on HYPE/FUNNY peaks (default on)")
    p.add_argument("--chat-crawl-font-size", type=int, default=38)
    p.add_argument("--chat-crawl-speed", type=float, default=6.0,
                   help="Seconds for a message to cross the screen")
    p.add_argument("--chat-crawl-max-concurrent", type=int, default=3)
    p.add_argument("--chat-crawl-max-per-second", type=int, default=2)

    # Logo
    p.add_argument("--logo", default=None, help="Path to logo PNG for watermark")
    p.add_argument("--logo-size", type=int, default=120, help="Logo width in pixels")
    p.add_argument("--logo-position", choices=["top-right", "top-left"], default="top-right")

    # Vertical secondary
    p.add_argument("--vertical", action="store_true",
                   help="Also produce 9:16 vertical clips alongside the primary output")
    p.add_argument("--content-ratio", type=float, default=CONTENT_RATIO_DEFAULT,
                   help="Vertical layout: top content portion (default 0.60)")
    p.add_argument("--cam-region", default=None,
                   help="Manual camera region override as x,y,w,h")
    p.add_argument("--content-region", default=None,
                   help="Manual content region override as x,y,w,h")

    # Speech subtitles
    p.add_argument("--speech-subtitles", action="store_true")
    p.add_argument("--subtitle-sidecar-only", action="store_true")
    p.add_argument("--subtitle-model", default="tiny")
    p.add_argument("--subtitle-language", default=None)
    p.add_argument("--subtitle-device", default="cpu", choices=["cpu", "cuda", "auto"])
    p.add_argument("--subtitle-compute-type", default="int8")
    p.add_argument("--subtitle-beam-size", type=int, default=1)
    p.add_argument("--subtitle-vad", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--subtitle-style",
                   default=(
                       "Fontname=Arial,Fontsize=28,PrimaryColour=&H00FFFFFF,"
                       "OutlineColour=&HCC000000,BorderStyle=1,Outline=3,Shadow=1,"
                       "Alignment=2,MarginV=64"
                   ))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    _print_banner()

    raw_mode = bool(getattr(args, "raw", False))
    if raw_mode:
        _print_phase("RAW MODE -- clean cuts, no effects")
    else:
        _print_phase("CODEX MODE -- siphoning engagement")

    try:
        result = export_codex_clips(args)
    except Exception as e:
        _spinner.stop()
        print(f"\n  {_c(_CRIMSON, '[error]')} {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    for w in result["warnings"]:
        print(f"  {_c(_EMBER, '[warning]')} {w}", file=sys.stderr)

    # Print clip cards
    for i, clip in enumerate(result["clips"], 1):
        _print_clip_card(i, clip, raw_mode)

    _print_summary(result, raw_mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
