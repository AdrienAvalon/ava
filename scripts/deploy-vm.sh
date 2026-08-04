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
  ssh_vm "cd $RACINE_VM && $UV pip install --no-deps -q '$WHEEL'"
  echo "   wheel : $(basename "$WHEEL")"
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
echec=0

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
