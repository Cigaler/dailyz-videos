#!/usr/bin/env python3
"""
DailyZ Voice Round 2 — Audio Generation Script
Run with: ELEVENLABS_API_KEY=your_key python3 generate_audio.py
Requires: requests (pip install requests)
"""

import os
import sys
import time
import requests

API_KEY = os.environ.get("ELEVENLABS_API_KEY")
if not API_KEY:
    print("ERROR: Set ELEVENLABS_API_KEY environment variable")
    print("Example: ELEVENLABS_API_KEY=sk_... python3 generate_audio.py")
    sys.exit(1)

BASE_URL = "https://api.elevenlabs.io/v1"
HEADERS = {"xi-api-key": API_KEY, "Content-Type": "application/json"}
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# Voice IDs
CHRIS_ID = "iP05pwEIh0LThisvBnZD"
BRIAN_ID = "nPczCjzI2devNBz1zQrb"

# Test Scripts
SCRIPT_A = (
    "Most people work harder every year — and end up with less. "
    "That's not bad luck. That's a broken system. "
    "Here's what they never taught you about money."
)
SCRIPT_B = (
    "So apparently AI just made an entire movie in 4 hours. "
    "The director found out on Twitter. "
    "This is either the best or worst thing to happen to Hollywood — "
    "and honestly? I can't decide which."
)

# Voice settings variants
VARIANTS = [
    # (filename_prefix, voice_id, stability, similarity_boost, style)
    ("chris-C1", CHRIS_ID, 0.35, 0.75, 0.40),  # Conversational
    ("chris-C2", CHRIS_ID, 0.45, 0.80, 0.30),  # Narrative Warmth
    ("chris-C3", CHRIS_ID, 0.30, 0.70, 0.50),  # Assertive/Dynamic
    ("brian-B1", BRIAN_ID, 0.40, 0.70, 0.40),  # Warm/Curious
    ("brian-B2", BRIAN_ID, 0.35, 0.75, 0.35),  # Relaxed Authority
]

# Custom voice descriptions for Voice Design
CUSTOM_VOICES = [
    (
        "founder",
        (
            "A medium-deep male voice. Natural, warm, slightly rough — "
            "like a founder explaining something he finds genuinely fascinating. "
            "Calm and unhurried. Has a subtle low-frequency vibration that gives "
            "sentences weight without being heavy. Not corporate, not polished AI. "
            "Sounds like a real person."
        ),
    ),
    (
        "analyst",
        (
            "A male voice with quiet authority. Medium-deep baritone with a slight "
            "grain or texture — not smooth TTS. Versatile and emotionally open — "
            "can carry both serious tech analysis and lighter trend commentary. "
            "Measured pace, composed energy. Sounds trustworthy without sounding "
            "like a news anchor."
        ),
    ),
    (
        "narrator",
        (
            "A distinctive male voice, medium-deep, with noticeable but not harsh "
            "vocal roughness. Calm, composed default tone. Conveys genuine curiosity "
            "and intellectual weight. Very adaptable — equally comfortable in a "
            "fast-paced trend video and a serious cultural piece. Has a slight "
            "wryness that prevents it from sounding pompous."
        ),
    ),
]


def generate_tts(voice_id, text, stability, similarity_boost, style, output_path):
    url = f"{BASE_URL}/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": True,
        },
    }
    r = requests.post(url, json=payload, headers=HEADERS)
    if r.status_code == 200:
        with open(output_path, "wb") as f:
            f.write(r.content)
        print(f"  ✓ {os.path.basename(output_path)}")
        return True
    else:
        print(f"  ✗ {os.path.basename(output_path)} — {r.status_code}: {r.text[:200]}")
        return False


def generate_custom_voice_and_tts(description, name):
    print(f"\nGenerating custom voice: {name}")
    # Step 1: Generate voice from description
    url = f"{BASE_URL}/voice-generation/generate-voice"
    payload = {
        "gender": "male",
        "age": "middle_aged",
        "accent": "american",
        "accent_strength": 1.0,
        "text": description,
    }
    r = requests.post(url, json=payload, headers=HEADERS)
    if r.status_code != 200:
        print(f"  ✗ Voice generation failed: {r.status_code}: {r.text[:200]}")
        return None

    voice_id = r.json().get("voice_id")
    if not voice_id:
        print(f"  ✗ No voice_id in response: {r.json()}")
        return None
    print(f"  ✓ Generated voice_id: {voice_id}")

    # Step 2: Save to library (so we can use it for TTS)
    save_url = f"{BASE_URL}/voice-generation/create-previews"
    # Actually just generate TTS directly with the voice_id
    return voice_id


def main():
    print("DailyZ Voice Round 2 — Generating audio samples")
    print(f"Output: {OUTPUT_DIR}")
    print()

    # Generate Chris/Brian variants
    for prefix, voice_id, stability, similarity, style in VARIANTS:
        print(f"\nGenerating {prefix}...")
        for script_label, script_text in [("scriptA", SCRIPT_A), ("scriptB", SCRIPT_B)]:
            output_path = os.path.join(OUTPUT_DIR, f"{prefix}-{script_label}.mp3")
            generate_tts(voice_id, script_text, stability, similarity, style, output_path)
            time.sleep(1)  # Rate limiting

    # Generate custom voices
    for name, description in CUSTOM_VOICES:
        print(f"\nCustom voice '{name}':")
        voice_id = generate_custom_voice_and_tts(description, name)
        if voice_id:
            for script_label, script_text in [("scriptA", SCRIPT_A), ("scriptB", SCRIPT_B)]:
                output_path = os.path.join(OUTPUT_DIR, f"custom-{name}-{script_label}.mp3")
                generate_tts(voice_id, script_text, 0.50, 0.75, 0.35, output_path)
                time.sleep(1)

    print("\n✓ Done. Check the output directory for all generated files.")
    print("\nNOTE: Push the generated files to GitHub:")
    print("  cd /path/to/dailyz-videos && git add voice-casting/round2/*.mp3 && git commit -m 'Add Round 2 audio samples' && git push")


if __name__ == "__main__":
    main()
