# Patches upstream

Modifications de code OpenJarvis upstream — à éviter sauf si impossible autrement.

Chaque patch est un module Python importé depuis `ava_extensions/boot.py` et qui
applique sa modification au runtime (monkey-patch ou hook).

## Liste

| Patch | Cible upstream | Objectif | Statut upstream |
|---|---|---|---|
| `anthropic_enhancements.py` | SDK `anthropic` (`Messages.create`) | Active **adaptive thinking** + **prompt caching** sur Claude (Sonnet/Opus). Forçage `temperature=1.0` quand thinking actif et neutralisation `temperature/top_p/top_k` sur les modèles qui refusent ces paramètres. | Pas de PR upstream — comportement spécifique à Ava |
| `system_prompt_loader.py` | OpenJarvis agent loader | Charge la persona Ava depuis `ava_extensions/identity/system_prompts/ava.md` au démarrage de l'agent. | À évaluer pour upstream |
| `learning_guard.py` | Chargeur de configuration et `SystemBuilder` | Force les optimiseurs upstream non évalués à rester inactifs ; l'évolution Ava utilisera un chemin GitOps évalué séparé. | Spécifique à la politique Avalon |

## Format attendu

- Un fichier Python par patch, idempotent (ne pas double-patcher au reload), avec un marker explicite (`__wrapped__` ou drapeau module-level).
- Header obligatoire : raison du patch + comportement upstream cassant qu'il contourne + piste de PR upstream si applicable.
- Logger `INFO` à l'enregistrement pour tracer dans les logs daemon.
