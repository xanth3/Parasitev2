"""Twitch VOD chat fetcher (GraphQL, offset pagination).

yt-dlp's rechat extractor uses Twitch's deprecated v5 /comments endpoint which
404s on current VODs. This script calls the same public GraphQL operation the
Twitch web player uses (VideoCommentsByOffsetOrCursor) with offset-based
pagination, paginates the full VOD, and writes a TwitchDownloader-compatible
chat.json that heatmap.py can consume unchanged.

Offset pagination is used instead of cursor because cursor requires a valid
Client-Integrity token, which Twitch gates behind a JS challenge we don't
solve. Offset queries aren't integrity-checked and return up to ~100 messages
at or after the requested second.

Usage:
    python fetch_chat.py https://www.twitch.tv/videos/2754474282
    python fetch_chat.py 2754474282 --out chat_2754474282.json --max-seconds 23766
"""

import argparse
import json
import re
import time
from pathlib import Path

import requests

GQL_URL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PERSISTED_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def extract_vod_id(arg):
    m = re.search(r"(\d{6,})", arg)
    if not m:
        raise SystemExit(f"Could not extract VOD ID from: {arg}")
    return m.group(1)


def build_body(video_id, offset):
    return [{
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": {"videoID": video_id, "contentOffsetSeconds": int(offset)},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
    }]


def fragments_to_body(fragments):
    if not isinstance(fragments, list):
        return ""
    return "".join(f.get("text", "") for f in fragments if isinstance(f, dict))


def normalize_comment(node):
    msg = node.get("message") or {}
    commenter = node.get("commenter") or {}
    return {
        "_id": node.get("id"),
        "content_offset_seconds": float(node.get("contentOffsetSeconds", 0)),
        "commenter": {
            "display_name": commenter.get("displayName", ""),
            "name": commenter.get("login", ""),
            "_id": commenter.get("id", ""),
        },
        "message": {
            "body": fragments_to_body(msg.get("fragments")),
            "fragments": msg.get("fragments") or [],
        },
    }


def fetch_page(session, video_id, offset):
    body = build_body(video_id, offset)
    for attempt in range(5):
        try:
            r = session.post(GQL_URL, json=body, timeout=30)
            if r.status_code == 200:
                payload = r.json()
                if not isinstance(payload, list) or not payload:
                    raise RuntimeError(f"Unexpected GQL payload: {payload!r}")
                errors = payload[0].get("errors") or []
                data = payload[0].get("data") or {}
                video = data.get("video")
                if video is None:
                    raise RuntimeError(f"Video not returned (errors={errors})")
                return video.get("comments")
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Exhausted retries at offset={offset}")


def main():
    p = argparse.ArgumentParser(description="Fetch Twitch VOD chat via GraphQL")
    p.add_argument("vod", help="VOD URL or numeric ID")
    p.add_argument("--out", help="Output JSON path (default: chat_<id>.json)")
    p.add_argument("--max-seconds", type=int, default=0,
                   help="Hard upper bound on VOD length in seconds (0 = no cap)")
    p.add_argument("--progress-every", type=int, default=50,
                   help="Print a progress line every N pages (default 50)")
    args = p.parse_args()

    video_id = extract_vod_id(args.vod)
    out_path = Path(args.out) if args.out else Path(f"chat_{video_id}.json")

    session = requests.Session()
    session.headers.update({"Client-ID": CLIENT_ID, "Content-Type": "application/json"})

    comments = []
    seen_ids = set()
    offset = 0
    page = 0
    stagnant = 0
    t0 = time.time()

    while True:
        page += 1
        data = fetch_page(session, video_id, offset)
        if data is None:
            print(f"  page {page}: comments=null at offset={offset}, stopping")
            break

        edges = data.get("edges") or []
        new_in_page = 0
        last_off = offset
        for e in edges:
            node = e.get("node") or {}
            nid = node.get("id")
            if not nid or nid in seen_ids:
                continue
            seen_ids.add(nid)
            comments.append(normalize_comment(node))
            new_in_page += 1
            off = float(node.get("contentOffsetSeconds", last_off))
            if off > last_off:
                last_off = off

        if new_in_page == 0:
            stagnant += 1
            if stagnant >= 3:
                print(f"  page {page}: stagnant for 3 pages at offset={offset}, stopping")
                break
            offset += 5
            continue

        stagnant = 0
        offset = int(last_off) + 1

        if page % args.progress_every == 0:
            elapsed = time.time() - t0
            rate = len(comments) / elapsed if elapsed else 0
            print(f"  page {page:5d}  total={len(comments):7,}  "
                  f"@{offset:6d}s  elapsed={elapsed:6.1f}s  rate={rate:5.0f} msg/s",
                  flush=True)

        if args.max_seconds and offset >= args.max_seconds:
            print(f"  reached --max-seconds={args.max_seconds}, stopping")
            break

    out = {
        "video": {"id": video_id, "length": offset},
        "comments": comments,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"\nWrote {len(comments):,} comments ({page} pages) -> {out_path} "
          f"({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
