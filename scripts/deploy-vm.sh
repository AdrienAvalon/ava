#!/usr/bin/env bash
# Deploie Ava sur la VM `avalon-ai-ava-01` (DMZ 192.168.100.15), depuis le laptop.
#
# ⚠ POURQUOI CE SCRIPT EXISTE : LE DEPLOIEMENT MANUEL A CASSE LA PRODUCTION DEUX FOIS
#   EN UNE SOIREE (2026-08-03), toujours sur le meme geste — un `uv sync` avec une
#   liste d'extras incomplete.
#
#   1. `uv sync --extra dev` (pour installer pytest) a DESINSTALLE fastapi et uvicorn.
#      `uv sync` ne veut pas dire « ajoute » mais « l'environnement doit etre EXACTEMENT
#      le projet + les extras nommes ». Le daemon est entre en boucle de redemarrage.
#   2. Le meme geste a retire le SDK `anthropic`. Le service refusait alors de demarrer
#      sur « No inference engine available » ALORS QUE LA CLE ETAIT PRESENTE : le code
#      fait `except ImportError: pass` et avale l'erreur. Rien, dans le message, ne
#      mentionne un paquet manquant.
#   3. Un `uv sync` posterieur a l'installation de la wheel Rust l'a RETIREE — elle
#      n'est pas dans le lock, donc `uv sync` la considere comme un intrus.
#
#   Chacune de ces pannes vient d'un ordre ou d'une liste qu'il faut se rappeler. Un
#   humain l'oublie ; un script non.
set -euo pipefail

VM_JUMP="${AVA_JUMP:-avalon@192.168.2.40}"
VM="${AVA_VM:-avalon@192.168.100.15}"
RACINE_VM="${AVA_RACINE:-/home/avalon/ava}"
BRANCHE="${AVA_BRANCHE:-ava-main}"

# ⚠ LA LISTE COMPLETE DES EXTRAS DE PRODUCTION — a garder synchronisee avec CLAUDE.md.
#   `inference-cloud` porte `anthropic` ET `openai` : l'omettre COUPE LA PAROLE A AVA,
#   en silence. `framework-comparison` (polars) est requis par la COLLECTE pytest :
#   sans lui la suite s'interrompt et affiche « 0 echec » — un zero qui veut dire
#   « rien n'a ete mesure », pas « tout va bien ».
EXTRAS=(server speech dashboard inference-cloud framework-comparison dev)

# `uv` n'est PAS dans le PATH d'un shell non interactif : en SSH scripte, la commande
# echouerait en « uv: fichier introuvable » et l'on croirait la synchronisation faite.
UV="/home/avalon/.local/bin/uv"

# Exécute une commande sur la VM, à travers le jump host.
# ⚠ La VM est en DMZ : elle n'est PAS joignable en SSH direct depuis le laptop. Toute
#   commande passe par AVA. Un script qui l'oublie échoue en « Connection timed out »
#   et fait chercher un problème de pare-feu qui n'existe pas.
# ⚠ `2>/dev/null` sur le SSH EXTERIEUR uniquement : les deux hôtes affichent une
#   bannière « Authorized users only » sur stderr à chaque connexion, ce qui noyait la
#   sortie sous 14 lignes de bruit pour 6 lignes utiles. Un rapport de déploiement
#   illisible ne se lit pas, donc ne sert à rien.
#   ⚠ Les vraies erreurs SSH partent avec — c'est le compromis. Elles restent visibles
#   par le code de retour, que chaque étape contrôle (`set -e` + `verifier`).
ssh_vm() { ssh -o BatchMode=yes "$VM_JUMP" "ssh -o BatchMode=yes $VM $(printf '%q' "$1")" 2>/dev/null; }
titre() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

