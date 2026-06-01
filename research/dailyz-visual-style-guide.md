# DailyZ Visual Style Guide for AI Tools Shorts

This guide turns the existing DailyZ production SOP and launch-plan branding into a copy-paste visual setup for CapCut, Canva, and free voiceover workflows.

## 1. Video Canvas Spec

- Canvas size: `1080x1920`
- Aspect ratio: `9:16`
- Format: vertical short video
- Target frame rate: `60 fps`
- Safe fallback: `30 fps` if the source footage is only `30 fps` or CapCut playback gets unstable
- Export format: `MP4`
- Runtime target: `35 to 45 seconds`

Copy-paste setup:

```text
1080x1920
9:16 vertical
60 fps
MP4
35-45 seconds
```

## 2. Caption Style

- Font: `Anton`
- Fallback font: `Montserrat ExtraBold`
- Size: start at `80 pt` in CapCut, reduce to `72 pt` if lines wrap
- Text color: `#FFFFFF`
- Highlight color for key words: `#D8FF3E`
- Caption box: `#101010` at `85%` opacity
- Position: centered in the lower-middle safe area, roughly `65 to 70%` down the frame so platform UI does not cover it
- Animation: `word-by-word` reveal
- Max lines: `2`
- Max words per line: `6`
- Case: sentence case or all caps for the first hook only

CapCut style recipe:

```text
Font: Anton
Size: 80
Text: #FFFFFF
Keyword highlight: #D8FF3E
Background box: #101010 / 85%
Position: lower-middle safe area
Animation: word-by-word
Max: 2 lines, 6 words per line
```

## 3. Color Palette

- Primary background: `#101010`
- Primary accent: `#D8FF3E`
- Secondary accent: `#FF5A36`
- Light neutral: `#F6F6F2`

Usage rule:

- Use `#101010` for backgrounds, overlays, and caption boxes.
- Use `#D8FF3E` for key words, strokes, arrows, buttons, and hook emphasis.
- Use `#FF5A36` sparingly for alerts, comparisons, or "new" callouts.
- Use `#F6F6F2` only as a soft neutral behind screenshots or cover cards.

## 4. Outro Spec

- Outro length: last `3 seconds`
- Background: dimmed final footage or near-black full-screen card
- Main overlay text: `FOLLOW FOR 1 AI TOOL A DAY`
- Secondary line: `@dailyztrends`
- Optional small footer: `Save this for later`

Outro layout:

```text
Line 1: FOLLOW FOR 1 AI TOOL A DAY
Line 2: @dailyztrends
Optional small line: Save this for later
```

Style notes:

- Put line 1 in `Anton` all caps
- Keep `@dailyztrends` in lime `#D8FF3E`
- Add a quick fade-in or pop animation, not a slow cinematic transition

## 5. Stock Footage Sources

Use free B-roll only. Prioritize close-up tech footage, hands, screens, dashboards, dark offices, and abstract light textures over generic humanoid robots.

### Pexels

Site: `https://www.pexels.com/videos/`

Copy-paste search terms:

- `typing laptop dark`
- `coding screen close up`
- `computer screen reflection`
- `startup office night`
- `phone scrolling app`
- `dashboard analytics screen`
- `hands typing keyboard neon`
- `server room lights`
- `city lights timelapse`
- `abstract neon background`

### Pixabay

Site: `https://pixabay.com/videos/`

Copy-paste search terms:

- `technology background`
- `data center`
- `computer code`
- `laptop work close up`
- `smartphone app scrolling`
- `digital interface`
- `office monitor`
- `cyber background`
- `neon lights abstract`
- `business person laptop`

Selection rule:

- Choose clips with dark backgrounds, blue/green light, or high-contrast screens.
- Avoid obviously fake robot footage unless the script is specifically about robotics.
- Keep each B-roll clip on screen for `1 to 2.5 seconds`.

## 6. Voiceover Spec

- Tool: `ElevenLabs Free`
- Primary voice: `Janet`
- Fallback voice rule: if `Janet` is unavailable, use the closest neutral US-English female narrator voice and keep it consistent across all uploads
- Speaking pace: aim for `0.95x to 1.05x` normal pace
- Tone: sharp, clear, neutral, lightly skeptical
- Delivery style: short sentences, slight emphasis on tool names and payoff words

Voice direction:

```text
Voice: Janet
Pace: 0.95x to 1.05x
Tone: clear, sharp, neutral
Read style: fast explainer, not dramatic announcer
```

Performance rules:

- Keep total spoken script inside `65 to 100` words
- Do not sound overly excited or salesy
- Regenerate only if product names sound wrong or the read feels rushed

## 7. Canva Cover Image Spec

- Canvas size: `1080x1920`
- Use case: YouTube Shorts cover frame, TikTok cover reference, and first-frame style card
- Headline length: `3 to 5 words`
- Font: `Anton`
- Fallback font: `League Spartan Bold`
- Headline color: `#D8FF3E`
- Background color: `#101010`
- Accent color: `#FF5A36`
- Supporting neutral: `#F6F6F2`

Layout description:

1. Place one dark or blurred tech image full-bleed in the background.
2. Add a black overlay at `55 to 70%` opacity.
3. Put the headline in the top half or dead center, very large.
4. Add one supporting visual only:
   - tool logo
   - app screenshot
   - cropped interface panel
5. Add a thin lime border, arrow, or underline for emphasis.
6. Keep the bottom area clean so TikTok and YouTube UI will not block the key words.

Canva recipe:

```text
Canvas: 1080x1920
Background: dark tech image + black overlay
Headline: Anton, all caps, #D8FF3E
Accent: #FF5A36
Support image: 1 screenshot or logo only
Layout: bold text top/center, clean bottom third
```

Recommended headline formulas:

- `BEST FREE AI TOOL`
- `THIS SAVES 2 HOURS`
- `BETTER THAN GOOGLE?`
- `FASTEST WORKFLOW YET`
- `DON'T PAY FOR THIS`

## Default DailyZ Visual Stack

Use this exact baseline unless a topic clearly needs a variation:

```text
Canvas: 1080x1920 / 60 fps
Background: #101010
Accent: #D8FF3E
Caption font: Anton
Caption style: word-by-word
Voice: Janet
Outro CTA: FOLLOW FOR 1 AI TOOL A DAY
Handle: @dailyztrends
Cover headline: 3 to 5 words, all caps
```
