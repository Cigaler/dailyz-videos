# DailyZ Production Log

## Video 001

- Date: `2026-05-31`
- Script source: `research/dailyz-scripts-batch-01.md` -> Video `#1` (`Google Just Turned Search Into an AI Assistant`)
- Final video path: `output/videos/video-001.mp4`
- Runtime: `44.111s`
- Format: `1080x1920`, vertical `9:16`, `30 fps`, `MP4`

## Tool notes

- Browser editor attempted first: `CapCut`
- Browser outcome:
  - public landing page loaded
  - hidden `magic-tools/text-to-speech` workspace loaded after clicking generate
  - usable generation was blocked by `Get Pro to use this feature with 1 credit` plus a sign-in modal
- Secondary browser TTS attempt: `TTSMaker`
- Secondary browser outcome:
  - blocked by Cloudflare challenge: `Performing security verification`
- Working fallback used:
  - narration generated from a free web TTS endpoint inside `scripts/render_dailyz_video_001.sh`
  - slides rendered locally with `ImageMagick`
  - final assembly/export handled by `ffmpeg`

## Visual settings used

- Background: `#101010`
- Primary accent: `#D8FF3E`
- Secondary accent: `#FF5A36`
- Font: `Anton`
- Layout:
  - lime top rule and border
  - left accent bar
  - all-caps headline
  - rounded dark subtext card
  - footer brand line `DAILYZ / @dailyztrends`

## Production timing

- Research + browser tool attempts: `~12 min`
- Render script build + QA: `~15 min`
- Total elapsed time: `~27 min`

## Notes

- The first render was too slow at roughly `50s`, so the narration was sped up to `1.20x` in the render script to hit the target runtime window.
- Output was checked with `ffprobe` and frame extracts after rendering.

## Video 002

- Date: `2026-06-01`
- Script source: `research/dailyz-trend-scripts-urgent.md` -> `Google's AI Video Stack in Plain English`
- Render script: `scripts/render_dailyz_video_002.sh` via shared helper `scripts/render_dailyz_video_common.sh`
- Final video path: `output/videos/video-002.mp4`
- Runtime: `44.277s`
- Format: `1080x1920`, vertical `9:16`, `30 fps`, `MP4`
- Notes:
  - kept the same six-slide visual system as `video-001`
  - narration tempo stayed at `1.20x`
  - topic framing emphasizes `Veo 3` as the output jump and `Flow` as the creator workflow layer

## Video 003

- Date: `2026-06-01`
- Script source: `research/dailyz-trend-scripts-urgent.md` -> `The Coding Stack Everyone Is Arguing About`
- Render script: `scripts/render_dailyz_video_003.sh` via shared helper `scripts/render_dailyz_video_common.sh`
- Final video path: `output/videos/video-003.mp4`
- Runtime: `44.678s`
- Format: `1080x1920`, vertical `9:16`, `30 fps`, `MP4`
- Notes:
  - kept the same six-slide visual system as `video-001`
  - narration tempo set to `1.18x` to keep the comparison readable while landing inside the target window
  - comparison positioning is explicit: `Cursor` for speed, `Replit` for beginners, `Claude Code` for workflow leverage

## Video 004

- Date: `2026-06-01`
- Script source: `research/dailyz-trend-scripts-urgent.md` -> `The AI Agent Test That Feels Real`
- Render script: `scripts/render_dailyz_video_004.sh` via shared helper `scripts/render_dailyz_video_common.sh`
- Final video path: `output/videos/video-004.mp4`
- Runtime: `44.878s`
- Format: `1080x1920`, vertical `9:16`, `30 fps`, `MP4`
- Notes:
  - kept the same six-slide visual system as `video-001`
  - narration tempo was tightened from `1.18x` to `1.26x` after QA passes showed the first export was over the runtime cap
  - `n8n` is pronounced as `n eight n` in the TTS copy so the free voiceover endpoint says it clearly

## Urgent batch notes

- Production approach stayed consistent across `002`-`004`:
  - slides rendered locally with `ImageMagick`
  - narration generated from the same free web TTS endpoint used in `video-001`
  - clips assembled and concatenated with `ffmpeg`
- Shared helper note:
  - the first helper revision emitted bash `circular name reference` warnings because local namerefs matched the caller array names
  - this was fixed before the final exports by renaming the internal nameref bindings
