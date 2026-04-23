# Parasite

Twitch VOD chat heatmap toolkit with a conversational agent on top. Pulls the
full chat of a past broadcast, bins it into density buckets, and surfaces the
highest-engagement minutes so you can cut clips without rewatching.

- **Parasite** — the toolkit: the machinery that latches onto a VOD, siphons chat via GraphQL, and produces heatmaps / peak reports.
- **Symbiote** — the agent: the half you talk to. Picks the right Parasite tool for each question and gives clip-ready answers.

## Symbiote CLI (recommended)

Natural-language interface — drop a VOD link, ask questions, get clip-ready
answers. Powered by Claude.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # PowerShell: $env:ANTHROPIC_API_KEY = '...'
python symbiote.py                    # defaults to Sonnet 4.6
python symbiote.py --opus             # Opus 4.7 for deeper analysis
```

Example session:

```text
you> gimme data on https://www.twitch.tv/videos/2754474282
symbiote> [fetches chat, archives to archive/<date>_<streamer>_v<id>/, reports top peaks]
you> what's going on at 2:57
symbiote> [pulls the window, quotes chat signal, suggests cut points]
you> when did people spam cinema
symbiote> [regex-searches chat, returns density peaks for that word]
you> show me the heatmap
symbiote> [opens archive/<dir>/heatmap.png]
```

## Archive layout

Every VOD Symbiote fetches is saved to its own dated folder:

```text
archive/
  2026-04-22_zackrawrr_v2754474282/
    chat.json          # full chat history
    info.json          # yt-dlp metadata (title, chapters, streamer)
    heatmap.png        # density histogram
    peaks.csv          # top-N peaks table
    peaks_detail.md    # per-peak token + sample report
    meta.json          # {vod_id, streamer, upload_date, fetched_at, duration, ...}
```

Directory name is `<upload_date>_<streamer>_v<vod_id>`. Sorting by filename
gives you stream chronology.

## Raw scripts (fallback / CI)

The agent's tools are the same code paths as these standalone scripts:

```bash
# 1. fetch the full chat via Twitch GraphQL (offset pagination, no auth needed)
python fetch_chat.py https://www.twitch.tv/videos/<id> --max-seconds <duration>

# 2. heatmap + top-N peaks
python heatmap.py chat_<id>.json --info vod_<id>.info.json --top 15

# 3. per-peak chat detail for clip planning
python peaks_detail.py chat_<id>.json --peaks peaks.csv --out peaks_detail.md
```

Outputs: `heatmap.png`, `peaks.csv`, `peaks_detail.md`.

See [CLAUDE.md](CLAUDE.md) for architecture notes and Twitch API constraints.
