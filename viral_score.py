"""Siphon virality engine - scores Twitch VOD peaks on a 0-100 Virality Index.

Four-pillar algorithm:
  Velocity (37.5%): log2-scaled message-rate spike vs rolling 5-min baseline
  Mood     (37.5%): dominant emote category weighted by confidence
  Echo     (25%):   2-minute post-peak retention -> Story / Lingering / Jump Scare
  Magnitude (x):    peak volume vs VOD p99 as a 0.5-1.0 multiplicative scaler
  Heatmap: diagnostic only; not part of the virality formula

Smart Padding: each peak gets a dynamic cut window at 1-second resolution -
starts 5s before the velocity ramp, ends when the echo tail falls silent for 10s.
Adjacent windows within 15s are merged into a single clip (max 300s).

Usage:
    python viral_score.py archive/<dir>/chat.json \\
        --peaks archive/<dir>/peaks.csv \\
        --info  archive/<dir>/info.json \\
        --csv   archive/<dir>/viral_score.csv \\
        --md    archive/<dir>/siphon_report.md
"""

import argparse
import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

# Windows cp1252 safety - must come before any Unicode output
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from heatmap import (
    chapter_for,
    extract_body,
    extract_offset,
    hms,
    load_chapters,
    load_comments,
)
from peaks_detail import load_peaks


# ---------------------------------------------------------------------------
# Mood lexicon - whitespace-split matching so short tokens (W, L, +2) survive
# ---------------------------------------------------------------------------

BASE_MOOD_LEXICON = {
    "HYPE":  {"POG", "POGGERS", "POGCHAMP", "HYPE", "LETSGO", "LETSGOO", "GIGACHAD",
              "CLEAN", "INSANE", "W", "WW", "+2", "GG", "GOAT", "MENACE",
              "BASED", "ACTUAL", "PAGMAN", "BIG"},
    "FUNNY": {"LUL", "LULW", "OMEGALUL", "KEKW", "LMAO", "ICANT", "XD", "DEAD",
               "CLUELESS", "OMEGADANCE", "HAHA", "LMFAO",
              "4HEAD", "JEBAITED"},
    "SHOCK": {"WTF", "OMG", "HUH", "BRUH", "WIDEPEEPO", "NOWAY", "???", "WHAT",
              "HOLY", "NOSHOT", "NAHHH", "NAH", "BRO", "WAIT", "MONKAW",
              "MONKAS", "CRASHOUT"},
    "DRAMA": {"L", "RATIO", "CRINGE", "SADGE", "YIKES", "FELLOFF", "NOOO", "COPE",
              "MALD", "TOUCH", "GRASS", "BOZO", "COOKED", "COPIUM", "REAL"},
}

STREAMER_MOOD_LEXICONS = {
    "zackrawrr": {
        "HYPE": {"CINEMA", "AURA", "COOKING"},
        "FUNNY": {"BALD", "BALDY", "BALDIMORT", "SAMESHIRT", "SAME", "SHIRT"},
        "DRAMA": {"FARMING", "LOOKING"},
    },
    "asmongold": {
        "HYPE": {"CINEMA", "AURA", "COOKING"},
        "FUNNY": {"BALD", "BALDY", "BALDIMORT", "SAMESHIRT", "SAME", "SHIRT"},
        "DRAMA": {"FARMING", "LOOKING"},
    },
    "caseoh": {
        "HYPE": {
            "CASEOHLIFE", "CASEOHSPOOKYDAILYDOODLES", "CASEOHDAILYDOODLESTWERK",
            "CASEOHDAILYDOODLEDANCE", "CASEOHKITTYYY", "CASEOHGOOBLIFE",
            "CASEOHLOCKINCASEOH", "CASEOHWIIJAMS", "CASEOHPINK", "CASEOHTHUMBSUP",
            "TWITCHCONHYPE", "DINODANCE", "XWIIZEPRAY", "GOTY", "GG", "WWW",
            "WWWW", "WWWWW", "YOOOO", "YOOOOO", "GOATEMOTEY", "CASEOHALPHA",
        },
        "FUNNY": {
            "CASEOHBBL", "BBL", "CASEOHNOODLES", "CASEOHDOOKIESTAIN",
            "CASEOHCOMEONCUH", "CASEOHBTVKITTY", "CASEOHBYRANGEEDESIGNS",
            "JINXLUL", "ARCHIT3LUL", "LMAOO", "LMAOOO", "LMAOOOO", "LOLLL",
            "BABYRAGE", "STINKYCHEESE", "BULLY", "FEET", "TOES", "KITTY",
            "CASEOHPETKITTY", "CASEOHKITTYYY",
        },
        "SHOCK": {
            "NOTLIKETHIS", "OMG", "NO", "NAH", "BRUH", "BACKROOMS", "DOOR",
            "RAVEN", "RED", "EYE", "LOST", "SPOOKY", "CASEOHBANNED",
        },
        "DRAMA": {
            "PURGE", "MODS", "CAP", "REAL", "STOP", "LOCK", "LBRE", "DAD",
            "KID", "KIDS", "ARKANSAS",
        },
    },
}

