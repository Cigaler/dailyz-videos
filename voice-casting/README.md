# DailyZ Voice Casting

Voice evaluation and recommendation for the DailyZ channel's signature voice.

## Files

- **VOICE_EVALUATION.md** — Full evaluation report with scores, settings, and recommendation
- **previews/** — Official ElevenLabs preview MP3s for each shortlisted candidate
- **test-scripts/** — Scripts to use for final listening session (see VOICE_EVALUATION.md)

## Quick Reference

| Rank | Voice | Voice ID | Weighted Score |
|------|-------|----------|---------------|
| 🥇 #1 | Callum | `N2lVS1w4EtoT3dr4eOWO` | **8.00** |
| 🥈 #2 | George | `JBFqnCBsd6RMkjVDRZzb` | **7.36** |
| 🥉 #3 | Brian | `nPczCjzI2devNBz1zQrb` | **7.14** |

## How to Run the Owner Listening Session

1. Open [ElevenLabs app](https://app.elevenlabs.io) → Text to Speech
2. For each candidate voice, copy the Voice ID and settings from VOICE_EVALUATION.md
3. Run both test scripts (Script A + Script B) per voice
4. Let the owner select their preferred voice
5. Lock that voice ID into the production pipeline

## Preview Files

- `previews/callum-preview.mp3` — Callum (Husky Trickster) — **RECOMMENDED**
- `previews/george-preview.mp3` — George (Warm Storyteller) — **BACKUP**
- `previews/brian-preview.mp3` — Brian (Deep Resonant) — **THIRD OPTION**
- `previews/bill-preview.mp3` — Bill (Wise, Mature) — Honorable mention
- `previews/roger-preview.mp3` — Roger (Laid-Back Resonant) — Honorable mention
- `previews/chris-preview.mp3` — Chris (Charming) — Honorable mention
