# Parasite

Twitch VOD chat heatmap toolkit with a conversational agent on top. Pulls the
full chat of a past broadcast, bins it into density buckets, and surfaces the
highest-engagement minutes so you can cut clips without rewatching.

- **Parasite** — the toolkit: the machinery that latches onto a VOD, siphons chat via GraphQL, and produces heatmaps / peak reports.
- **Symbiote** — the agent: the half you talk to. Picks the right Parasite tool for each question and gives clip-ready answers.

## Symbiote CLI (recommended)

Manual tool REPL by default, with an optional Gemini-powered agent named
Symbot when you want natural-language orchestration. In manual mode, press
Up/Down at an empty prompt to open the selectable command menu.

```bash
pip install -r requirements.txt

# Get a free key at https://aistudio.google.com/app/apikey
# Optional Groq fallback key: https://console.groq.com/keys
# Easiest: create a .env file next to symbiote.py
cp .env.example .env
# edit .env and paste your keys

python symbiote.py                    # manual mode by default
python symbiote.py --agent --provider auto
python symbiote.py --pro --agent      # Symbot with Gemini 2.5 Pro
python symbiote.py --agent --provider groq --model fast
python symbiote.py --agent --provider gemini --fallback-provider groq
```

When run interactively, `python symbiote.py` asks `Summon agent Symbot? [y/N]`.
Answer `n` or press Enter for manual mode. The key can also be set as a system
env var (`setx GEMINI_API_KEY "AIza..."` on Windows, `export GEMINI_API_KEY='AIza...'`
on bash) — that always wins over `.env`.

### One-word command

Add the repo directory to your PATH once, then just type `symbiote` from anywhere.

**Windows (PowerShell, once):**
```powershell
$p = "C:\Users\DARKLXRD\Desktop\Parasitev2"   # adjust to your clone path
[Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";$p", "User")
# close + reopen terminal, then:
symbiote
symbiote --agent
symbiote --pro --agent
```

**macOS / Linux (once):**
```bash
echo 'export PATH="$PATH:/path/to/Parasitev2"' >> ~/.bashrc   # or ~/.zshrc
chmod +x /path/to/Parasitev2/symbiote
source ~/.bashrc
# then:
symbiote
symbiote --agent
symbiote --pro --agent
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
python heatmap.py chat_<id>.json --info vod_<id>.info.json --top 20

# 3. per-peak chat detail for clip planning
python peaks_detail.py chat_<id>.json --peaks peaks.csv --out peaks_detail.md

# 4. export the top Siphon clips
python export_top_clips.py archive/<dir>/viral_score.csv \
  --chat archive/<dir>/chat.json \
  --video ~/Desktop/VODs/<vod>.mp4 \
  --top 5 --from-top 20 --pad 10

# polished exports: fade-out, optional intro zoom, SHOCK punch-in
python export_top_clips.py archive/<dir>/viral_score.csv \
  --chat archive/<dir>/chat.json \
  --video path/to/vod.mp4 \
  --top 5 --pad 10 \
  --polish --intro-zoom --shock-cam --thumbnail

# optional speech-to-text subtitles with faster-whisper
python export_top_clips.py archive/<dir>/viral_score.csv \
  --chat archive/<dir>/chat.json \
  --video path/to/vod.mp4 \
  --top 5 --pad 10 \
  --speech-subtitles --subtitle-model tiny
```

Outputs: `heatmap.png`, `peaks.csv`, `peaks_detail.md`, plus clip exports in
`~/Desktop/VODClips`.

Clip export looks for local VOD files in `~/Desktop/VODs` first. If no local
file is available, it falls back to `https://www.twitch.tv/videos/<VOD ID>` as
the ffmpeg input. Set `VODS_DIR` or `VOD_CLIPS_DIR` to override those folders.

Polished exports use ffmpeg filters and force re-encode. `--polish` adds video
and audio fade-out, `--intro-zoom` adds a slow opening zoom-out, and
`--shock-cam` punches in on SHOCK windows. Face targeting uses OpenCV Haar
cascade detection first; if no face is found, it falls back to a center crop.

`--speech-subtitles` extracts each clip's audio, transcribes it locally with
`faster-whisper`, writes an `.srt` sidecar to `subtitles/`, and burns captions
into the exported MP4. Use `--subtitle-sidecar-only` to write SRT files without
burning them in. Subtitle styling is controlled with `--subtitle-style`.

Gemini remains the default Symbot provider. If Gemini hits quota/rate-limit
exhaustion, Symbot automatically falls back to Groq for the current turn when
`GROQ_API_KEY` is set. Use `--no-fallback` to disable that behavior.

Siphon treats `ASSEMBLE` and `SCATTER` as break/bathroom markers, not hype.
Those windows are removed from scored/exported clip consideration.

See [CLAUDE.md](CLAUDE.md) for architecture notes and Twitch API constraints.
