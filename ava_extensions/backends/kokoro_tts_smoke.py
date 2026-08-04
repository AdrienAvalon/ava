#!/usr/bin/env python3
"""Smoke test M0 : Claude API -> Kokoro TTS FR.

Usage : uv run python ava_extensions/backends/kokoro_tts_smoke.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("TMPDIR", str(Path.home() / "ava" / ".tmp-exec"))


def call_claude(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def tts_kokoro(text: str, out_path: Path) -> float:
    from kokoro import KPipeline

    pipeline = KPipeline(lang_code="f", repo_id="hexgrad/Kokoro-82M", device="cpu")

    start = time.monotonic()
    chunks: list = []
    for _gs, _ps, audio in pipeline(text, voice="ff_siwis", speed=1):
        chunks.append(audio)
    elapsed = time.monotonic() - start

    full = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), full, 24000)
    return elapsed


def main() -> None:
    prompt = (
        "Dis-moi en deux phrases courtes, ton du quotidien, "
        "que tu es Ava et que tu es contente de demarrer ton premier jour chez Avalon."
    )
    print(f"Claude prompt: {prompt}")

    t0 = time.monotonic()
    response = call_claude(prompt)
    t_claude = time.monotonic() - t0
    print(f"Claude ({t_claude:.2f}s): {response}")

    out = Path("/tmp/ava_m0_smoke.wav")
    t_tts = tts_kokoro(response, out)
    print(f"Kokoro TTS ({t_tts:.2f}s): {out} ({out.stat().st_size} bytes)")
    print("\nM0 smoke test OK.")


if __name__ == "__main__":
    main()
