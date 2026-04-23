# Parasite

Twitch VOD chat heatmap toolkit with a conversational agent (**Symbiote**) on
top. Fetches the full chat of a past broadcast, bins it into density buckets,
and surfaces the highest-engagement moments — used to find peaks in a long
stream without watching it.

**Names:** *Parasite* = the toolkit (scripts, fetch machinery, data). *Symbiote* = the agent the user talks to in the terminal. Keep them distinct when writing docs.

## Scripts

Three standalone scripts plus one agent entrypoint. No package.

- [fetch_chat.py](fetch_chat.py) — pulls the full chat from a VOD URL/ID via Twitch's public GraphQL API and writes a TwitchDownloader-compatible `chat_<id>.json`.
- [heatmap.py](heatmap.py) — consumes that JSON, emits a PNG histogram, a top-N terminal table, and `peaks.csv`.
- [peaks_detail.py](peaks_detail.py) — expands each peak into a Markdown report with top tokens and sampled messages.
- [symbiote.py](symbiote.py) — the Symbiote agent. REPL + Claude tool-use loop. Imports from the three scripts above.
- [requirements.txt](requirements.txt) — `matplotlib`, `requests`, `anthropic`. Everything else is stdlib.

## Why fetch_chat.py exists

yt-dlp's `--write-subs --sub-langs rechat` and its `--write-comments` path both
use the deprecated Twitch v5 `/comments` endpoint, which now returns 404. The
local [VoidRip](file:///c:/Users/DARKLXRD/Desktop/GITHUB/VoidRip) tool wraps
yt-dlp and inherits that failure.

`fetch_chat.py` uses the modern GraphQL operation `VideoCommentsByOffsetOrCursor`
(the one Twitch's own web player uses). Specifics:

- Public web `Client-ID`: `kimne78kx3ncx6brgo4mv6wki5h1ko`.
- Persisted query hash: `b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a`. Twitch rotates these occasionally — if the fetcher starts returning `video=null`, update this hash (grab from DevTools on twitch.tv).
- **Pagination: offset-only**, not cursor. Cursor pagination requires a valid `Client-Integrity` header. The `/integrity` endpoint hands out tokens, but Twitch rejects them unless a JS PoW challenge (`kpsdk`) is also satisfied — which we don't. Offset queries (`contentOffsetSeconds`) are not integrity-checked. We advance by `last_seen_offset + 1` per page and dedup by comment ID. Downside: if a single second has more than one page worth of messages (~100), the overflow is dropped — negligible for heatmap purposes.
- Stop condition: 3 consecutive stagnant pages (no new IDs) OR `--max-seconds` reached.

If Twitch ever hardens offset-based queries too, the fallback is TwitchDownloaderCLI (external binary, maintains the integrity handshake itself).

## Input schema (for heatmap.py)

`load_comments` auto-detects:

- `{"comments": [...]}` — TwitchDownloaderCLI, yt-dlp `.rechat.json`, and `fetch_chat.py` output all use this shape.
- Bare top-level list `[...]` — some raw Twitch v5 paginated dumps.
- `{"data"|"items"|"messages": [...]}` — fallback for other exporters.

Per-comment extraction:

- Offset: `content_offset_seconds` (preferred) or camelCase `contentOffsetSeconds`.
- Body: `message` as a string, or `message.body`, or joined `message.fragments[].text`.

## Chapters

Optional `--info <yt-dlp .info.json>` reads the `chapters` array and:

- Draws a dashed orange vertical line at each chapter `start_time` on the PNG.
- Labels each peak in the top-N table with the chapter title it falls inside.

## Commands

```bash
pip install -r requirements.txt

# The agent (recommended — orchestrates fetch + analysis from natural language)
export ANTHROPIC_API_KEY=sk-ant-...
python symbiote.py
python symbiote.py --opus          # Claude Opus 4.7 for deeper analysis

# Raw scripts (fallback / CI)
python fetch_chat.py https://www.twitch.tv/videos/<id> --max-seconds 23766
python heatmap.py chat_<id>.json --info vod_<id>.info.json --top 15
python peaks_detail.py chat_<id>.json --peaks peaks.csv --out peaks_detail.md
```

## Archive layout

`symbiote.py`'s `fetch_vod` tool writes each VOD to its own dated directory:

```text
archive/
  <YYYY-MM-DD>_<streamer>_v<vod_id>/
    chat.json          # full chat history (TwitchDownloader-compatible)
    info.json          # yt-dlp metadata
    heatmap.png        # density histogram
    peaks.csv          # top-N peaks
    peaks_detail.md    # per-peak tokens + samples
    meta.json          # {vod_id, streamer, upload_date, fetched_at, duration, message_count, pages}
```

Directory date is the **VOD's upload date** (from yt-dlp `info.json.upload_date`), not the fetch date — sorting by filename gives stream chronology. If `info.json` can't be pulled, falls back to today's UTC date.

Path helpers in [symbiote.py](symbiote.py) (`chat_path`, `info_path`, `heatmap_path`, `vod_archive_dir`) glob `archive/*_v{vod_id}/` to locate artifacts. They also fall through to the legacy flat layout (`chat_<id>.json` in cwd) so the raw scripts still work.

## Symbiote agent tools

Six tools exposed to Claude via the Messages API tool-use loop in `symbiote.py`:

| Tool | Purpose |
| --- | --- |
| `fetch_vod` | Download chat + yt-dlp info, archive into dated folder, write `meta.json`. Cache-hits if already archived. |
| `list_vods` | Walk `archive/` and return all known VODs with metadata. Also picks up legacy flat-layout chats. |
| `get_peaks` | Bin chat into buckets, return top-N peaks with HH:MM:SS + chapter label. |
| `analyze_window` | Given a timestamp + duration, return top tokens and sampled messages. Accepts `H:MM:SS`, `H:MM`, or raw seconds. |
| `search_chat` | Regex-search the chat. Returns total matches + density peaks. |
| `open_heatmap` | `os.startfile` the VOD's heatmap.png (regenerates via `heatmap.py` if missing). |

System prompt in `SYSTEM_PROMPT`. Tool schemas in `TOOLS`. Execution dispatch in `run_tool`. Streaming tool loop in `chat_turn`.

**Prompt caching:** `cache_control: {type: "ephemeral"}` on the system block — tools + system render together (tools render first), so the marker caches both as a single prefix. Current prefix is ~2K tokens, borderline for Sonnet 4.6's 2048-token minimum cache size; if tools or system prompt grow, cache hits will kick in.

## Conventions

- Each script is single-file. No premature module split.
- stdlib + `matplotlib` + `requests` + `anthropic` only. Don't add pandas/numpy — `collections.Counter` is enough for binning.
- Model choice: Sonnet 4.6 default, Opus 4.7 behind `--opus`. Don't downgrade to Haiku — tool orchestration needs real reasoning.
- No sentiment/LLM-classification layer yet. If added later, put it in a separate module that consumes the `peaks.csv` from an archive dir — keep the core pipeline fast and offline.
