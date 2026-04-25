# Parasite

Twitch VOD chat heatmap toolkit with a conversational agent (**Symbiote**) on
top. Fetches the full chat of a past broadcast, bins it into density buckets,
and surfaces the highest-engagement moments — used to find peaks in a long
stream without watching it.

**Names:** *Parasite* = the toolkit (scripts, fetch machinery, data). *Symbiote* = the agent the user talks to in the terminal. Keep them distinct when writing docs.

## Scripts

Five standalone scripts plus one agent entrypoint. No package.

- [fetch_chat.py](fetch_chat.py) — pulls the full chat from a VOD URL/ID via Twitch's public GraphQL API and writes a TwitchDownloader-compatible `chat_<id>.json`.
- [heatmap.py](heatmap.py) — consumes that JSON, emits a PNG histogram, a top-N terminal table, and `peaks.csv`.
- [peaks_detail.py](peaks_detail.py) — expands each peak into a Markdown report with top tokens and sampled messages.
- [viral_score.py](viral_score.py) — **Siphon virality engine.** Consumes `chat.json` + `peaks.csv`, scores each peak on a 0–100 Virality Index, computes smart-padded cut windows, and writes `viral_score.csv` + `siphon_report.md`. See *Siphon virality engine* section below.
- [clipper.py](clipper.py) — **Codex content manager.** Post-processes Siphon clips with visual effects: drama zoom (face punch-in on SHOCK/DRAMA), Thumerian chat crawl (crimson/silver scrolling overlay on HYPE/FUNNY), speech subtitles, logo branding, and optional 9:16 vertical secondary output. See *Codex content manager* section below.
- [symbiote.py](symbiote.py) — the Symbiote agent. REPL + Gemini function-calling loop. Imports from the scripts above. Also has a **Manual Mode** fallback — see below.
- [requirements.txt](requirements.txt) — `matplotlib`, `requests`, `google-genai`. Everything else is stdlib.

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

# Symbiote starts in manual mode by default; summon Symbot for the Gemini agent.
# Get a free key at https://aistudio.google.com/app/apikey
setx GEMINI_API_KEY "AIza..."           # PowerShell, close + reopen terminal
python symbiote.py                      # asks "Summon agent Symbot? [y/N]"
python symbiote.py --agent --provider auto
python symbiote.py --agent --provider groq --model fast
python symbiote.py --agent --provider gemini --fallback-provider groq
python symbiote.py --pro --agent        # Gemini 2.5 Pro for deeper analysis
python symbiote.py --manual             # Manual mode, no prompt

# Raw scripts (fallback / CI / power-user path)
python fetch_chat.py https://www.twitch.tv/videos/<id> --max-seconds 23766
python heatmap.py chat_<id>.json --info vod_<id>.info.json --top 15
python peaks_detail.py chat_<id>.json --peaks peaks.csv --out peaks_detail.md
python viral_score.py archive/<dir>/chat.json --peaks archive/<dir>/peaks.csv \
    --info archive/<dir>/info.json
python export_top_clips.py archive/<dir>/viral_score.csv \
    --chat archive/<dir>/chat.json --video path/to/vod.mp4 --dry-run
python clipper.py archive/<dir>/viral_score.csv \
    --chat archive/<dir>/chat.json --video path/to/vod.mp4 \
    --drama-zoom --chat-crawl --vertical --dry-run
