# ava_extensions/

Tout le code spécifique à Ava vit ici. Le code OpenJarvis upstream (dans `src/openjarvis/`) n'est JAMAIS modifié en place — les modifications passent par `ava_extensions/patches/` avec justification.

## Arborescence

- `identity/system_prompts/` : persona Ava (core.md, pas de multi-profil)
- `identity/voice_samples/` : échantillons audio (gitignored)
- `branding/` : icônes, thème Tauri
- `backends/` : nouveaux backends TTS/STT/wake via registries upstream (kokoro_tts.py, piper_tts.py, silero_vad.py, wake_ava.py)
- `backends/tts_models/` : modèles ONNX téléchargés (gitignored, ~400 MB)
- `patches/` : modifications de code upstream avec justification dans patches/README.md
- `memory/` : enrichissements mémoire longue durée (soul, user_profile, facts)
- `skills/` : skills Ava au format agentskills.io
- `config/` : `ava.toml` (override de `~/.openjarvis/config.toml`)
