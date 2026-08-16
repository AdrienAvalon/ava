# CLAUDE.md — Projet Ava

> **À Claude Code** : ce fichier est ton point d'entrée. Lis-le en début de chaque session. Respecte les conventions. En cas de doute, **demande** plutôt que de deviner. Mets ce fichier à jour (section "État actuel") en fin de session.

---

## 1. Contexte du projet

**Ava** est une IA personnelle féminine construite en forkant [open-jarvis/OpenJarvis](https://github.com/open-jarvis/OpenJarvis) (Stanford Hazy Research / Scaling Intelligence Lab, alpha 0.1.1, activement maintenu — 214 commits sur les 3 derniers mois).

**Objectif** : assistant conversationnel vocal en français, branché sur l'API Anthropic (Claude Sonnet 4.6 / Opus 4.6 / Haiku 4.5), déployé sur infrastructure souveraine auto-hébergée.

**Identité Ava** :
- **Personnalité féminine** — ton complice, direct, attachant, pas servile
- **Voix de femme française** naturelle (priorité absolue sur la qualité vocale)
- **Persona commune + identité vérifiée** : chaque historique est cloisonné par principal ;
  un overlay relationnel privé, opt-in et révocable peut être lié par politique GitOps
- **Autonomie croissante** : skills vocales → skills agentiques → capacités proactives (suggestions, rappels, routines)
- **Mémoire** : le magasin OpenJarvis partagé reste legacy et en retrait ; le ledger
  gouverné demeure en shadow jusqu'à preuve de restauration et d'ancrage externe

**Langue** : français principal, anglais passif (comprend, peut lire de la doc technique EN mais répond FR sauf demande explicite).

---

## 2. État actuel

> **Claude Code : mets cette section à jour à la fin de chaque session.**

- **Dernier état relu** : 2026-08-16 (avant commits d'intégration)
- **Branche active** : `ava-main`
- **Jalon en cours** : identité HTTP, conversations durables, overlay relationnel et
  évaluations adversariales avant activation
- **Prochaine étape** : suites globales, release immuable attestée, shadow avec le moteur
  effectivement déployé, puis activation GitOps séparée si tous les gates passent
- **Questions en suspens** : aucune décision produit bloquante ; l'activation reste
  volontairement `enabled: false` jusqu'aux preuves E2E

### Décisions actées 2026-04-16

- **Pas de fallback local M0→M6** : 100 % API Anthropic. Fallback Ollama reporté post-M7 si besoin démontré.
- **VM Proxmox dédiée** : `avalon-ai-ava-01`, VMID 107, PVE-01, Debian 13, 8 vCPU / 16 Go RAM / 80 Go disque, IP DMZ `192.168.100.15`.
- **Repos** : `origin = github.com/AdrienAvalon/ava` (privé), `mirror = gitlab.avalon-network.com/avalon/ava` (self-hosted, push auto).
- **TTS validé POC 2026-04-16** : **Kokoro-82M** avec voix `ff_siwis` (Apache-2.0, CPU-only, souverain, 0 €/mois). Piper reste dispo comme fallback ultra-rapide (0.3s) si besoin. ElevenLabs rejeté (free tier bloque API voix). Cartesia rejeté au profit d'une solution open source qualité équivalente.
- **Décision historique remplacée** : la persona commune reste unique, mais les
  conversations sont maintenant cloisonnées par principal vérifié et l'overlay privé ne
  peut être sélectionné que par un binding serveur explicite.
- **Python 3.12** (OpenJarvis supporte 3.10–3.13 via `pyproject.toml`).
- **Extended thinking Claude** : patch dans `ava_extensions/patches/anthropic_thinking.py` (non présent upstream).
- **Prompt caching** systématique (cache SystemPromptBuilder + tools) — économie ~80-90 % sur tours répétés.
- **Persona Ava** : complice, directe, humour fin, tutoiement, non servile, peut challenger, assume compétence technique.
- **Gate qualité voix obligatoire** avant M0 : POC 1 soirée comparant Cartesia / Piper / ElevenLabs sur phrases Ava types.
- **Nouveau module CP v2 `ava_health`** : monitoring Ava (latence Claude API, queue TTS, skills success rate, budget Cartesia mensuel, memory size). Intégration Wazuh, Prometheus, Loki comme autres VMs DMZ.
- **Skill priorité M6** : **infra Avalon** (wrappers `/cp-status`, `/sops-get`, `/komodo`, `/ansible-audit`) — valeur unique vs Claude mobile générique.

---

## 3. Comprendre OpenJarvis (base du fork)

> Synthèse 2026-04-16 — basée sur exploration du repo upstream

### Architecture en 5 piliers

1. **Intelligence** (modèles) — moteur d'inférence, registres d'engines
2. **Agents** (orchestration) — boucle conversation, tool use, multi-turn
3. **Tools / Skills** — format [agentskills.io](https://agentskills.io), discovery auto
4. **Engine** (backends LLM) — cloud (Anthropic, OpenAI, Gemini, OpenRouter), local (Ollama, vLLM, LlamaCPP)
5. **Learning** — policy selection, métriques, optimisation comportement sur la durée

### Stack

- **Python ~90 %** du codebase (core, agents, skills, engines, speech)
- **Rust** — crate `openjarvis-python` obligatoire en production ; la release exige une
  wheel attestée au SHA et refuse de basculer si son import échoue
- **TypeScript + React 19** — frontend Tauri 2, Shadcn UI, Zustand state, Vite

### Flow typique

```
Micro → STT (faster-whisper) → Agent (OrchestratorAgent)
  → Engine.stream() (Anthropic API) → Tool use éventuel
  → TTS (Piper / OpenAI TTS) → Haut-parleur
```

Daemon HTTP sur `localhost:8000` par défaut. Tauri UI communique via IPC + WebSocket streaming. **Headless possible** (daemon tout seul sans UI).

### Ce qui est déjà fait (on hérite)

| Capacité | Fichier clé | État |
|---|---|---|
| Anthropic backend | `src/openjarvis/engine/cloud.py:522-610` | ✅ Tool use, streaming, message format OK |
| faster-whisper STT | `src/openjarvis/speech/faster_whisper.py` | ✅ FR prêt à l'emploi |
| Registry pattern (engines, TTS, agents, skills) | `src/openjarvis/core/registry.py` | ✅ Extension propre via `@Register.register(...)` |
| SystemPromptBuilder + prompt caching | `src/openjarvis/prompt/builder.py` | ✅ Gèle le prompt au build, cache Anthropic optimal |
| Agent orchestrator (function calling + structured) | `src/openjarvis/agents/orchestrator.py` | ✅ Multi-turn, tool use, streaming |
| Skills loading (agentskills.io) | `src/openjarvis/skills/manager.py` | ✅ Discovery auto, deps validation |
| Tauri desktop + daemon HTTP | `frontend/` + `src/openjarvis/daemon/gateway.py` | ✅ Split propre |
| Config TOML | `~/.openjarvis/config.toml` | ✅ Hardware detection auto (GPU/CPU) |

### Ce qui manque (= notre valeur ajoutée)

| Gap | Jalon | Où patcher |
|---|---|---|
| **Voix FR féminine naturelle** (Kokoro-82M FR non intégré upstream) | M2 | `ava_extensions/backends/kokoro_tts.py` via `@TTSRegistry.register("kokoro")` |
| **Personnalité Ava** (pas de persona FR par défaut) | M3 | `ava_extensions/identity/system_prompts/core.md` injecté via `SystemPromptBuilder` |
| **Extended thinking Claude** (pas de `thinking` field dans `_generate_anthropic`) | M4 | `ava_extensions/patches/anthropic_thinking.py` patch de `cloud.py:522-610` |
| **Wake word "Ava"** | M5 | `ava_extensions/backends/wake_ava.py` + openWakeWord ou Porcupine |
| **VAD** (voice activity detection) | M5 | même PR que wake word — gère silence/fin d'énoncé |
| **Skills FR personnalisées** (agenda, domotique, infra Avalon, rappels) | M6 | `ava_extensions/skills/` (format agentskills.io) |
| **Rebranding** (productName, icônes, thème CSS) | M1 | `ava_extensions/branding/` + `tauri.conf.ava.json` override |

### Red flags techniques notés

1. Speech côté cloud (OpenAI TTS) dominant, peu d'options locales FR → Piper sera notre ajout clé
2. i18n limité (strings, prompts, erreurs en anglais) → à adapter progressivement pour expérience FR native
3. Pas de VAD intégré → à gérer côté Ava (M5)
4. Alpha 0.1.1 = instabilité possible des API internes → chaque sync upstream peut demander des ajustements `ava_extensions/`

---

## 4. Stack technique Ava

### Environnement

- **VM** : `avalon-ai-ava-01` sur PVE-01 (VMID 107)
- **OS** : Debian 13
- **Ressources** : 8 vCPU, 16 Go RAM, 80 Go disque (extensible)
- **Réseau** : IP DMZ `192.168.100.15`, accès via jump AVA
- **Hardening** : rôles Ansible `common` + `hardening` + `nftables` + `monitoring` + `teleport` + `wazuh_agent`

### Stack logicielle

- **Python 3.12** (OpenJarvis accepte 3.10–3.13)
- **Gestionnaire deps** : `uv` (convention OpenJarvis)
- **Rust** stable récent (extension Python + Tauri)
- **Node.js ≥ 20** (frontend Tauri)
- **Frontend** : React 19 + Shadcn + Tailwind + Tauri 2 — **Vite 8 / TypeScript 7 /
  react-router 8** depuis le 2026-08-03

> ⚠️ **`uv sync --extra X` RETIRE TOUS LES AUTRES EXTRAS — ça a mis Ava par terre le
> 2026-08-03.** La commande ne fait pas *« ajoute X »* mais *« l'environnement doit être
> EXACTEMENT le projet + X »*. Un `uv sync --extra dev` lancé pour installer pytest a donc
> desinstallé **fastapi et uvicorn**, et le daemon est entré en boucle de redémarrage.
> ⚠️ **Le journal systemd ne dit PAS pourquoi** : il n'affiche que
> `status=1/FAILURE` en boucle, et `journalctl` filtré sur l'unité ne montre aucune trace
> applicative. Le message existe pourtant, clair et actionnable — *« Server dependencies
> not installed. Install the server extra »* — mais il faut **lancer le service à la main**
> pour le voir :
> `cd /home/avalon/ava && ~/.local/bin/uv run jarvis serve --host 127.0.0.1 --port 8001`
> C'est le premier geste à faire face à un crash-loop de ce service, avant toute hypothèse.
> **Extras requis en production — LA LISTE COMPLÈTE, à nommer TOUS à chaque `uv sync`** :
> ```
> uv sync --extra server --extra speech --extra dashboard \
>         --extra inference-cloud --extra framework-comparison --extra dev
> ```
> ⚠️ **`inference-cloud` porte `anthropic` ET `openai` — l'omettre COUPE LA PAROLE À AVA,
> en silence.** Vécu le 2026-08-03 : le service refusait de démarrer sur « No inference
> engine available » alors que `ANTHROPIC_API_KEY` était bien présente. La cause est dans
> `engine/cloud.py` :
> ```python
> if os.environ.get("ANTHROPIC_API_KEY"):
>     try:
>         import anthropic
>         self._anthropic_client = anthropic.Anthropic()
>     except ImportError:
>         pass          # ← avale l'erreur
> ```
> Clé présente + SDK absent → aucun client Claude → aucun moteur ne sert
> `claude-sonnet-4-6` → `get_engine()` rend `None` → `sys.exit(1)`. **Rien dans le message
> ne mentionne un paquet manquant** : on soupçonne la clé, la config, le merge — jamais un
> `except ImportError: pass` trois couches plus bas.
> ⚠️ **`framework-comparison` (polars) n'est pas optionnel non plus** : sans lui, la
> COLLECTE pytest s'interrompt (`Interrupted: 1 error during collection`) et la suite
> affiche **0 échec** — un zéro qui veut dire « rien n'a été mesuré », pas « tout va bien ».
>
> **Diagnostic réutilisable, 2 lignes au lieu d'une heure :**
> ```bash
> ./.venv/bin/python -c "from openjarvis.core.config import load_config; \
>   from openjarvis.engine import get_engine; c=load_config(); \
>   print(get_engine(c,None), get_engine(c,None,model='claude-sonnet-4-6'))"
> ```
> Un moteur rendu SANS modèle mais `None` AVEC : le moteur existe, il ne sait pas servir
> ce modèle-là. C'est un **SDK ou une clé** qui manque, jamais le moteur.
>
> ⚠️ **Le merge amont du 2026-08-03 a rendu ce défaut FATAL sans l'avoir créé** : la
> nouvelle version écarte un moteur incapable de servir le modèle demandé (« skipped
> rather than chosen and failing per-request later »), l'ancienne le choisissait quand
> même et échouait à chaque requête. Le durcissement est bon — mieux vaut refuser de
> démarrer que répondre par une erreur à l'usage — mais il transforme une configuration
> incomplète en panne au démarrage.
> ⚠️ `uv` n'est pas dans le PATH d'un shell **non interactif** : en SSH scripté, utiliser
> `~/.local/bin/uv`. Sans ça la commande échoue en « uv: fichier introuvable » et l'on
> croit à tort que la synchronisation a eu lieu.

> ✅ **L'EXTENSION NATIVE `openjarvis_rust` EST COMPILÉE ET DÉPLOYÉE depuis le 2026-08-03**
> — elle ne l'avait **jamais** été, ni en local ni sur la VM. Conséquence de son absence :
> **137 tests en échec** (75 dans `tests/security`, 39 dans `tests/memory`, 23 ailleurs),
> et `_rust_bridge.py` annonce explicitement qu'il n'existe **aucun repli Python** — « The
> Rust backend is mandatory ». **17 fichiers** l'importent, dont TOUT `security/` (scanner,
> SSRF, rate limiter, capabilities, file policy, injection scanner) et une partie de
> `tools/` (shell_exec, file_write, http_request, git_tool).
> ⚠️ **Le daemon démarrait et répondait quand même** — c'était tout le piège : « Ava
> fonctionne » ne voulait pas dire « sa couche de sécurité est active ». Elle ne l'était pas.
>
> ### Recompiler après une modification du code Rust
>
> Pour un test de développement local uniquement, `uv run maturin develop -m
> rust/crates/openjarvis-python/Cargo.toml --release` reste possible. Cette extension
> locale ne constitue jamais un artefact livrable à la VM.
>
> La release VM passe exclusivement par le builder versionné et épinglé :
> ```bash
> # checkout Ava propre et commit exact à livrer
> scripts/build-rust-attested.sh
> # le script affiche les deux chemins content-addressed à transmettre au deployeur
> AVA_RUST_WHEEL=/chemin/affiche/openjarvis_rust-...manylinux_2_36_x86_64.whl \
> AVA_RUST_ATTESTATION=/chemin/affiche/openjarvis_rust-...whl.attestation \
>   scripts/deploy-vm.sh
> ```
> `deploy/docker/Dockerfile.rust-builder` épingle Python 3.12.13, Rust 1.88.0 et
> Maturin 1.14.1 par image/digest. Il exécute les tests Cargo verrouillés, produit une
> wheel `cp312-cp312-manylinux_2_36_x86_64`, l'installe dans un venv isolé et en vérifie
> l'import. Le workflow `ava-ci` publie le même couple wheel/manifeste pendant 14 jours.
>
> L'attestation `ava-rust-wheel-attestation-v1` est un **manifeste de checksums non
> signé**, pas une preuve cryptographique d'auteur. Le deployeur recoupe néanmoins le
> commit, le sous-arbre Rust, le Dockerfile, les images épinglées, la wheel et sa
> compatibilité avant le premier SSH, puis recontrôle les octets sur la VM. Il installe
> toujours les extras par `uv sync --frozen` avant la wheel, car un sync ultérieur la
> retirerait. Aucun compilateur ni chaîne Rust n'est installé sur la VM durcie.

### Cerveau — Phase 1 (M0→M6)

- **Claude API Anthropic uniquement**
- Modèle par défaut : `claude-sonnet-4-6`
- Tâches complexes / raisonnement long : `claude-opus-4-6` + thinking (M4)
- Tâches rapides (Q&A factuel court, STT→intent simple) : `claude-haiku-4-5`
- **Prompt caching** : systématique (cache SystemPromptBuilder, économie coût ~80-90 % sur tours répétés)
- **Pas de fallback local**. Si API down → Ava silencieuse (assumé).

### Voix (POC validé 2026-04-16)

- **STT** : `faster-whisper` `int8`, modèle `large-v3` (déjà intégré upstream)
- **TTS default M2** : **Kokoro-82M** avec voix `ff_siwis` (Apache-2.0, repo `hexgrad/Kokoro-82M`).
  - Souverain, CPU-only, 0 €/mois
  - Latence ~2.6s par phrase sur CPU laptop → streaming chunk-by-chunk natif de Kokoro compense (premier chunk <1s)
  - Sample rate 24000 Hz
  - Workaround hardening : `TMPDIR` doit pointer vers un chemin exécutable (pas `/tmp` noexec CIS)
  - Phonemizer dep : `espeak-ng` système requis (`apt install espeak-ng libespeak-ng1`)
- **TTS fallback ultra-rapide** : **Piper** `fr_FR-siwis-medium` (0.3s par phrase, robotique mais si on a besoin de débit max)
- **TTS rejetés au POC** :
  - Cartesia Sonic — qualité équivalente à Kokoro mais cloud payant (15 €/mois)
  - ElevenLabs — free tier bloque l'API voix (erreur 402 `paid_plan_required`)
  - OpenAI TTS `nova/shimmer` — accent US perceptible en FR
  - XTTS v2 — 3-5 s latence sans GPU, inutilisable temps réel
- **Alternatives futures (M7+)** :
  - **Kyutai TTS 1.6B en_fr** (Paris, meilleur FR natif, streaming 200 ms) si on acquiert un GPU
  - **Chatterbox Multilingual** si besoin voice cloning Ava custom (10s d'audio → voix clonée)
- **Wake word** (M5) : openWakeWord (open source) ou Porcupine (gratuit non-commercial). Risque 3 lettres → envisager "Hey Ava" si faux positifs trop fréquents.
- **VAD** (M5) : silero-vad (CPU, léger, précis, nécessaire pour barge-in)

### Stack exclu en Phase 1

- ~~llama.cpp / Ollama~~ — post-M6
- ~~Qwen / Mistral locaux~~ — post-M6
- ~~vLLM, SGLang, TensorRT, CUDA~~ — jamais (pas de GPU)

---

## 5. Stratégie Git — fork avec upstream tracking

### Remotes

```
upstream  = https://github.com/open-jarvis/OpenJarvis.git            (lecture seule)
origin    = git@github.com:AdrienAvalon/ava.git                      (GitHub privé)
mirror    = git@gitlab.avalon-network.com:avalon/ava.git             (GitLab self-hosted)
```

**Lors d'un push, on pousse toujours sur `origin` ET `mirror`.** Configurer via :
```bash
git remote set-url --add --push origin git@github.com:AdrienAvalon/ava.git
git remote set-url --add --push origin git@gitlab.avalon-network.com:avalon/ava.git
```
Ou alternative propre : script `scripts/git-push-all.sh`.

### Branches

- `main` — **miroir strict de upstream/main**. Ne JAMAIS committer dessus directement.
- `ava-main` — branche de travail principale. Toutes les features y sont mergées.
- `ava/<feature>` — branches feature (ex: `ava/piper-tts-fr`, `ava/thinking-blocks-patch`)

### Workflow synchronisation upstream (hebdomadaire, vu le rythme de 214 commits/trimestre)

```bash
git fetch upstream
git checkout main
git merge --ff-only upstream/main
git push origin main
git checkout ava-main
git merge main                        # propage upstream dans le fork
# Résoudre conflits éventuels sur les fichiers patchés
git push
```

### Format commits

- `ava: <description>` pour nos modifications
- Commits atomiques, messages FR ou EN (cohérence par branche)
- Si patch d'un fichier upstream : mentionner chemin + raison dans le corps du commit

---

## 6. Architecture du code — principe d'isolation

**Règle d'or** : tout ce qui est spécifique à Ava vit dans `ava_extensions/`. On ne modifie le code upstream qu'en dernier recours via `ava_extensions/patches/`.

### Structure

```
ava/
├── CLAUDE.md                          ← ce fichier
├── README.md                          ← README du fork (privé)
│
├── docs/
│   ├── architecture.md                ← détails archi étendue
│   ├── identity.md                    ← personnalité Ava
│   ├── roadmap.md                     ← jalons détaillés
│   ├── decisions/                     ← ADR
│   │   └── NNN-<slug>.md
│   └── session-notes/                 ← journal sessions Claude Code
│       └── YYYY-MM-DD-<topic>.md
│
├── ava_extensions/                    ← TOUT ce qui est à nous
│   ├── README.md                      ← conventions techniques
│   ├── identity/
│   │   ├── system_prompts/
│   │   │   └── ava.md                 ← persona commune, sans identité privée
│   │   └── relationship_profiles/     ← overlays privés versionnés, sélection serveur
│   │   └── voice_samples/             ← hors Git (.gitignore)
│   ├── branding/
│   │   ├── icons/
│   │   └── theme.css
│   ├── backends/                      ← via registries upstream
│   │   ├── piper_tts.py               ← M2
│   │   ├── wake_ava.py                ← M5
│   │   └── silero_vad.py              ← M5
│   ├── patches/                       ← modifications code upstream (minimiser)
│   │   ├── README.md                  ← liste patches + justification + statut upstream PR
│   │   ├── anthropic_thinking.py      ← M4
│   │   └── anthropic_prompt_cache.py  ← M4
│   ├── memory/                        ← enrichissements mémoire longue durée
│   ├── skills/                        ← skills FR Ava (agendas, domotique, infra, etc.)
│   └── config/
│       └── ava.toml                   ← config globale Ava (override ~/.openjarvis/config.toml)
│
├── openjarvis_code/                   ← code upstream (ne jamais modifier en place)
│   └── src/openjarvis/...
│
└── scripts/
    ├── git-push-all.sh
    ├── sync-upstream.sh
    └── deploy-vm.sh
```

### Extension via registries

Exemple Piper :
```python
# ava_extensions/backends/piper_tts.py
from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

@TTSRegistry.register("piper")
class PiperTTSBackend(TTSBackend):
    ...
```
Zéro modification fichier upstream → récupération updates triviale.

### Patches documentés

`ava_extensions/patches/README.md` tient la liste :

| Patch | Fichier upstream | Lignes | Raison | Statut upstream |
|---|---|---|---|---|
| thinking_blocks | `src/openjarvis/engine/cloud.py` | 522-610 | Extended thinking Claude (Opus 4.6+) | PR à proposer |
| prompt_caching | `src/openjarvis/engine/cloud.py` | 292-345 | Cache_control sur system + tools | PR à proposer |

---

## 7. Roadmap — jalons

### M-1 — POC voix (1 soirée, AVANT M0)

> **Objectif** : valider la voix avant d'engager 50 h de dev. Si aucune voix ne convainc → STOP projet.

- [ ] Script Python 300 lignes sur laptop local (pas besoin VM)
- [ ] Installer `faster-whisper`, `anthropic`, `cartesia`, `piper-tts`
- [ ] Pipeline : STT faster-whisper FR → Claude Sonnet 4.6 → comparer TTS Cartesia / Piper / ElevenLabs
- [ ] 10 phrases types Ava à lire (questions, confirmations, humour, refus poli, explication technique)
- [ ] Écoute A/B/C des 3 voix sur les 10 phrases
- [ ] **Gate** : si Cartesia convainc → M0. Si rien ne convainc → on discute scope (ElevenLabs ? Fallback texte-only ?)

### M0 — Setup initial (1 soir)

- [ ] Création VM `avalon-ai-ava-01` (VMID 107, PVE-01, Debian 13, 8 vCPU / 16 Go / 80 Go, IP `192.168.100.15`)
- [ ] Ajout dans `infrastructure.yml` infra_avalon + inventaire Ansible + rôles common/hardening/nftables/monitoring/teleport/wazuh_agent
- [ ] Fork OpenJarvis → `github.com/AdrienAvalon/ava` (privé)
- [ ] Création repo mirror `gitlab.avalon-network.com/avalon/ava`
- [ ] Clone sur la VM Ava
- [ ] Configuration 3 remotes (upstream / origin + mirror via pushURL multiples)
- [ ] Création branches `main` (miroir) et `ava-main` (travail)
- [ ] Structure `ava_extensions/` + README
- [ ] `.gitignore` strict (secrets, voice_samples, logs, `.env`, `*.sqlite`)
- [ ] Scripts `scripts/git-push-all.sh` et `scripts/sync-upstream.sh`
- [ ] Premier commit `ava: initial fork structure`
- [ ] Install uv + Python 3.12 + deps OpenJarvis (extras `inference-cloud` + `speech` + `server`)
- [ ] Smoke test : `openjarvis --help` + appel Claude via config Anthropic

### M1 — Rebranding minimal (1 soir)

- [ ] `tauri.conf.ava.json` override → productName "Ava"
- [ ] Icônes Ava (placeholder possible au départ)
- [ ] Thème CSS `ava_extensions/branding/theme.css`
- [ ] Strings visibles (titre, about, window) → "Ava"
- [ ] Validation : lancement Tauri → app s'appelle bien "Ava"

### M2 — Voix française naturelle + expérience audio propre (1 weekend)

> **Gate qualité voix M-1** : POC comparatif Cartesia vs Piper vs ElevenLabs **avant** M0 officiel. Si aucune voix ne convainc → on arrête le projet avant de dépenser le temps.

#### 2.1 Backends TTS (pluggables)

- [ ] Backend `ava_extensions/backends/kokoro_tts.py` via `@TTSRegistry.register("kokoro")` — **backend par défaut** (validé POC)
- [ ] Backend `ava_extensions/backends/piper_tts.py` via `@TTSRegistry.register("piper")` — fallback ultra-rapide (0.3s)
- [ ] Installation `espeak-ng` + `libespeak-ng1` via rôle Ansible (dépendance phonemizer pour Kokoro FR)
- [ ] Configuration `TMPDIR=/var/lib/ava/tmp-exec` systemd unit Ava (workaround hardening CIS `/tmp` noexec)
- [ ] Téléchargement modèles dans `ava_extensions/backends/tts_models/` (gitignored, ~400 MB : Kokoro ONNX + Piper ONNX)
- [ ] Config `ava.toml` : `tts.backend = "kokoro"`, `tts.fallback = "piper"`, `tts.voice = "ff_siwis"`
- [ ] **Pas de clé API TTS** — économie 15-22 €/mois vs cloud

#### 2.2 Streaming chunk-by-chunk

- [ ] Intercepter stream Claude au niveau phrase complète (split `. ! ? \n\n`)
- [ ] Dès phrase complète : push vers TTS, commencer à jouer audio pendant que Claude continue de streamer
- [ ] Gain mesurable : 2-5 s de latence perçue économisée sur réponses > 3 phrases

#### 2.3 Text normalization preprocessor FR

- [ ] Module `ava_extensions/speech/normalize_fr.py` :
  - Dates : `01/02/2026` → `premier février deux mille vingt-six`
  - Heures : `14h30` → `quatorze heures trente`
  - Nombres : `1234` → `mille deux cent trente-quatre`
  - Abréviations : `M.` → `Monsieur`, `km` → `kilomètres`, `€` → `euros`
  - Markdown : supprimer `**`, `_`, blocs code, URLs longues
  - Emojis : mapper en mot FR ou supprimer
- [ ] Tests unitaires sur 50 cas types

#### 2.4 Cache TTS local

- [ ] Store SQLite `ava_extensions/speech/tts_cache.db`
- [ ] Hash SHA256(texte + voice_id + speed) → chemin WAV local
- [ ] Cache hit : 0 ms latence, 0 € coût API
- [ ] TTL configurable (défaut 30 j), purge automatique > 500 MB
- [ ] Phrases récurrentes pré-chauffées : "bonjour", "j'écoute", "je regarde ça", "c'est fait", "je n'ai pas compris"

#### 2.5 Interruption / barge-in

- [ ] VAD continu pendant qu'Ava parle (silero-vad en thread audio input)
- [ ] Si voix utilisateur détectée → fade-out 100 ms audio output + stop TTS queue
- [ ] Reprise sur la nouvelle query sans perdre contexte Claude

#### 2.6 Pronunciation dictionary

- [ ] Fichier YAML `ava_extensions/speech/pronunciation_fr.yml`
- [ ] Overrides custom : `Docker: "dau-keur"`, `Avalon: "a-va-lon"`, `Ansible: "an-si-beul"`, `AWS: "A-double-vé-S"`, `SOPS: "sopse"`, `Komodo: "ko-mo-do"`
- [ ] Appliqué juste avant l'envoi au backend TTS

#### 2.7 Validation M2

- [ ] Conversation vocale FR bout-en-bout via daemon HTTP
- [ ] Écoute 10 minutes en usage réel → confirmation qualité Kokoro perçue terrain
- [ ] Mesure latence first-byte-audio < 1500 ms (Kokoro CPU est plus lent que Cartesia cloud, streaming chunk doit compenser)
- [ ] Budget mensuel TTS = 0 € (souverain Kokoro)

### M3 — Personnalité Ava (2-3 soirées, itératif)

- [ ] `ava_extensions/identity/system_prompts/ava.md` — persona complète (ton, valeurs, garde-fous, style, humour)
- [ ] Injection via `SystemPromptBuilder` custom (subclass ou config prompt_path)
- [ ] Tests conversationnels : 20-30 échanges sur sujets variés (technique, émotionnel, pratique, humour)
- [ ] Itération ton jusqu'à ce que la personnalité soit cohérente et attachante
- [ ] **Validation propriétaire obligatoire** avant merge

### M4 — Patches Claude (1 soirée)

- [ ] `ava_extensions/patches/anthropic_thinking.py` — étend `_generate_anthropic()` avec `thinking: {type: "enabled", budget_tokens: 10000}` quand modèle = Opus 4.6
- [ ] `ava_extensions/patches/anthropic_prompt_cache.py` — `cache_control` sur system + tools
- [ ] Tests : frontend affiche reasoning streamé + cache hit rate loggé
- [ ] Documentation `patches/README.md`
- [ ] (Optionnel) proposer PR upstream pour contribution

### M5 — Wake word "Ava" + VAD (weekend)

- [ ] Choix openWakeWord vs Porcupine (gate : qualité détection "Ava" en FR → probable openWakeWord avec entraînement custom)
- [ ] Backend `ava_extensions/backends/wake_ava.py`
- [ ] Backend `ava_extensions/backends/silero_vad.py` (fin d'énoncé automatique, pas de push-to-talk)
- [ ] Intégration flux audio : wake → VAD → STT → brain → TTS
- [ ] Validation : "Ava, ..." déclenche écoute, silence fin d'énoncé détecté correctement
- [ ] **Risque connu** : wake word 3 lettres = faux positifs probables. Si trop fréquents, envisager "Hey Ava" ou nom plus distinctif.

### M6 — Skills personnelles + mémoire longue durée (plusieurs semaines)

#### 6.1 Skill **Infra Avalon** (priorité 1 — valeur unique)

- [ ] `ava_extensions/skills/avalon_infra/` avec manifest agentskills.io
- [ ] Wrappers des commandes existantes :
  - [ ] `infra_status` → GET `http://192.168.2.40:8100/api/v1/dashboard`
  - [ ] `cp_query` → GET arbitraire sur l'API CP v2 (endpoints publics LAN-only)
  - [ ] `sops_get` → wrapper `sops -d --extract` (clés safe uniquement, pas de secrets sensibles)
  - [ ] `komodo_action` → POST `/execute/{Op}` avec auth X-Api-Key/Secret depuis SOPS
  - [ ] `ansible_audit` → lecture dernier audit task via Semaphore API
  - [ ] `grafana_query` → PromQL via API Grafana (read-only)
  - [ ] `loki_logs` → LogQL via API Loki
- [ ] Usage cible : "Ava, comment va l'infra ?", "Ava, est-ce que la pipeline CP v2 est passée ?", "Ava, déploie le stack matrix"
- [ ] **Audit log** : toute commande Ava → infra loggée dans Loki `job="ava-skills"` pour traçabilité

#### 6.2 Skills personnelles standard

- [ ] Agenda : Google Calendar via MCP `claude_ai_Google_Calendar`
- [ ] Email : Gmail via MCP `claude_ai_Gmail`
- [ ] Notes rapides : Nextcloud Notes API (déjà hébergé) ou SQLite local
- [ ] Rappels / timers : stockage SQLite + notification desktop (Tauri) + Matrix #ops
- [ ] Recherche web : skill upstream à activer dans config
- [ ] Docs techniques : context7 MCP pour docs libs à jour

#### 6.3 Mémoire longue durée

- [x] Mettre `memory_facts.jsonl` en quarantaine : aucune lecture, injection ou
  écriture depuis les frontières HTTP, Matrix ou CLI conversationnelle
- [x] Garder le ledger et la mémoire gouvernée en shadow, sans promotion
  automatique ni apprentissage depuis les conversations ou les traces
- [ ] Migrer les faits legacy avec attribution, scope et validation humaine ;
  ne jamais réactiver le magasin partagé comme outil ou contexte modèle
- [ ] Prouver sauvegarde logique, restauration, ancrage append-only externe et
  cloisonnement des scopes avant toute lecture de mémoire gouvernée en runtime

#### 6.4 Tests bout-en-bout

- [ ] "Ava, rappelle-moi une tâche demain à 14h" → état `task:*` isolé avec TTL → notification, sans mémoire legacy
- [ ] "Ava, comment va l'infra ?" → skill infra → CP v2 dashboard → synthèse vocale 2 phrases
- [ ] "Ava, est-ce que j'ai des emails urgents ?" → skill Gmail → filtrage IA → liste courte vocale

### M7+ — Évolutions futures

- Fallback local Ollama (Qwen 2.5 7B Q4) si coupure API fréquente
- Évaluation voix alternatives (Cartesia, ElevenLabs, Kokoro) si Piper insuffisant
- Clonage de voix custom (XTTS v2) si upgrade GPU
- Agents autonomes et proactifs (Ava propose des actions sans qu'on demande)
- Interface mobile (Tauri Mobile)
- Intégration Home Assistant (domotique vocale)
- Skills avancées : codage, recherche académique, gestion finance perso

---

## 8. Autonomie de Claude Code — règles

### ✅ Claude Code peut faire SANS DEMANDER

- Créer/modifier tout fichier dans `ava_extensions/`
- Créer/modifier tout fichier dans `docs/` (sauf `roadmap.md` — validation requise)
- Créer des branches feature `ava/<nom>`
- Committer sur les branches feature
- Lancer les tests, linters, type-checkers
- Installer des dépendances **Python** via `uv add` si légères, open source, maintenues
- Mettre à jour cette section "État actuel" en fin de session
- Rédiger les session-notes

### ⚠️ Claude Code DOIT demander avant

- Toute modification de code upstream (ajout dans `patches/`)
- Ajout de dépendance lourde (>100 MB, GPU, peu maintenue)
- Merge d'une branche feature dans `ava-main`
- Modification du system prompt d'identité Ava
- Décisions produit : nom, voix, personnalité, branding
- Changement de modèle Claude par défaut
- Push vers `origin` ou `mirror` de modifications à impact architectural
- Sync upstream avec conflits non triviaux

### 🚫 Claude Code NE DOIT JAMAIS

- Committer des secrets (API keys, tokens, certificats)
- Committer des échantillons vocaux (`voice_samples/`)
- Committer des données personnelles identifiables
- Pousser sur `main` directement (miroir de upstream)
- Modifier `openjarvis_code/` en place (toujours passer par `ava_extensions/`)
- Supprimer les garde-fous du system prompt (safety, encouragement interactions humaines réelles)

---

## 9. Conventions de code

### Python

- Style : conventions OpenJarvis (`pyproject.toml`)
- Formatter : `ruff format`
- Linter : `ruff check`
- Types : annotations obligatoires sur nouveau code dans `ava_extensions/`
- Tests : `pytest`, tests unitaires pour backend ou patch
- Imports : absolus

### TypeScript / React

- Conventions frontend OpenJarvis
- Composants fonctionnels + hooks
- Shadcn + Tailwind uniquement

### Markdown

- Titres en français pour docs projet
- ADR techniques : anglais acceptable
- Exemples de code commentés

### Messages commit

- `ava: <description à l'impératif>`
- Exemple : `ava: add Piper FR TTS backend`
- Corps du commit pour le pourquoi si non évident

---

## 10. Sécurité et confidentialité

### Secrets

- Jamais en clair dans le code
- `.env` gitignored OU SOPS (cohérence avec infra_avalon)
- `.env.example` fourni avec noms sans valeurs
- **Clé API Anthropic** : dans SOPS `secrets/infra.yml` (section `anthropic.api_key`), injectée via variable d'env au démarrage du daemon Ava

### `.gitignore` critique

```
.env
.env.local
*.key
*.pem
ava_extensions/identity/voice_samples/
ava_extensions/memory/data/
logs/
*.db
*.sqlite
ava_extensions/backends/piper_models/
```

### RGPD

- Droit à l'oubli : commande vocale / CLI pour purger mémoire sur item précis
- Pas de partage avec tiers au-delà de l'API Anthropic (DPA Anthropic s'applique)
- Stockage chiffré au repos (LUKS sur la VM ou chiffrement applicatif SQLite)

---

## 11. Procédure de début de session

1. Lire ce `CLAUDE.md` en entier
2. Lire la dernière session-note dans `docs/session-notes/`
3. `git status`, `git log --oneline -10`, branche active
4. Si pas touché depuis > 7 jours : proposer `git fetch upstream` et voir les évolutions OpenJarvis à intégrer
5. Annoncer : "On reprend à <X>, prochaine étape prévue <Y>. On continue ?"
6. Attendre validation avant de démarrer

## 12. Procédure de fin de session

1. Commit propre du travail en cours (sur branche feature si pas encore mergé)
2. Mise à jour de la section "État actuel"
3. Création d'une session-note `docs/session-notes/YYYY-MM-DD-<topic>.md` :
   - Ce qui a été fait
   - Décisions (et pourquoi)
   - Problèmes rencontrés et résolution
   - Questions en suspens
   - Prochaine étape concrète et actionnable
4. Push vers `origin` et `mirror` (sauf instruction contraire)
5. Résumé au propriétaire avant fin

---

## 13. Communication avec le propriétaire

- **Langue** : français par défaut
- **Ton** : direct, franc, pas de flatterie. Challenger les décisions fragiles techniquement ou produit.
- **Transparence** : tâche mal cadrée ou risquée → le dire avant de commencer
- **Pas de surpromesses** : "je pense que ça devrait marcher, voici les risques" plutôt que "c'est bon je gère"
- **Doute** : poser la question plutôt que trancher seul

---

## 14. Références

- **Upstream** : https://github.com/open-jarvis/OpenJarvis
- **OpenJarvis docs** : https://open-jarvis.github.io/OpenJarvis
- **Anthropic API** : https://docs.claude.com
- **Piper TTS** : https://github.com/rhasspy/piper
- **faster-whisper** : https://github.com/SYSTRAN/faster-whisper
- **silero-vad** : https://github.com/snakers4/silero-vad
- **openWakeWord** : https://github.com/dscripka/openWakeWord
- **agentskills.io spec** : https://agentskills.io/specification
- **Infra hôte (Avalon)** : `/home/avalon/Documents/gitlab/infra_avalon/CLAUDE.md`

---

_Fichier vivant. Dernière révision : 2026-08-16 (identité vérifiée, conversations
durables, overlay relationnel privé, évaluations et releases immuables)._

## ⚠ Telemetrie externe (PostHog) — COUPEE le 2026-08-04, et a re-verifier apres chaque synchro amont

OpenJarvis amont peut pousser des evenements d'usage vers une instance PostHog tierce :
`https://34.231.106.201.sslip.io` — une IP AWS derriere un domaine *wildcard DNS* qui encode
l'IP dans son propre nom. Ajoute en amont par la **PR #351 du 17 mai 2026**
(`src/openjarvis/core/config.py`, historiquement `AnalyticsConfig.enabled = True`), entre dans ce fork par la
**synchronisation amont du 3 aout** (353 commits).

**Coupee par defaut dans le fork**, par `[analytics] enabled = false` dans
`~/.openjarvis/config.toml` et par `OPENJARVIS_NO_ANALYTICS=1` dans l'unite et le `.env`
GitOps. `DO_NOT_TRACK` et `OPENJARVIS_NO_ANALYTICS` sont aussi des kill switches runtime.
Le controle dans `scripts/deploy-vm.sh` (validation avant bascule et sante finale) fait
toujours echouer le deploiement si
ces gardes ont derive.

> ⚠ **CE N'EST PAS UNE REVUE DE CODE QUI L'A TROUVEE, C'EST UNE ALERTE DE SECURITE.**
> L'egress DMZ **bloque** ces envois → chaque echec est retente en boucle → Zeek a compte
> **~430 connexions en 2 h** vers une meme IP externe depuis `192.168.100.15`, ce qui a
> declenche l'alerte Grafana **« Beaconing suspect (egress DMZ soutenu) »**. Le motif etait
> exactement celui d'un canal C2 : intervalles reguliers, meme destination, hote DMZ.
> **La lecon vaut au-dela de ce cas** : une dependance amont peut ajouter un flux sortant
> sans que rien dans le diff ne saute aux yeux, et c'est le reseau qui le dit — pas le code.
> Et une alerte de securite qui tire pour du bruit connu finit ignoree : c'est le pire
> resultat possible, donc on coupe la cause plutot que de museler la regle.

**Diagnostic, si le motif revient** : `{job="zeek"} | json | id_orig_h="192.168.100.15"` pour
compter les connexions, puis **les logs du resolveur unbound** (`{unit="unbound.service"}`) pour
obtenir le **nom de domaine** — l'IP seule ne dit rien, c'est la requete DNS qui a nomme
`34.231.106.201.sslip.io` et permis de remonter a PostHog en une minute.