# ⚠ `verifier` plutôt que `A && B || C` : shellcheck SC2015 rappelle à juste titre que
#   cette forme n'est PAS un if-then-else — si `echo` échoue, la branche d'erreur part
#   quand même. Sur un script dont le seul rôle est de dire la vérité sur un
#   déploiement, une branche d'erreur qui se déclenche à tort est un défaut de fond.
verifier() { # <libellé> <valeur obtenue> <valeur attendue>
  if [ "$2" = "$3" ]; then
    echo "   ✓ $1"
  else
    echo "   ✗ $1 — obtenu : ${2:-<vide>}"
    echec=1
  fi
}

titre "1/6  Code — fast-forward depuis $BRANCHE"
ssh_vm "cd $RACINE_VM && git pull --ff-only origin $BRANCHE"

titre "2/6  Dependances Python (liste COMPLETE des extras)"
EXTRA_ARGS=""
for e in "${EXTRAS[@]}"; do EXTRA_ARGS="$EXTRA_ARGS --extra $e"; done
ssh_vm "cd $RACINE_VM && $UV sync $EXTRA_ARGS"

# ⚠ ORDRE NON NEGOCIABLE : la wheel Rust s'installe APRES `uv sync`, jamais avant.
#   Elle n'est pas dans le lock ; un `uv sync` posterieur la supprimerait, et
#   l'environnement paraitrait bon une minute plus tot.
titre "3/6  Extension native Rust (apres le sync, jamais avant)"
WHEEL=$(ssh -o BatchMode=yes "$VM_JUMP" "ssh -o BatchMode=yes $VM 'ls -t /tmp/openjarvis_rust-*.whl 2>/dev/null | head -1'")
if [ -n "$WHEEL" ]; then
  # ⚠ LA WHEEL EST-ELLE PLUS RECENTE QUE LE CODE RUST QU'ELLE EST CENSEE PORTER ?
  #   Elle etait choisie par `ls -t` sans le moindre controle d'identite : ni version,
  #   ni empreinte, ni correspondance avec le commit qu'on vient de tirer a l'etape 1.
  #   Scenario vecu-en-puissance : on modifie `rust/crates/openjarvis-security`, on
  #   pousse, on lance ce script en OUBLIANT de reconstruire la wheel dans le conteneur
  #   Debian 13 (procedure manuelle en 5 etapes). L'etape 3 retrouve alors la wheel de
  #   la semaine precedente, l'installe, et l'etape 6 reussit son `get_rust_module()`
  #   → « couche securite active », « Deploiement complet et verifie ».
  #   **La couche de securite tournerait sur du code ancien pendant que le rapport
  #   affirme le contraire** — et le script signale lui-meme plus bas que cette couche
  #   « ne se voit PAS a l'usage ». C'est exactement le genre d'ecart qu'on ne
  #   decouvrirait qu'en cherchant autre chose.
  WHEEL_TS=$(ssh_vm "stat -c %Y '$WHEEL'" || echo 0)
  RUST_TS=$(ssh_vm "cd $RACINE_VM && git log -1 --format=%ct -- rust/ 2>/dev/null" || echo 0)
  ssh_vm "cd $RACINE_VM && $UV pip install --no-deps -q '$WHEEL'"
  echo "   wheel : $(basename "$WHEEL")"
  if [ "${WHEEL_TS:-0}" -lt "${RUST_TS:-0}" ]; then
    echo "   ✗ WHEEL PERIMEE — construite le $(date -d "@$WHEEL_TS" '+%F %H:%M' 2>/dev/null)," \
         "le code Rust a change le $(date -d "@$RUST_TS" '+%F %H:%M' 2>/dev/null)."
    echo "     La couche securite tournerait sur du code ancien. Reconstruire la wheel :"
    echo "     cf. CLAUDE.md § « Recompiler apres une modification du code Rust »."
    echec_precoce=1
  else
    echo "   ✓ wheel posterieure au dernier changement du code Rust"
  fi
else
  echo "   ⚠ AUCUNE wheel sur la VM — la couche securite (security/, 17 fichiers) sera INERTE."
  echo "     Recompiler : cf. CLAUDE.md § « Recompiler apres une modification du code Rust »."
