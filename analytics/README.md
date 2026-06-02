# DailyZ Analytics System

This folder is the Phase 2 performance database for every published DailyZ video. The goal is simple: capture consistent post-publish data, classify outcomes the same way every time, and feed the lessons back into script and format choices.

## Files

- `performance_log.json`: the source-of-truth database for every video.
- `what_works.md`: the running list of winning patterns to reuse.
- `avoid.md`: the running list of losing patterns to stop repeating.
- `experiments_log.md`: the ledger for one controlled experiment per week.
- `update_views.py`: the manual CLI for entering new view counts.

## How to add a new video entry

1. Duplicate an existing object in `performance_log.json`.
2. Increment `video_id` and keep it zero-padded (`005`, `006`, and so on).
3. Fill in the planned metadata before publish:
   - `title`
   - `format`
   - `hook_text`
   - `topic_tags`
   - `title_style`
   - `platform`
4. After the video is posted, update:
   - `publish_date`
   - `post_time`
5. Every time a block of 5 new videos has fresh metrics, paste in the new counts with `update_views.py`.

Example:

```bash
python3 analytics/update_views.py 005 youtube 24h 18450
python3 analytics/update_views.py 005 tiktok 24h 62200
```

## Manual update workflow

The owner should batch updates every 5 videos so comparisons stay clean.

1. Publish the next 5 videos.
2. Once 24-hour numbers are available, run `update_views.py` for each live platform.
3. Repeat the same process at 7 days and 30 days.
4. After the 7-day numbers are filled in for that batch, review the ranking and update:
   - `result`
   - `lesson_learned`
   - `what_works.md`
   - `avoid.md`
   - `experiments_log.md` if a deliberate test was running

## How results are classified

Use published videos with non-null `7d` views as the comparison set.

1. Calculate a 7-day score for each video:
   - `youtube 7d + tiktok 7d`
   - if a platform is not used for that video, treat the missing side as zero
2. Rank all published videos by that score.
3. Classify:
   - `winner`: top 25%
   - `underperformer`: bottom 25%
   - `average`: everything in the middle
4. Keep `result` as `pending` until the video has enough data to compare fairly.

## How lessons feed back into decisions

- If winners cluster around one format, hook style, topic, or post time, add that pattern to `what_works.md`.
- If underperformers share a repeated structure or weak angle, log it in `avoid.md`.
- If a specific variable was intentionally tested, record the outcome in `experiments_log.md`.
- Use those notes before writing the next script batch so each new set of videos reflects the latest evidence.

## Trend output from `update_views.py`

The script prints two quick signals after each update:

- `Current average views`: the average for the selected platform and timeframe across every video that has a value.
- `Trend vs previous batch of 5`: compares the latest batch average against the previous batch average for the same platform and timeframe.

Trend rules:

- `up`: latest batch average is more than 5% higher
- `down`: latest batch average is more than 5% lower
- `flat`: change is within 5%, or there is not enough history yet to compare two full batches