```

**Why Gemini:** the project ships to users, and Gemini 2.5 Flash has a free tier — no payment method required. Anthropic's API is pay-as-you-go.

**Provider fallback:** Gemini remains primary by default. If Gemini returns quota/rate-limit exhaustion (`429`, `RESOURCE_EXHAUSTED`, quota, rate limit, `retryDelay`), Symbot marks Gemini on cooldown and retries the same user turn on Groq when `GROQ_API_KEY` is set. `--provider groq --model fast` uses Groq directly. `--no-fallback` disables automatic fallback.

**Free-tier limits (verified live 2026-04-23):** Gemini 2.5 Flash free tier is **5 RPM** per project (error: `generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 5`) plus a daily cap. A single Symbiote turn often fires 2 API calls (tool dispatch + result synthesis), so a heavy conversation hits this quickly. `chat_turn` catches 429s, parses the `retryDelay` field from the error, sleeps that long, and retries up to 3 times (`_open_stream` / `_retry_delay_from`). Users see a `[rate-limited — waiting Ns then retrying]` inline while it waits.

**Ownership split:** Gemini is for **general-public distribution** — anyone can clone the repo, grab a free AI Studio key, and run `symbiote.py`. The repo owner personally uses Claude Code (this file's reader) for their own analyses, hitting the raw scripts directly via Bash/Read tools — no Gemini quota, no chatbot intermediary. That's why we keep the raw scripts fully functional alongside `symbiote.py`.

## Archive layout

`symbiote.py`'s `fetch_vod` tool writes each VOD to its own dated directory:

```text
archive/
  <YYYY-MM-DD>_<streamer>_v<vod_id>/
    chat.json          # full chat history (TwitchDownloader-compatible)
    info.json          # yt-dlp metadata
    heatmap.png        # density histogram
    peaks.csv          # top-N peaks (raw density)
    peaks_detail.md    # per-peak tokens + samples
    viral_score.csv    # Siphon scored + padded clips with reasoning column
    siphon_report.md   # per-clip pillar breakdown + mood tokens + sample messages
    meta.json          # {vod_id, streamer, upload_date, fetched_at, duration, message_count, pages}

~/Desktop/VODs/        # VOD video files (configurable via VODS_DIR env var)
  <streamer>_<date>_v<vod_id>.<ext>