fi

titre "4/6  Frontend"
# ⚠ LE `grep` MASQUAIT L'ÉCHEC DU BUILD — trouvé par l'audit du 2026-08-04. En shell, le
#   pipe lie plus fort que `&&` : le code de retour de la liste est celui de `grep`, pas
#   celui de `npm run build`. Un build qui échoue mais dont la sortie contient le mot
#   « error » faisait donc RÉUSSIR l'étape, et le déploiement continuait sur un frontend
#   non reconstruit. `set -e` ne rattrapait rien : il ne voyait qu'un succès.
#   On teste donc le build SÉPARÉMENT, puis on affiche.
ssh_vm "cd $RACINE_VM/frontend && npm ci --silent && npm run build > /tmp/ava-build.log 2>&1"
ssh_vm "grep -E 'built in|precache' /tmp/ava-build.log || true"

titre "5/6  Redemarrage"
ssh_vm "sudo systemctl restart openjarvis"

titre "6/6  Verification — ce qui FAIT FOI, pas ce qu'on espere"
sleep 12
echec=${echec_precoce:-0}

# ⚠ LES EXTENSIONS SONT-ELLES REELLEMENT ENREGISTREES ? Aucun des controles suivants ne
#   le disait jusqu'au 2026-08-04 — on verifiait le service, deux ports HTTP, `import
#   anthropic` et le module Rust. Or `boot.py` attrape `Exception` PAR GROUPE et se
#   contente d'un `logger.warning` : c'est le bon choix (l'isolation evite la perte
#   totale), mais il transforme une panne franche en degradation MUETTE.
#   Demontre en live sur ce depot : `python -c 'import openjarvis'` imprime
#   « Ava: patches SDK Anthropic indisponible » puis continue, les trois autres groupes
#   se chargeant normalement. Une synchro amont qui deplace `openjarvis.tools._stubs`
#   (importe par `home_assistant.py`) ferait echouer tout le groupe `_skills` → Ava
#   repondrait « je n'ai pas acces a la maison » sur une infra parfaitement saine,
#   pendant que ce script afficherait « Deploiement complet et verifie ».
#   Un registre vide est SILENCIEUX par construction : le decorateur ne s'execute pas,
#   rien ne leve. Il faut donc aller le lire.
titre_extensions=$(ssh_vm "cd $RACINE_VM && ./.venv/bin/python -c \"
import ava_extensions.boot
from openjarvis.core.registry import ToolRegistry, TTSRegistry, SpeechRegistry
attendus = {
    'outil avalon_status': 'avalon_status' in ToolRegistry.keys(),
    'outil home_assistant': 'home_assistant' in ToolRegistry.keys(),
    'outil memoire': 'memoire' in ToolRegistry.keys(),
    'voix kokoro-fr (TTS)': 'kokoro-fr' in TTSRegistry.keys(),
    'dictee openai_ava (STT)': 'openai_ava' in SpeechRegistry.keys(),
}
for nom, ok in attendus.items():
    print(('OK ' if ok else 'KO ') + nom)
\" 2>/dev/null" || echo "")
if [ -z "$titre_extensions" ]; then
  echo "   ✗ impossible de lire les registres — boot.py n'a peut-etre pas pu s'importer"
  echec=1
else
  while IFS= read -r ligne; do
    [ -z "$ligne" ] && continue
    case "$ligne" in
      OK*) echo "   ✓ ${ligne#OK }" ;;
      KO*) echo "   ✗ ${ligne#KO } — NON ENREGISTRE (groupe boot.py en echec, cf. journal systemd)"; echec=1 ;;
    esac
  done <<< "$titre_extensions"
fi

verifier "service actif" "$(ssh_vm 'systemctl is-active openjarvis' || true)" "active"

verifier "page HTTP 200" \
  "$(ssh_vm "curl -s -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8000/" || echo 000)" "200"