MOOD_LEXICON = BASE_MOOD_LEXICON

BREAK_MARKERS = {"ASSEMBLE", "SCATTER"}

PILLAR_WEIGHTS = {"velocity": 0.375, "mood": 0.375, "echo": 0.25}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _nonzero(values: list[int]) -> list[int]:
    return [v for v in values if v > 0]


def bin_by_minute(comments: list[dict]) -> list[int]:
    """Dense array: result[m] = message count in minute m of the VOD."""
    counts: dict[int, int] = {}
    for c in comments:
        off = extract_offset(c)
        if off is None:
            continue
        m = int(off // 60)
        counts[m] = counts.get(m, 0) + 1
    if not counts:
        return []
    n = max(counts) + 1
    return [counts.get(m, 0) for m in range(n)]


def bin_by_second(comments: list[dict], start: int, end: int) -> list[int]:
    """Dense array of length (end-start): result[i] = messages in second (start+i)."""
    size = max(0, end - start)
    counts = [0] * size
    for c in comments:
        off = extract_offset(c)
        if off is None:
            continue
        idx = int(off) - start
        if 0 <= idx < size:
            counts[idx] += 1
    return counts


def percentile(values: list[int], p: float = 0.99) -> float:
    """Linear-interpolation percentile over non-zero values. Returns 1.0 if empty."""
    nz = sorted(_nonzero(values))
    n = len(nz)
    if n == 0:
        return 1.0
    if n == 1:
        return float(nz[0])
    k = p * (n - 1)
    lo = int(k)
    frac = k - lo
    if lo + 1 >= n:
        return float(nz[lo])
    return nz[lo] + frac * (nz[lo + 1] - nz[lo])


def compute_baseline(minute_counts: list[int], m: int, global_floor: float) -> float:
    """Mean of the 5 non-zero minutes before m, never below global_floor."""
    window = _nonzero(minute_counts[max(0, m - 5): m])
    raw = mean(window) if window else global_floor
    return max(raw, global_floor)


def percentile_rank(values: list[int], value: int) -> float:
    """Return the percentile rank of value among non-zero heatmap buckets."""
    nz = _nonzero(values)
    if not nz:
        return 0.0
    below_or_equal = sum(1 for v in nz if v <= value)
    return 100.0 * below_or_equal / len(nz)


def local_prominence(minute_counts: list[int], m: int) -> float:
    """How strongly this heatmap bucket stands above its nearby context."""
    context = (
        minute_counts[max(0, m - 5):m]
        + minute_counts[m + 1:min(len(minute_counts), m + 6)]
    )
    nz = _nonzero(context)
    local_floor = mean(nz) if nz else 1.0
    return minute_counts[m] / max(1.0, local_floor)


def _merge_lexicons(*lexicons: dict[str, set[str]]) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {k: set(v) for k, v in BASE_MOOD_LEXICON.items()}
    for lexicon in lexicons:
        for bucket, tokens in lexicon.items():
            merged.setdefault(bucket, set()).update(t.upper() for t in tokens)
    return merged


def _token_to_bucket(lexicon: dict[str, set[str]]) -> dict[str, str]:
    return {
        tok: bucket
        for bucket, tokens in lexicon.items()
        for tok in tokens
    }


def normalize_streamer_key(name: str | None) -> str:
    key = (name or "").lower().strip()
    key = key.replace("-", "_")
    key = key.rstrip("_")
    if key in {"caseoh_", "caseoh"}:
        return "caseoh"
    if key in {"zackrawrr", "zack_rawrr", "asmongold"}:
        return key
    return key


def streamer_from_info(info_path: str | Path | None) -> str | None:
    if not info_path:
        return None
    try:
        data = json.loads(Path(info_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return (
        data.get("uploader_id")
        or data.get("uploader")
        or data.get("channel")
        or data.get("creator")
    )


def lexicon_for_streamer(streamer: str | None) -> tuple[dict[str, set[str]], str]:
    key = normalize_streamer_key(streamer)
    return _merge_lexicons(STREAMER_MOOD_LEXICONS.get(key, {})), key or "base"


# ---------------------------------------------------------------------------
# Mood scoring - dedicated whitespace-split tokenizer (not tokens_in)
# ---------------------------------------------------------------------------

def classify_mood(
    window_bodies: list[str],
    mood_lexicon: dict[str, set[str]] | None = None,
) -> tuple[str, float, int, float]:
    """
    Returns (dominant_mood, concentration, total_mood_tokens, mood_score).
    Applies confidence shrinkage so sparse windows can't score 100.
    """
    lexicon = mood_lexicon or BASE_MOOD_LEXICON
    token_to_bucket = _token_to_bucket(lexicon)
    bucket_counts: dict[str, int] = {k: 0 for k in lexicon}
    total = 0
    for body in window_bodies:
        for raw in body.split():
            tok = raw.upper().rstrip("!?,.")
            bucket = token_to_bucket.get(tok)
            if bucket:
                bucket_counts[bucket] += 1
                total += 1

    floor = max(10, int(0.03 * len(window_bodies)))
    if total < floor or max(bucket_counts.values()) == 0:
        return ("mixed", 0.0, total, 0.0)

    dominant = max(bucket_counts, key=bucket_counts.get)
    concentration = bucket_counts[dominant] / total
    # Shrinkage: a peak with only 5 mood tokens can't score 100 even if unanimous
    mood_score = min(100.0, 200 * concentration * min(1.0, total / 20))
    return (dominant, concentration, total, mood_score)


def has_break_marker(window_bodies: list[str]) -> bool:
    """True when a window is bathroom/break noise, not a clip signal."""
    for body in window_bodies:
        tokens = {raw.upper().rstrip("!?,.") for raw in body.split()}
        if tokens & BREAK_MARKERS:
            return True
    return False


# ---------------------------------------------------------------------------
# Smart padding
# ---------------------------------------------------------------------------

def smart_padding(
    peak_seconds: int,
    comments: list[dict],
    baseline_per_sec: float,
) -> tuple[int, int]:
    """
    Walk at 1-second resolution around peak_seconds.
    Start: 5 quiet seconds found going backwards -> use that position as lead-in.
    End  : 10 consecutive quiet seconds going forward -> cut there.
    Duration clamped [30, 180]s.
    """
    look_back = 120
    look_ahead = 180
    look_start = max(0, peak_seconds - look_back)
    look_end = peak_seconds + look_ahead

    sec_counts = bin_by_second(comments, look_start, look_end)
    size = len(sec_counts)
    if size == 0:
        return (max(0, peak_seconds - 15), peak_seconds + 60)

    threshold = max(0.5, baseline_per_sec)
    peak_idx = min(peak_seconds - look_start, size - 1)

    # Walk backwards: find 5 consecutive quiet seconds (velocity ramp-up point)
    clip_start_idx = 0
    consec = 0
    for i in range(peak_idx, -1, -1):
        if sec_counts[i] <= threshold:
            consec += 1
            if consec >= 5:
                # i is the leftmost of the 5 quiet seconds - use it as natural start
                clip_start_idx = i
                break
        else:
            consec = 0

    # Walk forwards: find 10 consecutive quiet seconds (echo tail dies)
    clip_end_idx = size - 1
    consec = 0
    for i in range(peak_idx, size):
        if sec_counts[i] <= threshold:
            consec += 1
            if consec >= 10:
                clip_end_idx = max(0, i - consec + 1)
                break
        else:
            consec = 0

    cut_start = max(0, look_start + clip_start_idx)
    cut_end = look_start + clip_end_idx

    duration = cut_end - cut_start
    if duration < 30:
        cut_end = cut_start + 30
    elif duration > 180:
        cut_end = cut_start + 180

    return (cut_start, cut_end)


# ---------------------------------------------------------------------------
# Merge overlapping windows
# ---------------------------------------------------------------------------

def merge_overlapping_windows(scored_peaks: list[dict]) -> list[dict]:
    """
    Fuse windows that are <=15s apart (max merged duration 300s).
    The higher-virality peak's metadata wins for merged entries.
    """
    if not scored_peaks:
        return []

    by_start = sorted(scored_peaks, key=lambda p: p["cut_start"])
    merged: list[dict] = []
    cur = dict(by_start[0])
    cur["merged_peaks"] = list(cur["merged_peaks"])

    for nxt in by_start[1:]:
        gap = nxt["cut_start"] - cur["cut_end"]
        span = nxt["cut_end"] - cur["cut_start"]
        if gap <= 15 and span <= 300:
            cur["cut_end"] = max(cur["cut_end"], nxt["cut_end"])
            cur["cut_window"] = f"{hms(cur['cut_start'])} -> {hms(cur['cut_end'])}"
            cur["merged_peaks"] = cur["merged_peaks"] + nxt["merged_peaks"]
            if nxt["virality"] > cur["virality"]:
                for k in ("virality", "mood", "mood_concentration", "velocity_multiplier",
                          "echo_ratio", "echo_label", "magnitude_factor", "heatmap_count",
                          "heatmap_percentile", "heatmap_prominence", "heatmap_score", "reasoning",
                          "pillars", "segment", "timestamp", "seconds", "count"):
                    cur[k] = nxt[k]
        else:
            merged.append(cur)
            cur = dict(nxt)
            cur["merged_peaks"] = list(cur["merged_peaks"])
    merged.append(cur)

    merged.sort(key=lambda p: p["virality"], reverse=True)
    for i, p in enumerate(merged):
        p["rank"] = i + 1
    return merged


# ---------------------------------------------------------------------------
# Reasoning string
# ---------------------------------------------------------------------------

def _reasoning(
    multiplier: float,
    mood: str,
    concentration: float,
    echo_ratio: float,
    magnitude_factor: float,
    heatmap_percentile: float,
    heatmap_prominence: float,
) -> str:
    parts = [f"{multiplier:.1f}x velocity"]
    parts.append(f"p{int(round(heatmap_percentile))} heatmap")
    parts.append(f"{heatmap_prominence:.1f}x local heat")
    if mood != "mixed":
        parts.append(f"{mood} ({concentration:.2f})")
    else:
        parts.append("mixed mood")
    if echo_ratio > 0.6:
        parts.append(f"Story echo {echo_ratio:.2f}")
    elif echo_ratio > 0.3:
        parts.append(f"Lingering echo {echo_ratio:.2f}")
    else:
        parts.append(f"Jump Scare echo {echo_ratio:.2f}")
    parts.append(f"p{int(magnitude_factor * 100)} volume")
    return " - ".join(parts)


# ---------------------------------------------------------------------------
# Per-peak scorer
# ---------------------------------------------------------------------------

_SKIP_SECONDS = 300  # ignore peaks in the first 5 min (noisy baseline)


def score_peak(
    peak: dict,
    minute_counts: list[int],
    comments: list[dict],
    *,
    global_median: float,
    p99: float,
    chapters: list[dict],
    mood_lexicon: dict[str, set[str]] | None = None,
    lexicon_name: str = "base",
) -> dict:
    m = peak["seconds"] // 60
    c_m = minute_counts[m] if m < len(minute_counts) else 0

    global_floor = max(1.0, 0.3 * global_median)
    baseline = compute_baseline(minute_counts, m, global_floor)

    # Velocity - log2 scale: 2x -> 23, 10x -> 77, 20x -> 100
    multiplier = c_m / baseline
    velocity_score = min(100.0, max(0.0,
        100 * math.log2(max(1.0, multiplier)) / math.log2(20)
    ))

    # Mood - whitespace tokenizer on 60s window
    ws, we = peak["seconds"], peak["seconds"] + 60
    win_bodies = [
        extract_body(c)
        for c in comments
        if (off := extract_offset(c)) is not None and ws <= off < we
    ]
    if has_break_marker(win_bodies):
        return {}
    mood, concentration, mood_tok_total, mood_score = classify_mood(win_bodies, mood_lexicon)

    # Echo - 2-minute retention after peak
    m1 = minute_counts[m + 1] if m + 1 < len(minute_counts) else 0
    m2 = minute_counts[m + 2] if m + 2 < len(minute_counts) else 0
    n_echo = (1 if m + 1 < len(minute_counts) else 0) + (1 if m + 2 < len(minute_counts) else 0)
    echo_mean = (m1 + m2) / n_echo if n_echo > 0 else 0.0
    echo_ratio = echo_mean / max(1, c_m)
    echo_score = min(100.0, 150 * echo_ratio)
    echo_label = (
        "Story" if echo_ratio > 0.6
        else "Lingering" if echo_ratio > 0.3
        else "Jump Scare"
    )

    # Magnitude - absolute reach scaler (0.5..1.0)
    magnitude_factor = min(1.0, c_m / max(1.0, p99))
    heatmap_percentile = percentile_rank(minute_counts, c_m)
    heatmap_prominence = local_prominence(minute_counts, m)
    heatmap_power = min(1.0, c_m / max(1.0, p99))
    prominence_score = min(100.0, max(0.0,
        100 * math.log2(max(1.0, heatmap_prominence)) / math.log2(10)
    ))
    heatmap_score = (
        0.55 * heatmap_percentile
        + 0.30 * (heatmap_power * 100)
        + 0.15 * prominence_score
    )

    # Final virality
    raw = (PILLAR_WEIGHTS["velocity"] * velocity_score
           + PILLAR_WEIGHTS["mood"] * mood_score
           + PILLAR_WEIGHTS["echo"] * echo_score)
    virality = max(0, min(100, int(round(raw * (0.5 + 0.5 * magnitude_factor)))))

    # Smart padding
    cut_start, cut_end = smart_padding(peak["seconds"], comments, baseline / 60)

    # Chapter segment
    segment = peak.get("segment") or chapter_for(peak["seconds"], chapters)

    return {
        "rank":               peak["rank"],
        "timestamp":          peak["timestamp"],
        "seconds":            peak["seconds"],
        "count":              c_m,
        "segment":            segment,
        "virality":           virality,
        "mood":               mood,
        "mood_lexicon":       lexicon_name,
        "mood_concentration": round(concentration, 3),
        "velocity_multiplier": round(multiplier, 2),
        "heatmap_count":      c_m,
        "heatmap_percentile": round(heatmap_percentile, 1),
        "heatmap_prominence": round(heatmap_prominence, 2),
        "heatmap_score":      int(round(heatmap_score)),
        "echo_ratio":         round(echo_ratio, 3),
        "echo_label":         echo_label,
        "magnitude_factor":   round(magnitude_factor, 3),
        "cut_start":          cut_start,
        "cut_end":            cut_end,
        "cut_window":         f"{hms(cut_start)} -> {hms(cut_end)}",
        "reasoning":          _reasoning(
            multiplier,
            mood,
            concentration,
            echo_ratio,
            magnitude_factor,
            heatmap_percentile,
            heatmap_prominence,
        ),
        "pillars":            {
            "velocity": int(round(velocity_score)),
            "mood":     int(round(mood_score)),
            "echo":     int(round(echo_score)),
            "heatmap":  int(round(heatmap_score)),
        },
        "merged_peaks":       [peak["rank"]],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_peaks(
    comments: list[dict],
    peaks: list[dict],
    chapters: list[dict] | None = None,
    streamer: str | None = None,
) -> list[dict]:
    """Score peaks and return list sorted by virality desc, windows merged."""
    minute_counts = bin_by_minute(comments)
    if not minute_counts:
        return []
    mood_lexicon, lexicon_name = lexicon_for_streamer(streamer)

    nz = _nonzero(minute_counts)
    global_median = median(nz) if nz else 1.0
    p99 = percentile(minute_counts)

    eligible = [pk for pk in peaks if pk["seconds"] >= _SKIP_SECONDS] or peaks
    scored = [
        scored
        for pk in eligible
        if (scored := score_peak(pk, minute_counts, comments,
                                 global_median=global_median, p99=p99,
                                 chapters=chapters or [],
                                 mood_lexicon=mood_lexicon,
                                 lexicon_name=lexicon_name))
    ]
    return merge_overlapping_windows(scored)


def score_from_paths(
    chat_path: str | Path,
    peaks_path: str | Path,
    info_path: str | Path | None = None,
) -> list[dict]:
    """Path-level convenience wrapper used by symbiote.py."""
    comments = load_comments(str(chat_path))
    peaks = load_peaks(str(peaks_path))
    chapters = load_chapters(str(info_path)) if info_path else []
    return score_peaks(comments, peaks, chapters, streamer_from_info(info_path))


# ---------------------------------------------------------------------------
# CSV / Markdown output
# ---------------------------------------------------------------------------

def write_csv(scored: list[dict], out_path: str | Path) -> None:
    fields = ["rank", "virality", "timestamp", "seconds", "cut_start", "cut_end", "cut_window", "mood",
              "mood_lexicon",
              "echo_label", "velocity_x", "heatmap_score", "heatmap_count", "heatmap_percentile",
              "heatmap_prominence", "magnitude", "segment", "reasoning"]
    rows = [{
        "rank":       p["rank"],
        "virality":   p["virality"],
        "timestamp":  p["timestamp"],
        "seconds":    p["seconds"],
        "cut_start":  p["cut_start"],
        "cut_end":    p["cut_end"],
        "cut_window": p["cut_window"],
        "mood":       p["mood"],
        "mood_lexicon": p.get("mood_lexicon", "base"),
        "echo_label": p["echo_label"],
        "velocity_x": f"{p['velocity_multiplier']:.1f}x",
        "heatmap_score": p["heatmap_score"],
        "heatmap_count": p["heatmap_count"],
        "heatmap_percentile": f"{p['heatmap_percentile']:.1f}",
        "heatmap_prominence": f"{p['heatmap_prominence']:.2f}",
        "magnitude":  f"{p['magnitude_factor']:.2f}",
        "segment":    p["segment"],
        "reasoning":  p["reasoning"],
    } for p in scored]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_md(
    scored: list[dict],
    comments: list[dict],
    out_path: str | Path,
    chat_name: str = "",
) -> None:
    lines = [
        f"# Siphon Report - {chat_name}",
        "",
        f"Scored **{len(scored)}** clip(s)  ",
        "Algorithm: Velocity (30%) - Mood (30%) - Echo (20%) - Heatmap (20%)",
        "",
    ]
    for p in scored:
        merged_note = (
            f" *(merged {', '.join('#' + str(r) for r in p['merged_peaks'])})*"
            if len(p["merged_peaks"]) > 1 else ""
        )
        lines += [
            "---",
            "",
            f"## #{p['rank']} - {p['virality']}/100 - {p['timestamp']} - {p['mood']}{merged_note}",
            "",
            f"**Cut window:** `{p['cut_window']}`  ",
            f"**Mood lexicon:** {p.get('mood_lexicon', 'base')}  ",
            f"**Reasoning:** {p['reasoning']}  ",
            f"**Segment:** {p['segment'] or '-'}  ",
            "",
            "| Metric | Score |",
            "|---|---|",
            f"| Velocity {p['velocity_multiplier']:.1f}x | {p['pillars']['velocity']} |",
            f"| Heatmap p{p['heatmap_percentile']:.0f} / {p['heatmap_prominence']:.1f}x local | {p['pillars']['heatmap']} |",
            f"| Mood ({p['mood']}) | {p['pillars']['mood']} |",
            f"| Echo ({p['echo_label']}) | {p['pillars']['echo']} |",
            f"| Magnitude factor | {p['magnitude_factor']:.2f} |",
            "",
        ]

        # Mood tokens from the padded cut window
        win = [c for c in comments
               if (off := extract_offset(c)) is not None
               and p["cut_start"] <= off < p["cut_end"]]
        mood_hits: Counter[str] = Counter()
        active_token_to_bucket = _token_to_bucket(
            _merge_lexicons(STREAMER_MOOD_LEXICONS.get(p.get("mood_lexicon", ""), {}))
        )
        for c in win:
            for raw in extract_body(c).split():
                tok = raw.upper().rstrip("!?,.")
                if active_token_to_bucket.get(tok) == p["mood"]:
                    mood_hits[tok] += 1
        if mood_hits:
            lines += [
                "**Top mood tokens**", "",
                " - ".join(f"`{t}` x{n}" for t, n in mood_hits.most_common(12)),
                "",
            ]

        # Sample messages from padded window
        if win:
            samples = win[::max(1, len(win) // 10)][:10]
            lines += ["**Sample messages**", "", "| +mm:ss | user | message |", "|---|---|---|"]
            for c in samples:
                off = extract_offset(c) or 0
                rel = max(0, int(off) - p["cut_start"])
                mm, ss = divmod(rel, 60)
                user = (c.get("commenter") or {}).get("display_name", "")
                body = extract_body(c).replace("|", "/").replace("\n", " ")[:140]
                lines.append(f"| +{mm:02d}:{ss:02d} | {user} | {body} |")
            lines.append("")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Siphon - Twitch VOD virality scorer")
    ap.add_argument("chat_json", help="Path to chat JSON (archive/chat.json or legacy)")
    ap.add_argument("--peaks",   required=True, help="peaks.csv from heatmap.py")
    ap.add_argument("--info",    default=None,  help="Optional yt-dlp info.json for chapters")
    ap.add_argument("--csv",     default=None,  help="Output viral_score.csv path")
    ap.add_argument("--md",      default=None,  help="Output siphon_report.md path")
    ap.add_argument("--top",     type=int, default=20, help="Peaks to score (default 20)")
    args = ap.parse_args()

    chat_p = Path(args.chat_json)
    out_dir = chat_p.parent
    csv_out = args.csv or str(out_dir / "viral_score.csv")
    md_out  = args.md  or str(out_dir / "siphon_report.md")

    print(f"Loading {chat_p.name}...", flush=True)
    comments = load_comments(str(chat_p))
    print(f"  {len(comments):,} messages")

    peaks = load_peaks(args.peaks)[: args.top]
    print(f"  {len(peaks)} peaks from {args.peaks}")

    chapters: list[dict] = []
    if args.info:
        chapters = load_chapters(args.info)
        print(f"  {len(chapters)} chapter(s)")

    print("Scoring...", flush=True)
    scored = score_peaks(comments, peaks, chapters, streamer_from_info(args.info))

    write_csv(scored, csv_out)
    print(f"-> {csv_out}")
    write_md(scored, comments, md_out, chat_p.name)
    print(f"-> {md_out}")

    print(f"\n{'#':<4} {'Score':<7} {'Timestamp':<11} {'Mood':<9} {'Echo':<13} {'Velocity':<9} {'Heatmap':<8} Window")
    print("-" * 96)
    for s in scored:
        tag = f" (+{len(s['merged_peaks'])-1})" if len(s["merged_peaks"]) > 1 else ""
        print(f"#{s['rank']:<3} {s['virality']:<7} {s['timestamp']:<11} "
              f"{s['mood']:<9} {s['echo_label']:<13} {s['velocity_multiplier']:<9.1f}"
              f"{s['heatmap_score']:<8}"
              f"{s['cut_window']}{tag}")


if __name__ == "__main__":
    main()
