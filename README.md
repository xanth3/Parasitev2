# Parasite

Twitch VOD chat heatmap generator. Pulls the full chat of a past broadcast,
bins it into density buckets, and surfaces the highest-engagement minutes so
you can cut clips without rewatching the stream.

## Pipeline

```bash
pip install -r requirements.txt

# 1. fetch the full chat via Twitch GraphQL (offset pagination, no auth needed)
python fetch_chat.py https://www.twitch.tv/videos/<id> --max-seconds <duration>

# 2. heatmap + top-N peaks
python heatmap.py chat_<id>.json --info vod_<id>.info.json --top 15

# 3. per-peak chat detail for clip planning
python peaks_detail.py chat_<id>.json --peaks peaks.csv --out peaks_detail.md
```

Outputs: `heatmap.png`, `peaks.csv`, `peaks_detail.md`.

See [CLAUDE.md](CLAUDE.md) for architecture notes and Twitch API constraints.