```

Directory date is the **VOD's upload date** (from yt-dlp `info.json.upload_date`), not the fetch date — sorting by filename gives stream chronology. If `info.json` can't be pulled, falls back to today's UTC date.

Path helpers in [symbiote.py](symbiote.py) (`chat_path`, `info_path`, `heatmap_path`, `vod_archive_dir`) glob `archive/*_v{vod_id}/` to locate artifacts. They also fall through to the legacy flat layout (`chat_<id>.json` in cwd) so the raw scripts still work.

## Symbiote agent tools

Seven tools exposed to Gemini via function-declarations in `symbiote.py`:

| Tool | Purpose |
| --- | --- |
| `fetch_vod` | Download chat + yt-dlp info + VOD video, archive into dated folder, write `meta.json`. `download_video=false` for chat-only. |
| `list_vods` | Walk `archive/` and return all known VODs with metadata. Also picks up legacy flat-layout chats. |
| `get_peaks` | Bin chat into buckets, return top-N peaks with HH:MM:SS + chapter label. Raw density only — no scoring. |
| `get_virality` | **Siphon engine.** Score peaks 0–100, smart-pad cut windows, classify mood, return reasoning. Use this for clip decisions. Writes `viral_score.csv` + `siphon_report.md`. |
| `export_top_clips` | Cut the best Siphon moments to MP4, write JSON sidecars, upload copy, manifest, and optional thumbnails in `~/Desktop/VODClips`. Prefers local files in `~/Desktop/VODs`, falls back to Twitch VOD URL. |
| `export_codex_clips` | Codex-styled export: drama zoom on SHOCK/DRAMA, Thumerian chat crawl on HYPE/FUNNY, speech subtitles, logo branding, optional 9:16 vertical secondary. Same input pipeline as `export_top_clips`. |
| `analyze_window` | Given a timestamp + duration, return top tokens and sampled messages. Accepts `H:MM:SS`, `H:MM`, or raw seconds. |
| `search_chat` | Regex-search the chat. Returns total matches + density peaks. |
| `open_heatmap` | `os.startfile` the VOD's heatmap.png (regenerates via `heatmap.py` if missing). |

System prompt in `SYSTEM_PROMPT`. Tool schemas in `TOOLS` (JSON Schema; `parameters` field, not `input_schema`). Execution dispatch in `run_tool` (filters kwargs to the tool function's signature, so Gemini passing an unexpected arg won't TypeError). Streaming function-call loop in `chat_turn`.

**Gemini specifics worth remembering when touching `chat_turn`:**

- Build `contents` as a list of `types.Content(role="user"|"model", parts=[...])`. Tool results go back as `role="user"` with `types.Part.from_function_response(name, response={"result": ...})`.
- In the streaming loop, text deltas and function-call parts both arrive through `chunk.candidates[0].content.parts`. Text parts can be split across chunks; function-call parts arrive fully formed. Aggregate text into `text_agg`, collect function_calls into `fn_calls`, then construct one `types.Content(role="model", parts=...)` per turn.
- `fc.args` is a proto `MapComposite`, not a dict — route through `_to_jsonable` before `json.dumps` or kwarg-splatting.
- Disable auto function calling (`AutomaticFunctionCallingConfig(disable=True)`) because we manage the loop manually.
- Rate-limit retries are wrapped in `_open_stream` — it catches the 429, reads the `retryDelay` from the error body, sleeps, and retries. Don't call `generate_content_stream` directly from `chat_turn` or you lose this.

**Windows console gotcha:** Gemini's prose uses `→`, `×`, em-dashes, and emotes that crash `cp1252` stdout. Top of `symbiote.py` and `viral_score.py` reconfigure `sys.stdout` / `sys.stderr` to `utf-8` with `errors="replace"` so a rogue glyph never kills a session. Leave this in even if the module looks cross-platform clean — without it, Windows Terminal users hit `UnicodeEncodeError` on the first `→`.

## Siphon virality engine (`viral_score.py`)

Turns a `peaks.csv` + `chat.json` into a ranked, padded, reasoning-annotated clip list.

**Algorithm — Virality Index 0–100:**

| Pillar | Weight | Formula |
| --- | --- | --- |
| Velocity | 37.5% | `100 × log₂(spike_mult) / log₂(20)` — 2× → 23, 10× → 77, 20× → 100 |
| Mood | 37.5% | Dominant emote category (HYPE/FUNNY/SHOCK/DRAMA) × confidence shrinkage |
| Echo | 25% | `min(100, 150 × mean(m+1, m+2) / peak_count)` — Story/Lingering/Jump Scare |
| Magnitude | ×scaler | `(0.5 + 0.5 × peak/p99)` — halves sub-median peaks, leaves p99 untouched |

Baseline guard: `max(mean(m-5..m-1), 0.3 × global_median)`. First 5 minutes skipped (noisy pre-stream).

**Smart Padding:** 1-second resolution walk around each peak. Start = 5s before velocity ramp-up (where chat goes quiet for 5 consecutive seconds walking backwards). End = where the echo falls silent for 10 consecutive seconds walking forward. Duration clamped 30–180s. Adjacent windows within 15s are merged into one clip (max 300s).

**Mood lexicon** (`MOOD_LEXICON` in `viral_score.py`): whitespace-split uppercase matching — captures short tokens (`W`, `L`, `+2`) that the `tokens_in` regex would miss.

**Break markers:** `ASSEMBLE` means bathroom/break start, and `SCATTER` means return from break. They are not hype/shock signals. Siphon removes windows with either marker from scoring/export consideration.

**Outputs:** `viral_score.csv` (reasoning column per clip) + `siphon_report.md` (pillar breakdown + mood tokens + sample messages per clip). Both land in the VOD's archive directory.

**Public API for `symbiote.py`:**
- `score_from_paths(chat_path, peaks_path, info_path=None) → list[dict]`
- `write_csv(scored, out_path)` / `write_md(scored, comments, out_path, chat_name)`

## Codex content manager (`clipper.py`)

Post-processes Siphon-scored clips with visual effects for the Codex channel aesthetic. Primary output preserves the source video's native aspect ratio. Vertical 9:16 is an optional secondary output via `--vertical`.

**Effects:**

| Effect | Trigger | Description |
| --- | --- | --- |
| Drama Zoom | SHOCK/DRAMA mood, virality > 85 | Face-detection punch-in using Haar cascades + `compute_face_focus_crop`. Same trim/crop/scale/concat segmentation as export_top_clips shock_cam. |
| Thumerian Chat Crawl | HYPE/FUNNY mood | ASS `\move()` scrolling overlay — crimson primary (`&H003232FF` BGR), silver glow outline (`&H00C0C0C0`). Rate-limited to 3 concurrent, 2/sec. |
| Speech Subtitles | `--speech-subtitles` flag | Reuses `transcribe_clip_to_srt` from export_top_clips. Adjusted MarginV for vertical. |
| Logo/Branding | `--logo` flag | PNG watermark via ffmpeg overlay filter. Top-right default. |
| Vertical 9:16 | `--vertical` flag | Split-screen: content crop top 60%, camera crop bottom 40%. Uses `detect_streamer_camera_box` for auto-detection. |
| Raw Mode | `--raw` flag | Zero post-processing — stream copy (`ffmpeg -c copy`), no effects, no re-encode. Fastest clean cuts. |

**CLI theme:** Vampiric/dark aesthetic — ANSI 256-color palette (blood red, crimson, ember, silver, bone), threaded `BloodDrip` spinner animation, ASCII art banner, mood badges, colored score tiers.

**Usage:**
```bash
# Codex mode (all effects)
python clipper.py archive/<dir>/viral_score.csv \
    --chat archive/<dir>/chat.json \
    --video path/to/vod.mp4 \
    --top 5 --drama-zoom --chat-crawl \
    --vertical --dry-run

# Raw mode (clean cuts, no effects)
python clipper.py archive/<dir>/viral_score.csv \
    --chat archive/<dir>/chat.json \
    --video path/to/vod.mp4 \
    --top 5 --raw --dry-run
```

**Public API for `symbiote.py`:**
- `export_codex_clips(args: argparse.Namespace) → dict` — main entry point
- `should_apply_drama_zoom(signals, row, min_score, moods) → bool`
- `write_chat_crawl_ass(comments, start, end, out_path, args, play_res_x, play_res_y) → Path`

## Manual Mode

Symbiote starts as a direct tool REPL by default — no AI in the loop. Type `agent`
inside manual mode, answer `y` to the startup prompt, or pass `--agent` to summon
Symbot, the Gemini-backed conversational agent.

**Triggers:**
- `python symbiote.py` — default startup asks whether to summon Symbot; Enter means manual
- `python symbiote.py --manual` — explicit flag
- `GEMINI_API_KEY` missing while summoning Symbot → stay in manual mode
- `DailyQuotaExhausted` mid-session → prompt to switch back to manual mode

**Commands in manual mode:**
```
fetch <url|id> [no-video]   — download chat (+ video unless 'no-video')
list                         — show archived VODs
peaks <id> [top=15]          — raw peak density table
score <id> [top=15]          — Siphon virality scoring
export <id> [top=5]          — cut top clips to Desktop/VODClips
codex <id> [top=5] [--raw]   — Codex-style clips (drama zoom + crawl), or raw clean cuts
window <id> <start> [dur=60] — analyze a timestamp window
search <id> <pattern>        — regex search chat
heatmap <id>                 — open heatmap PNG
agent                        — summon Symbot (Gemini)
help  |  quit
```

Press Up/Down at an empty `manual>` prompt to use the selectable command menu.

## VOD Video Files

`fetch_vod` (tool) and `fetch` (manual mode command) both download the VOD video via yt-dlp in addition to chat.

**VOD source directory — resolved in order:**
1. `VODS_DIR` environment variable if set
2. `~/Desktop/VODs` (resolves to `C:\Users\DARKLXRD\Desktop\VODs` on Windows)

Clip export output defaults to `~/Desktop/VODClips` (override with `VOD_CLIPS_DIR`).
If no local video exists for a VOD, export falls back to `https://www.twitch.tv/videos/<vod_id>` as the ffmpeg input.

Directory is created automatically. Filename template: `<streamer>_<upload_date>_v<vod_id>.<ext>`.

To skip video download: pass `download_video=false` to the `fetch_vod` Gemini tool, or append `no-video` in manual mode (`fetch <url> no-video`).

Override destination:
```
# PowerShell
setx VODS_DIR "D:\Stream\VODs"
# bash
export VODS_DIR=/mnt/storage/vods
```

## Conventions

- Each script is single-file. No premature module split.
- stdlib + `matplotlib` + `requests` + `google-genai` only. Don't add pandas/numpy — `collections.Counter` is enough for binning.
- Model choice: Gemini 2.5 Flash default (free, fast), Gemini 2.5 Pro behind `--pro` for deeper analysis at the cost of a tighter quota. `--model` accepts an arbitrary override if/when Google ships a better flash-tier model.
- **Live-tested 2026-04-23** — three-turn scripted conversation (list_vods → get_peaks → analyze_window) dispatched all tools correctly on the zackrawrr VOD. `SCATTER`/`ASSEMBLE` are now treated as break markers, not clip signals. Interpretive quality on clip-ready output is strong at `gemini-2.5-flash`; no need to default to `--pro`.
- No sentiment/LLM-classification layer yet. If added later, put it in a separate module that consumes the `peaks.csv` from an archive dir — keep the core pipeline fast and offline.
