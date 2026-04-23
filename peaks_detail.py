"""Per-peak chat detail extractor for clip planning.

Consumes the chat JSON and peaks.csv produced by fetch_chat.py + heatmap.py,
then emits a Markdown report with, for each top peak:
  - Timestamp (HH:MM:SS), bucket message count, segment/chapter label.
  - Top N word/emote tokens in the peak window.
  - Sampled messages spread across the window (for quick skim).

This is the "what was on screen" record — paired with heatmap.png it's enough
to jump straight into your editor and cut a clip without rewatching the VOD.

Usage:
    python peaks_detail.py chat_2754474282.json --peaks peaks.csv --out peaks_detail.md
    python peaks_detail.py chat_2754474282.json --peaks peaks.csv --window 60 --samples 25 --tokens 25
"""

import argparse
import csv
import json
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path


def hms(seconds):
    return str(timedelta(seconds=int(max(0, seconds))))


def tokens_in(messages, min_len=3):
    pat = re.compile(r"[A-Za-z][A-Za-z0-9_]{%d,}" % (min_len - 1))
    c = Counter()
    for body in messages:
        for w in pat.findall(body):
            c[w] += 1
    return c


def load_chat(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["comments"] if isinstance(data, dict) else data


def load_peaks(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "rank": int(r["rank"]),
                "timestamp": r["timestamp"],
                "seconds": int(r["seconds"]),
                "count": int(r["count"]),
                "segment": r.get("segment", ""),
            })
    return rows


def window_messages(comments, start, end):
    return [c for c in comments if start <= c["content_offset_seconds"] < end]


def main():
    p = argparse.ArgumentParser(description="Per-peak chat detail report")
    p.add_argument("chat_json")
    p.add_argument("--peaks", default="peaks.csv")
    p.add_argument("--window", type=int, default=60,
                   help="Window size in seconds around each peak start (default 60)")
    p.add_argument("--tokens", type=int, default=25, help="Top-N tokens to show")
    p.add_argument("--samples", type=int, default=25, help="Number of sampled messages")
    p.add_argument("--out", default="peaks_detail.md")
    args = p.parse_args()

    comments = load_chat(args.chat_json)
    peaks = load_peaks(args.peaks)

    lines = []
    lines.append(f"# Peak Moments — {Path(args.chat_json).name}")
    lines.append("")
    lines.append(f"Source chat: **{len(comments):,} messages**  ")
    lines.append(f"Peaks file: `{args.peaks}` ({len(peaks)} entries)  ")
    lines.append(f"Window per peak: {args.window}s")
    lines.append("")
    lines.append("Each entry below shows the chat signal for a single high-density "
                 "minute — top tokens/emotes and a timeline of sample messages. Use "
                 "this to decide which peaks are worth clipping without watching the VOD.")
    lines.append("")

    for pk in peaks:
        start = pk["seconds"]
        end = start + args.window
        window = window_messages(comments, start, end)
        bodies = [c["message"]["body"] for c in window]
        tokens = tokens_in(bodies).most_common(args.tokens)

        lines.append("---")
        lines.append("")
        lines.append(f"## Peak #{pk['rank']} — {pk['timestamp']} "
                     f"({pk['count']} msg/bin) — {pk['segment']}")
        lines.append("")
        lines.append(f"- Window: **{hms(start)} → {hms(end)}** "
                     f"({len(window)} messages)")
        lines.append("")

        if tokens:
            lines.append("**Top tokens**")
            lines.append("")
            chunks = [f"`{w}` ×{n}" for w, n in tokens]
            lines.append(" · ".join(chunks))
            lines.append("")

        if window:
            step = max(1, len(window) // args.samples)
            lines.append("**Sample messages across the window**")
            lines.append("")
            lines.append("| +mm:ss | user | message |")
            lines.append("|---|---|---|")
            for c in window[::step][: args.samples]:
                rel = int(c["content_offset_seconds"]) - start
                mm, ss = divmod(rel, 60)
                user = (c["commenter"].get("display_name") or "").replace("|", "/")
                body = c["message"]["body"].replace("|", "/").replace("\n", " ")
                body = body[:140]
                lines.append(f"| +{mm:02d}:{ss:02d} | {user} | {body} |")
            lines.append("")

    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote report -> {args.out} ({len(peaks)} peaks)")


if __name__ == "__main__":
    main()
