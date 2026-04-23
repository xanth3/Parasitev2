"""Twitch chat heatmap generator.

Consumes a downloaded Twitch VOD chat JSON (TwitchDownloader, yt-dlp rechat,
or raw Twitch v5 /comments) and produces:

    1. A PNG histogram of messages-per-bucket across the stream duration.
    2. A terminal table of the top-N peak buckets with HH:MM:SS timestamps.
    3. A CSV of those peaks (rank, timestamp, seconds, count, segment).

Pass an optional yt-dlp .info.json to label segments using the VOD's chapters.

Usage:
    python heatmap.py chat.json
    python heatmap.py chat.json --info vod.info.json --bin 60 --top 10
    python heatmap.py chat.json --bin 30 --min-msgs 20 --out hype.png
"""

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter


def extract_offset(comment):
    if "content_offset_seconds" in comment:
        return float(comment["content_offset_seconds"])
    if "contentOffsetSeconds" in comment:
        return float(comment["contentOffsetSeconds"])
    return None


def extract_body(comment):
    msg = comment.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        body = msg.get("body")
        if isinstance(body, str):
            return body
        frags = msg.get("fragments")
        if isinstance(frags, list):
            return "".join(f.get("text", "") for f in frags if isinstance(f, dict))
    return ""


def load_comments(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("comments"), list):
        return data["comments"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "messages"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    raise ValueError(f"Could not find a comments array in {path}")


def load_chapters(info_path):
    if not info_path:
        return []
    data = json.loads(Path(info_path).read_text(encoding="utf-8"))
    chapters = data.get("chapters") or []
    out = []
    for c in chapters:
        try:
            out.append({
                "start": float(c.get("start_time", 0)),
                "end": float(c.get("end_time", 0)),
                "title": c.get("title", ""),
            })
        except (TypeError, ValueError):
            continue
    return out


def chapter_for(offset, chapters):
    for c in chapters:
        if c["start"] <= offset < c["end"]:
            return c["title"]
    return ""


def hms(seconds):
    s = max(0, int(seconds))
    return str(timedelta(seconds=s))


def main():
    p = argparse.ArgumentParser(description="Twitch chat heatmap generator")
    p.add_argument("chat_json", help="Path to downloaded chat JSON")
    p.add_argument("--info", help="Optional yt-dlp .info.json for chapter labels")
    p.add_argument("--bin", dest="bin_size", type=int, default=60,
                   help="Bucket size in seconds (default 60)")
    p.add_argument("--top", type=int, default=10, help="Top-N peaks to report (default 10)")
    p.add_argument("--min-msgs", type=int, default=0,
                   help="Ignore buckets below this message count when ranking peaks")
    p.add_argument("--out", default="heatmap.png", help="Output PNG path")
    p.add_argument("--csv", default="peaks.csv", help="Output CSV path for the peaks table")
    args = p.parse_args()

    if args.bin_size <= 0:
        print("--bin must be > 0", file=sys.stderr)
        sys.exit(2)

    comments = load_comments(args.chat_json)
    offsets = [o for o in (extract_offset(c) for c in comments) if o is not None]
    if not offsets:
        print("No timestamps found in chat JSON.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(offsets):,} comments from {args.chat_json}")

    chapters = load_chapters(args.info)
    if chapters:
        print(f"Loaded {len(chapters)} chapter(s) from {args.info}")

    bin_size = args.bin_size
    buckets = Counter(int(o // bin_size) for o in offsets)
    max_bucket = max(buckets)
    xs_seconds = [b * bin_size for b in range(max_bucket + 1)]
    ys = [buckets.get(b, 0) for b in range(max_bucket + 1)]
    peak_y = max(ys) if ys else 0

    fig, ax = plt.subplots(figsize=(16, 5))
    ax.bar(xs_seconds, ys, width=bin_size, color="purple",
           edgecolor="black", alpha=0.75, align="edge")
    ax.set_title(f"Chat activity — {Path(args.chat_json).name} ({bin_size}s buckets)")
    ax.set_xlabel("Time into stream (HH:MM:SS)")
    ax.set_ylabel(f"Messages per {bin_size}s")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.set_xlim(0, (max_bucket + 1) * bin_size)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: hms(x)))
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

    for c in chapters:
        ax.axvline(c["start"], color="orange", linestyle="--", alpha=0.6, linewidth=1)
        ax.text(c["start"], peak_y, f" {c['title']}", rotation=90,
                va="top", ha="left", fontsize=8, color="darkorange")

    plt.tight_layout()
    plt.savefig(args.out, dpi=120)
    print(f"Saved heatmap -> {args.out}")

    ranked = sorted(
        ((b, n) for b, n in buckets.items() if n >= args.min_msgs),
        key=lambda x: x[1],
        reverse=True,
    )[: args.top]

    print(f"\n--- TOP {len(ranked)} PEAKS ---")
    print(f"{'Rank':<5} {'Timestamp':<10} {'Msg/bin':<9} {'Segment'}")
    print("-" * 70)
    rows = []
    for rank, (bucket, count) in enumerate(ranked, 1):
        t = bucket * bin_size
        seg = chapter_for(t, chapters)
        ts = hms(t)
        print(f"#{rank:<4} {ts:<10} {count:<9} {seg}")
        rows.append({"rank": rank, "timestamp": ts, "seconds": t,
                     "count": count, "segment": seg})

    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "timestamp", "seconds", "count", "segment"])
        w.writeheader()
        w.writerows(rows)
    print(f"Saved peaks -> {args.csv}")


if __name__ == "__main__":
    main()
