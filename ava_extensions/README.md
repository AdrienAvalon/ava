# ava_extensions/

Le code propre à Ava vit ici autant que possible. Les extensions et monkey-patches
restent préférés pour limiter les conflits avec OpenJarvis. Une frontière de sécurité
ou de serveur impossible à fermer depuis une extension peut toutefois exiger une
modification minimale sous `src/openjarvis/` ; elle doit alors porter un test de
régression et rester explicitement revue lors de chaque synchronisation amont.

## Arborescence

- `identity/system_prompts/` : persona Ava commune, sans identité privée
- `identity/relationship_profiles/` : overlays relationnels versionnés, sélectionnés uniquement par politique serveur et principal vérifié
- `identity/voice_samples/` : échantillons audio (gitignored)
- `branding/` : icônes, thème Tauri
- `backends/` : nouveaux backends TTS/STT/wake via registries upstream (kokoro_tts.py, piper_tts.py, silero_vad.py, wake_ava.py)
- `backends/tts_models/` : modèles ONNX téléchargés (gitignored, ~400 MB)
- `patches/` : modifications de code upstream avec justification dans patches/README.md
- `evals/relationship/` : corpus synthétique, gates relationnels et comparaison avant promotion humaine
- `memory/` : mémoire gouvernée en shadow et vérificateur de restauration ; jamais la mémoire canonique tant que les gates d'architecture restent ouverts
- `skills/` : skills Ava au format agentskills.io
- `config/` : `ava.toml` (override de `~/.openjarvis/config.toml`)