# ⚠ Le relay socat 8080 est le chemin que prend le tunnel Cloudflare. Verifier 8000
#   seul laisserait passer une panne INVISIBLE depuis le LAN mais totale depuis
#   l'exterieur — c'est-a-dire pour l'unique utilisateur.
verifier "relay 8080 HTTP 200" \
  "$(ssh_vm "curl -s -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8080/" || echo 000)" "200"

# La couche securite ne se voit PAS a l'usage : le daemon repond parfaitement sans elle.
# C'est tout le piege — « Ava fonctionne » ne veut pas dire « Ava est protegee ».
if ssh_vm "cd $RACINE_VM && ./.venv/bin/python -c 'from openjarvis._rust_bridge import get_rust_module; get_rust_module()'" 2>/dev/null; then
  echo "   ✓ extension Rust chargee (couche securite active)"
else
  echo "   ✗ extension Rust ABSENTE — security/ est inerte"; echec=1
fi

# ⚠ LA TELEMETRIE EXTERNE DOIT RESTER COUPEE. Ajoutee en amont par la PR #351 du
#   17 mai 2026, elle est entree ici par la synchronisation amont du 3 aout et a pousse
#   des evenements vers une instance PostHog tierce — `https://34.231.106.201.sslip.io`,
#   une IP AWS derriere un domaine wildcard qui encode l IP dans son nom.
#   `enabled` vaut **True par defaut** en amont et il n existe AUCUN opt-out par variable
#   d environnement : la seule facon de la couper est `[analytics] enabled = false` dans
#   `~/.openjarvis/config.toml`, qui vit HORS GIT. Une reinstallation, une remise a zero
#   de la config ou une prochaine synchro amont la rallumerait donc en silence.
#   Ce qui l a rendue visible n est pas une revue de code mais une **alerte de securite** :
#   l egress DMZ bloque ces envois, chaque echec est retente, et Zeek a compte
#   ~430 connexions en 2 h vers une meme IP externe → « Beaconing suspect (egress DMZ
#   soutenu) ». Une alerte qui tire pour du bruit connu finit ignoree : c est le pire
#   resultat possible, et c est pourquoi ce controle est ici plutot qu en commentaire.
if ssh_vm "cd $RACINE_VM && ./.venv/bin/python -c \"
import sys
from openjarvis.core.config import load_config
from openjarvis.analytics.identity import is_analytics_enabled
sys.exit(0 if not is_analytics_enabled(load_config().analytics) else 1)
\"" 2>/dev/null; then
  echo "   ✓ telemetrie externe (PostHog) coupee"
else
  echo "   ✗ TELEMETRIE EXTERNE ACTIVE — des evenements d usage partent vers un tiers,"
  echo "     et l egress DMZ les bloque en boucle (alerte « Beaconing suspect »)."
  echo "     Corriger : [analytics] enabled = false dans ~/.openjarvis/config.toml"
  echec=1
fi

# Le SDK anthropic manquant ne se voit qu'au demarrage suivant, par un message qui ne
# le nomme pas. On le controle donc explicitement.
if ssh_vm "cd $RACINE_VM && ./.venv/bin/python -c 'import anthropic'" 2>/dev/null; then
  echo "   ✓ SDK anthropic present"
else
  echo "   ✗ SDK anthropic ABSENT — Ava ne pourra pas parler"; echec=1
fi

if [ "$echec" -ne 0 ]; then
  printf '\n\033[1;31mDEPLOIEMENT INCOMPLET\033[0m — voir les lignes ✗ ci-dessus.\n'
  echo "Diagnostic d'un crash-loop : le journal systemd ne dit PAS pourquoi. Lancer a la main :"
  echo "  ssh $VM_JUMP \"ssh $VM 'cd $RACINE_VM && $UV run jarvis serve --host 127.0.0.1 --port 8002'\""
  exit 1
fi
printf '\n\033[1;32mDeploiement complet et verifie.\033[0m\n'
