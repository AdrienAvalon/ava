"""Ava speech-to-text backend — OpenAI GPT-4o-mini-transcribe with hallucination guard.

Improvements over the upstream `openai` backend:
- Uses `gpt-4o-mini-transcribe` (2024) instead of the legacy `whisper-1`.
  Lower hallucination rate, similar latency, similar cost (~0.006 USD/min).
- `temperature=0` to make decoding deterministic and minimize creative drift
  on silence or low-SNR audio.
- A short FR `prompt` that anchors the model in conversational context with
  Ava — drastically reduces the probability of YouTube-subtitle hallucinations
  ("Sous-titres réalisés par la communauté d'Amara.org" and friends).
- Post-filter that drops the well-known FR Whisper hallucinations and short
  noise-only segments. Returns empty text when the transcription matches a
  known hallucination — the frontend already skips empty transcripts.

Activated via `~/.openjarvis/config.toml`:

    [speech]
    backend = "openai_ava"
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import List, Optional

from openjarvis.core.registry import SpeechRegistry
from openjarvis.speech._stubs import SpeechBackend, TranscriptionResult

