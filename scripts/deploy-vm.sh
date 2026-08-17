#!/usr/bin/env bash
# Livre un commit Ava comme une release immuable, puis bascule atomiquement le service.
#
# Le script est lance depuis un checkout local propre. Il ne fait ni pull, ni uv sync,
# ni build npm dans le checkout actuellement servi par la VM. Le code du commit local
# est transfere par `git archive` dans un repertoire neuf nomme par son SHA. La release
# ne devient active qu'apres les validations hors ligne et la bascule atomique de
# `AVA_CURRENT_LINK`.
#
# Avant la bascule, la cible de retour passe le meme contrat de persona/politique que
# la candidate, puis un dead-man persistant root-owned est arme sur la VM. Son garde
# systemd et son timer restaurent cette release precedente apres expiration, reboot
# ou pointeur inattendu, meme si cette session SSH disparait definitivement.
#
# L'extension Rust est obligatoire. `AVA_RUST_WHEEL` designe une wheel locale et
# `AVA_RUST_ATTESTATION` son attestation locale (par defaut `<wheel>.attestation`) :
#
#   format=ava-rust-wheel-attestation-v1
#   attestation_type=unsigned-checksum-manifest
#   signature=none
#   git_sha=<40 hexadecimal characters>
#   rust_tree_sha256=<git archive --format=tar <sha> rust | sha256sum>
#   wheel_sha256=<sha256sum de la wheel>
#   wheel_filename=<basename de la wheel>
#   builder_* / *_version / wheel_compatibility=<provenance explicite du builder>
#
# Ces valeurs sont controlees avant le premier SSH, puis les archives et la wheel sont
# recontrolees sur la VM avant toute installation. L'attestation doit etre produite par
# la chaine de build de confiance qui a construit la wheel ; ce script ne la fabrique
# jamais a partir d'un artefact arbitraire.
# Le meme transfert ajoute un `evolutions-v1.json` strict, derive du journal Git du
# commit avant tout SSH, pour que la release sans `.git` conserve son historique lisible.
set -Eeuo pipefail
umask 077

VM_JUMP="${AVA_JUMP:-avalon@192.168.2.40}"
VM="${AVA_VM:-avalon@192.168.100.15}"
LEGACY_ROOT="${AVA_RACINE:-/home/avalon/ava}"
RELEASE_ROOT="${AVA_RELEASE_ROOT:-/home/avalon/ava-releases}"
CURRENT_LINK="${AVA_CURRENT_LINK:-/home/avalon/ava-current}"
SERVICE="${AVA_SERVICE:-openjarvis}"
UV="${AVA_UV:-/home/avalon/.local/bin/uv}"
HEALTH_DELAY_SECONDS="${AVA_HEALTH_DELAY_SECONDS:-12}"
RELEASE_KEEP="${AVA_RELEASE_KEEP:-3}"
LOCK_STALE_SECONDS="${AVA_DEPLOY_LOCK_STALE_SECONDS:-21600}"
SSH_BIN="${AVA_SSH_BIN:-ssh}"
RACINE_LOCALE=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
RUST_WHEEL="${AVA_RUST_WHEEL:-}"
RUST_ATTESTATION="${AVA_RUST_ATTESTATION:-${RUST_WHEEL:+${RUST_WHEEL}.attestation}}"
RUST_BUILDER_DOCKERFILE='deploy/docker/Dockerfile.rust-builder'
RUST_BUILDER_PLATFORM='linux/amd64'
RUST_BUILDER_PYTHON_IMAGE='python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf'
RUST_BUILDER_RUST_IMAGE='rust:1.88.0-bookworm@sha256:4727898c104ecd2e22d780925832502faee9fe4e70581b8572af081370b315a0'
DEADMAN_HELPER='/usr/local/libexec/avalon/ava-deploy-deadman.py'
DEADMAN_TTL_SECONDS="${AVA_DEADMAN_TTL_SECONDS:-180}"
RELAY_CA="${AVA_RELAY_CA:-/etc/ssl/certs/avalon-internal-ca.crt}"
RELAY_TLS_NAME="${AVA_RELAY_TLS_NAME:-192.168.100.15}"
EVOLUTIONS_EXPORT_LIMIT=100
EVOLUTIONS_FILENAME='evolutions-v1.json'

# `uv sync` est exact : oublier inference-cloud retire le SDK Anthropic, et installer
# la wheel Rust avant le sync la fait supprimer. La wheel est donc installee ensuite.
EXTRAS=(server speech dashboard inference-cloud framework-comparison dev)

fatal() {
  printf 'ERREUR: %s\n' "$*" >&2
  exit 1
}

titre() {
  printf '\n\033[1m-- %s\033[0m\n' "$1"
}

remote_path_is_safe() {
  local path=$1
  [[ "$path" =~ ^/[A-Za-z0-9._/-]+$ ]] || return 1
  [[ "$path" != "/" && "$path" != "/home" && "$path" != "/home/avalon" ]] || return 1
  [[ "/$path/" != *"/../"* && "/$path/" != *"/./"* ]]
}

ssh_destination_is_safe() {
  local destination=$1
  [[ "$destination" =~ ^([A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$ ]]
}

for remote_path in "$LEGACY_ROOT" "$RELEASE_ROOT" "$CURRENT_LINK" "$UV" "$DEADMAN_HELPER" "$RELAY_CA"; do
  remote_path_is_safe "$remote_path" || fatal "chemin distant refuse: $remote_path"
  [[ "$remote_path" != */ && "$remote_path" != *//* ]] \
    || fatal "chemin distant non canonique: $remote_path"
done
[[ "$LEGACY_ROOT" != "$RELEASE_ROOT" && "$LEGACY_ROOT" != "$CURRENT_LINK" \
  && "$RELEASE_ROOT" != "$CURRENT_LINK" ]] || fatal "racines distantes non distinctes"
ssh_destination_is_safe "$VM_JUMP" || fatal "destination SSH jump invalide"
ssh_destination_is_safe "$VM" || fatal "destination SSH VM invalide"
[[ "$SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ ]] || fatal "nom de service invalide"
[[ "$HEALTH_DELAY_SECONDS" =~ ^[0-9]+$ ]] || fatal "delai de sante invalide"
[[ "$DEADMAN_TTL_SECONDS" =~ ^[0-9]+$ \
  && "$DEADMAN_TTL_SECONDS" -ge 60 && "$DEADMAN_TTL_SECONDS" -le 900 ]] \
  || fatal "delai dead-man invalide"
[[ "$LOCK_STALE_SECONDS" =~ ^[0-9]+$ \
  && "$LOCK_STALE_SECONDS" -ge 300 && "$LOCK_STALE_SECONDS" -le 86400 ]] \
  || fatal "age maximal du verrou de deploiement invalide"
[[ "$RELAY_TLS_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
  || fatal "nom TLS du relais invalide"
[[ "$RELEASE_KEEP" =~ ^[0-9]+$ && "$RELEASE_KEEP" -ge 2 && "$RELEASE_KEEP" -le 10 ]] \
  || fatal "retention des releases invalide"
[[ -n "$RUST_WHEEL" ]] || fatal "AVA_RUST_WHEEL est obligatoire"
[[ -f "$RUST_WHEEL" && ! -L "$RUST_WHEEL" ]] || fatal "wheel Rust locale absente ou non reguliere"
[[ -n "$RUST_ATTESTATION" && -f "$RUST_ATTESTATION" && ! -L "$RUST_ATTESTATION" ]] \
  || fatal "attestation Rust locale absente ou non reguliere"

WHEEL_FILENAME=$(basename -- "$RUST_WHEEL")
[[ "$WHEEL_FILENAME" =~ ^[A-Za-z0-9._-]+-cp312-cp312-manylinux_2_36_x86_64\.whl$ ]] \
  || fatal "nom ou compatibilite de wheel Rust invalide"

# Un commit est l'unite de livraison. Refuser toute difference locale avant le premier
# SSH empeche de construire un contenu qui ne correspond pas au SHA annonce.
if ! ETAT_GIT=$(git -C "$RACINE_LOCALE" status --porcelain=v1 --untracked-files=all); then
  fatal "impossible de verifier le checkout Ava local"
fi
if [[ -n "$ETAT_GIT" ]]; then
  echo "ERREUR: le checkout Ava local contient des modifications non commitees."
  echo "Committer ou retirer ces changements avant tout deploiement. Aucun acces distant n'a ete tente."
  exit 1
fi
unset ETAT_GIT

ATTENDU=$(git -C "$RACINE_LOCALE" rev-parse --verify HEAD)
[[ "$ATTENDU" =~ ^[0-9a-f]{40}$ ]] || fatal "SHA Git local inattendu"
git -C "$RACINE_LOCALE" cat-file -e "${ATTENDU}:rust" \
  || fatal "le commit ne contient pas le sous-arbre rust"
git -C "$RACINE_LOCALE" cat-file -e "${ATTENDU}:${RUST_BUILDER_DOCKERFILE}" \
  || fatal "le commit ne contient pas le Dockerfile du builder Rust"
for reserved_path in .ava-artifacts .ava-building .ava-ready .ava-release; do
  if git -C "$RACINE_LOCALE" cat-file -e "${ATTENDU}:${reserved_path}" 2>/dev/null; then
    fatal "le commit contient le chemin interne reserve ${reserved_path}"
  fi
done

SOURCE_TREE_SHA256=$(git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" \
  | sha256sum | awk '{print $1}')
RUST_TREE_SHA256=$(git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" rust \
  | sha256sum | awk '{print $1}')
RUST_BUILDER_DOCKERFILE_SHA256=$(git -C "$RACINE_LOCALE" show \
  "${ATTENDU}:${RUST_BUILDER_DOCKERFILE}" | sha256sum | awk '{print $1}')
WHEEL_SHA256=$(sha256sum -- "$RUST_WHEEL" | awk '{print $1}')
ATTESTATION_SHA256=$(sha256sum -- "$RUST_ATTESTATION" | awk '{print $1}')
for checksum in "$SOURCE_TREE_SHA256" "$RUST_TREE_SHA256" "$WHEEL_SHA256" "$ATTESTATION_SHA256"; do
  [[ "$checksum" =~ ^[0-9a-f]{64}$ ]] || fatal "checksum local de release invalide"
done

declare -A ATTESTATION=()
while IFS='=' read -r key value || [[ -n "${key:-}${value:-}" ]]; do
  case "$key" in
    format|attestation_type|signature|git_sha|rust_tree_sha256|wheel_sha256|wheel_filename|builder_image_id|builder_dockerfile_sha256|builder_platform|builder_python_image|builder_rust_image|python_version|rust_version|maturin_version|wheel_compatibility) ;;
    *) fatal "cle inconnue dans l'attestation Rust" ;;
  esac
  [[ -n "$value" ]] || fatal "valeur vide dans l'attestation Rust"
  [[ ! -v "ATTESTATION[$key]" ]] || fatal "cle dupliquee dans l'attestation Rust"
  ATTESTATION["$key"]=$value
done < "$RUST_ATTESTATION"

[[ ${#ATTESTATION[@]} -eq 16 ]] || fatal "attestation Rust incomplete"
[[ "${ATTESTATION[format]}" == "ava-rust-wheel-attestation-v1" ]] \
  || fatal "format d'attestation Rust inconnu"
[[ "${ATTESTATION[attestation_type]}" == "unsigned-checksum-manifest" \
  && "${ATTESTATION[signature]}" == "none" ]] \
  || fatal "niveau de confiance de l'attestation Rust ambigu"
[[ "${ATTESTATION[git_sha]}" =~ ^[0-9a-f]{40}$ ]] \
  || fatal "git_sha invalide dans l'attestation Rust"
[[ "${ATTESTATION[rust_tree_sha256]}" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "rust_tree_sha256 invalide dans l'attestation Rust"
[[ "${ATTESTATION[wheel_sha256]}" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "wheel_sha256 invalide dans l'attestation Rust"
[[ "${ATTESTATION[wheel_filename]}" =~ ^[A-Za-z0-9._-]+\.whl$ ]] \
  || fatal "wheel_filename invalide dans l'attestation Rust"
[[ "${ATTESTATION[builder_image_id]}" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || fatal "builder_image_id invalide dans l'attestation Rust"
[[ "${ATTESTATION[builder_dockerfile_sha256]}" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "builder_dockerfile_sha256 invalide dans l'attestation Rust"
[[ "${ATTESTATION[git_sha]}" == "$ATTENDU" ]] \
  || fatal "la wheel Rust n'est pas attestee pour le commit local"
[[ "${ATTESTATION[rust_tree_sha256]}" == "$RUST_TREE_SHA256" ]] \
  || fatal "l'attestation ne correspond pas au sous-arbre rust du commit"
[[ "${ATTESTATION[wheel_sha256]}" == "$WHEEL_SHA256" ]] \
  || fatal "le SHA-256 de la wheel Rust ne correspond pas a l'attestation"
[[ "${ATTESTATION[wheel_filename]}" == "$WHEEL_FILENAME" ]] \
  || fatal "le nom de wheel Rust ne correspond pas a l'attestation"
[[ "${ATTESTATION[builder_dockerfile_sha256]}" == "$RUST_BUILDER_DOCKERFILE_SHA256" ]] \
  || fatal "l'attestation ne correspond pas au Dockerfile du builder Rust"
[[ "${ATTESTATION[builder_platform]}" == "$RUST_BUILDER_PLATFORM" \
  && "${ATTESTATION[builder_python_image]}" == "$RUST_BUILDER_PYTHON_IMAGE" \
  && "${ATTESTATION[builder_rust_image]}" == "$RUST_BUILDER_RUST_IMAGE" \
  && "${ATTESTATION[python_version]}" == "3.12.13" \
  && "${ATTESTATION[rust_version]}" == "1.88.0" \
  && "${ATTESTATION[maturin_version]}" == "1.14.1" \
  && "${ATTESTATION[wheel_compatibility]}" == "manylinux_2_36_x86_64" ]] \
  || fatal "provenance ou compatibilite du builder Rust inattendue"

# Une release `git archive` ne contient pas `.git`. Exporter avant le premier SSH un
# journal strict permet a l'outil evolutions de conserver une source locale bornee sans
# remettre un checkout mutable sur la VM.
command -v python3 >/dev/null 2>&1 || fatal "python3 local introuvable"
LOCAL_TMP=$(mktemp -d "${TMPDIR:-/tmp}/ava-deploy.XXXXXX")
early_cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  rm -rf -- "$LOCAL_TMP"
  exit "$status"
}
trap early_cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
LOCAL_EVOLUTIONS="$LOCAL_TMP/$EVOLUTIONS_FILENAME"
python3 - "$RACINE_LOCALE" "$ATTENDU" "$EVOLUTIONS_EXPORT_LIMIT" "$LOCAL_EVOLUTIONS" <<'PY'
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

root, git_sha, raw_limit, destination = sys.argv[1:]
limit = int(raw_limit)
if not 1 <= limit <= 500 or len(git_sha) != 40 or any(c not in "0123456789abcdef" for c in git_sha):
    raise SystemExit("contrat d'export evolutions invalide")
result = subprocess.run(
    [
        "git",
        "-C",
        root,
        "log",
        "-z",
        f"-{limit + 1}",
        "--date=short",
        "--pretty=format:%ad%x00%s%x00%b",
        git_sha,
    ],
    check=True,
    stdout=subprocess.PIPE,
)
fields = result.stdout.decode("utf-8", errors="replace").split("\x00")
if fields == [""]:
    fields = []
if len(fields) % 3:
    raise SystemExit("sortie git log evolutions mal formee")
rows = [fields[index : index + 3] for index in range(0, len(fields), 3)]
truncated = len(rows) > limit
entries = []
for day, subject, body in rows[:limit]:
    datetime.date.fromisoformat(day)
    entries.append(
        {
            "body": body[:20_000],
            "date": day,
            "subject": subject[:500],
        }
    )


def encode() -> bytes:
    document = {
        "entries": entries,
        "git_sha": git_sha,
        "schema": 1,
        "truncated": truncated,
    }
    return (json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


payload = encode()
while len(payload) > 1_500_000 and entries:
    entries.pop()
    truncated = True
    payload = encode()
if not entries or len(payload) > 1_500_000:
    raise SystemExit("export evolutions vide ou trop volumineux")
target = Path(destination)
descriptor = os.open(
    target,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
    0o600,
)
try:
    with os.fdopen(descriptor, "wb", closefd=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
finally:
    os.close(descriptor)
PY
EVOLUTIONS_SHA256=$(sha256sum -- "$LOCAL_EVOLUTIONS" | awk '{print $1}')
[[ "$EVOLUTIONS_SHA256" =~ ^[0-9a-f]{64}$ ]] || fatal "SHA-256 evolutions invalide"

# Execute une commande sur la VM via le seul chemin autorise, le jump host AVA.
# Le dernier argument est quote comme un mot shell unique avant de traverser les deux
# shells SSH. Aucun chemin ou nom non valide n'est injecte dans ces commandes.
ssh_vm() {
  local command=$1 quoted_vm quoted_command
  printf -v quoted_vm '%q' "$VM"
  printf -v quoted_command '%q' "$command"
  "$SSH_BIN" -o BatchMode=yes -o LogLevel=ERROR "$VM_JUMP" \
    "ssh -o BatchMode=yes -o LogLevel=ERROR $quoted_vm $quoted_command"
}

quote_remote() {
  printf '%q' "$1"
}

# La configuration persistante ne doit jamais decoupler la persona du code livre.
# Avec un override vide, le loader resout sa persona depuis son propre paquet : une
# bascule ou un rollback du lien current change donc code et identite d'un seul tenant.
validate_runtime_policy() {
  local expected_target=$1 q_expected_target
  q_expected_target=$(quote_remote "$expected_target")
  ssh_vm "set -eu
export OPENJARVIS_NO_ANALYTICS=1 DO_NOT_TRACK=1 AVA_BUNDLED_PERSONA_ONLY=1 AVA_PERCEPTION=0 PYTHONDONTWRITEBYTECODE=1
cd /tmp
AVA_EXPECTED_RELEASE=$q_expected_target $q_expected_target/.venv/bin/python -I -c \"import os; from pathlib import Path; from ava_extensions.patches.system_prompt_loader import _DEFAULT_PERSONA; from openjarvis.analytics.identity import is_analytics_enabled; from openjarvis.core.config import load_config; root=Path(os.environ['AVA_EXPECTED_RELEASE']).resolve(); persona=_DEFAULT_PERSONA.resolve(); config=load_config(); assert config.agent.system_prompt_path == ''; assert persona.is_relative_to(root); assert config.agent.default_system_prompt == persona.read_text(encoding='utf-8').strip(); assert not is_analytics_enabled(config.analytics)\""
}

previous_target_is_safe() {
  local target=$1 suffix q_target
  if [[ "$target" == "$LEGACY_ROOT" ]]; then
    q_target=$(quote_remote "$target")
    ssh_vm "set -eu
test -d $q_target
test ! -L $q_target
test -x $q_target/.venv/bin/jarvis
test -s $q_target/src/openjarvis/server/static/index.html"
    return
  fi
  [[ "$target" == "$RELEASE_ROOT/"* ]] || return 1
  suffix=${target#"$RELEASE_ROOT/"}
  [[ "$suffix" =~ ^[0-9a-f]{40}$ ]] || return 1
  q_target=$(quote_remote "$target")
  ssh_vm "set -eu
test -d $q_target
test ! -L $q_target
test -f $q_target/.ava-ready
test ! -L $q_target/.ava-ready
test -f $q_target/.ava-release
test ! -L $q_target/.ava-release
grep -qx 'format=ava-release-v1' $q_target/.ava-release
grep -qx 'git_sha=$suffix' $q_target/.ava-release
test \"\$(wc -l < $q_target/.ava-release)\" -eq 8
test \"\$(cat -- $q_target/.ava-ready)\" = '$suffix'
grep -Eq '^source_tree_sha256=[0-9a-f]{64}$' $q_target/.ava-release
grep -Eq '^rust_tree_sha256=[0-9a-f]{64}$' $q_target/.ava-release
grep -Eq '^wheel_sha256=[0-9a-f]{64}$' $q_target/.ava-release
grep -Eq '^attestation_sha256=[0-9a-f]{64}$' $q_target/.ava-release
grep -Eq '^evolutions_sha256=[0-9a-f]{64}$' $q_target/.ava-release
grep -Eq '^wheel_filename=[A-Za-z0-9._-]+-cp312-cp312-manylinux_2_36_x86_64\.whl$' $q_target/.ava-release
test -x $q_target/.venv/bin/jarvis
test -s $q_target/src/openjarvis/server/static/index.html"
}

RELEASE_PATH="${RELEASE_ROOT}/${ATTENDU}"
LOCK_PATH="${RELEASE_ROOT}/.deploy-lock"
ARTIFACT_DIR="${RELEASE_PATH}/.ava-artifacts"
REMOTE_WHEEL="${ARTIFACT_DIR}/${WHEEL_FILENAME}"
REMOTE_ATTESTATION="${ARTIFACT_DIR}/${WHEEL_FILENAME}.attestation"
REMOTE_SOURCE_ARCHIVE="${ARTIFACT_DIR}/source-tree.tar"
REMOTE_RUST_ARCHIVE="${ARTIFACT_DIR}/rust-tree.tar"
REMOTE_EVOLUTIONS="${ARTIFACT_DIR}/${EVOLUTIONS_FILENAME}"
CURRENT_PARENT=$(dirname -- "$CURRENT_LINK")

Q_RELEASE_ROOT=$(quote_remote "$RELEASE_ROOT")
Q_RELEASE=$(quote_remote "$RELEASE_PATH")
Q_LOCK=$(quote_remote "$LOCK_PATH")
Q_CURRENT=$(quote_remote "$CURRENT_LINK")
Q_CURRENT_PARENT=$(quote_remote "$CURRENT_PARENT")
Q_ARTIFACT_DIR=$(quote_remote "$ARTIFACT_DIR")
Q_REMOTE_WHEEL=$(quote_remote "$REMOTE_WHEEL")
Q_REMOTE_ATTESTATION=$(quote_remote "$REMOTE_ATTESTATION")
Q_REMOTE_SOURCE_ARCHIVE=$(quote_remote "$REMOTE_SOURCE_ARCHIVE")
Q_REMOTE_RUST_ARCHIVE=$(quote_remote "$REMOTE_RUST_ARCHIVE")
Q_REMOTE_EVOLUTIONS=$(quote_remote "$REMOTE_EVOLUTIONS")
Q_UV=$(quote_remote "$UV")
Q_SERVICE=$(quote_remote "$SERVICE")
Q_DEADMAN_HELPER=$(quote_remote "$DEADMAN_HELPER")
Q_RELAY_CA=$(quote_remote "$RELAY_CA")

LOCK_HELD=0
RELEASE_OWNED=0
SWITCHED=0
DEADMAN_ARMED=0
PREVIOUS_TARGET=""
BOOTSTRAP_WITHOUT_PREVIOUS=0
INITIALIZE_CURRENT_FROM_LEGACY=0
KEEP_FAILED_CANDIDATE=0

atomic_link() {
  local target=$1 q_target
  q_target=$(quote_remote "$target")
  ssh_vm "set -eu
tmp=${Q_CURRENT}.next.\$\$
trap 'rm -f -- \"\$tmp\"' EXIT HUP INT TERM
ln -s -- $q_target \"\$tmp\"
mv -Tf -- \"\$tmp\" $Q_CURRENT
trap - EXIT HUP INT TERM"
}

arm_deadman() {
  local q_previous result arm_mode
  if [[ "$BOOTSTRAP_WITHOUT_PREVIOUS" -eq 1 ]]; then
    arm_mode='--bootstrap'
  else
    q_previous=$(quote_remote "$PREVIOUS_TARGET")
    arm_mode="--previous $q_previous"
  fi
  result=$(ssh_vm "sudo $Q_DEADMAN_HELPER arm $arm_mode --candidate $Q_RELEASE --ttl $DEADMAN_TTL_SECONDS") \
    || fatal "impossible d'armer le dead-man distant"
  [[ "$result" == "armed" ]] || fatal "reponse d'armement dead-man inattendue"
  DEADMAN_ARMED=1
}

confirm_deadman() {
  local result
  result=$(ssh_vm "sudo $Q_DEADMAN_HELPER confirm --candidate $Q_RELEASE") \
    || fatal "impossible de confirmer le dead-man distant"
  if [[ "$DEADMAN_ARMED" -eq 1 ]]; then
    [[ "$result" == "confirmed" ]] || fatal "confirmation dead-man perdue"
  else
    [[ "$result" == "confirmed" || "$result" == "not-armed" ]] \
      || fatal "etat dead-man inattendu pour une release deja active"
  fi
  DEADMAN_ARMED=0
}

cancel_deadman() {
  local result
  [[ "$DEADMAN_ARMED" -eq 1 ]] || return 0
  result=$(ssh_vm "sudo $Q_DEADMAN_HELPER cancel --candidate $Q_RELEASE") || return 1
  [[ "$result" == "cancelled" || "$result" == "not-armed" ]] || return 1
  DEADMAN_ARMED=0
}

acquire_deployment_lock() {
  local result
  ssh_vm "set -eu
mkdir -p -- $Q_RELEASE_ROOT $Q_CURRENT_PARENT
test -d $Q_RELEASE_ROOT
test ! -L $Q_RELEASE_ROOT"
  if ! ssh_vm "mkdir -m 700 -- $Q_LOCK"; then
    result=$(ssh_vm "sudo $Q_DEADMAN_HELPER reclaim-lock --stale-after $LOCK_STALE_SECONDS" 2>/dev/null || true)
    case "$result" in
      reclaimed|not-locked)
        ssh_vm "mkdir -m 700 -- $Q_LOCK" \
          || fatal "verrou Ava repris concurremment apres reclamation"
        ;;
      fresh)
        fatal "un deploiement Ava recent detient deja le verrou"
        ;;
      *)
        fatal "verrou Ava existant non reclamable ou dead-man encore arme"
        ;;
    esac
  fi
  LOCK_HELD=1
  if ssh_vm "find $Q_RELEASE_ROOT -mindepth 1 -maxdepth 1 -type d -name '.backup-use.*' -print -quit | grep -q ."; then
    ssh_vm "rmdir -- $Q_LOCK" || true
    LOCK_HELD=0
    fatal "une sauvegarde utilise actuellement les releases Ava"
  fi
}

health_check() {
  local expected_target=${1:-$RELEASE_PATH}
  local service_state relay_state tls_relay_state code_direct code_relay code_tls
  local code_engine_direct code_engine_relay code_engine_tls active_target main_pid main_cwd
  service_state=$(ssh_vm "systemctl is-active $Q_SERVICE" 2>/dev/null || true)
  [[ "$service_state" == "active" ]] || {
    echo "   x service $SERVICE inactif (${service_state:-inconnu})" >&2
    return 1
  }
  relay_state=$(ssh_vm "systemctl is-active openjarvis-relay.service" 2>/dev/null || true)
  tls_relay_state=$(ssh_vm "systemctl is-active openjarvis-relay-tls.service" 2>/dev/null || true)
  [[ "$relay_state" == "active" && "$tls_relay_state" == "active" ]] || {
    echo "   x relais Ava inactifs (8080=${relay_state:-inconnu}, 8443=${tls_relay_state:-inconnu})" >&2
    return 1
  }
  active_target=$(ssh_vm "readlink -f -- $Q_CURRENT" 2>/dev/null || true)
  [[ "$active_target" == "$expected_target" ]] || {
    echo "   x current ne pointe pas sur la release attendue" >&2
    return 1
  }
  main_pid=$(ssh_vm "systemctl show $Q_SERVICE -p MainPID --value" 2>/dev/null || true)
  [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || {
    echo "   x PID principal du service introuvable" >&2
    return 1
  }
  main_cwd=$(ssh_vm "readlink -f -- /proc/$main_pid/cwd" 2>/dev/null || true)
  [[ "$main_cwd" == "$expected_target" ]] || {
    echo "   x le processus actif ne sert pas la release attendue" >&2
    return 1
  }
  code_direct=$(ssh_vm "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8000/" 2>/dev/null || true)
  [[ "$code_direct" == "200" ]] || {
    echo "   x endpoint local 8000: HTTP ${code_direct:-indisponible}" >&2
    return 1
  }
  code_relay=$(ssh_vm "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8080/" 2>/dev/null || true)
  [[ "$code_relay" == "200" ]] || {
    echo "   x relay 8080: HTTP ${code_relay:-indisponible}" >&2
    return 1
  }
  code_tls=$(ssh_vm "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 --cacert $Q_RELAY_CA --resolve '${RELAY_TLS_NAME}:8443:127.0.0.1' 'https://${RELAY_TLS_NAME}:8443/'" 2>/dev/null || true)
  [[ "$code_tls" == "200" ]] || {
    echo "   x relay TLS 8443: HTTP ${code_tls:-indisponible}" >&2
    return 1
  }
  code_engine_direct=$(ssh_vm "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8000/health" 2>/dev/null || true)
  [[ "$code_engine_direct" == "200" ]] || {
    echo "   x moteur local /health: HTTP ${code_engine_direct:-indisponible}" >&2
    return 1
  }
  code_engine_relay=$(ssh_vm "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8080/health" 2>/dev/null || true)
  [[ "$code_engine_relay" == "200" ]] || {
    echo "   x moteur relay /health: HTTP ${code_engine_relay:-indisponible}" >&2
    return 1
  }
  code_engine_tls=$(ssh_vm "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 --cacert $Q_RELAY_CA --resolve '${RELAY_TLS_NAME}:8443:127.0.0.1' 'https://${RELAY_TLS_NAME}:8443/health'" 2>/dev/null || true)
  [[ "$code_engine_tls" == "200" ]] || {
    echo "   x moteur relay TLS /health: HTTP ${code_engine_tls:-indisponible}" >&2
    return 1
  }
  ssh_vm "set -eu
export OPENJARVIS_NO_ANALYTICS=1 DO_NOT_TRACK=1 PYTHONDONTWRITEBYTECODE=1
cd /tmp
$Q_CURRENT/.venv/bin/python -I -c \"import anthropic, openjarvis_rust; from openjarvis._rust_bridge import get_rust_module; get_rust_module()\"" >/dev/null
  validate_runtime_policy "$expected_target" || {
    echo "   x persona ou politique runtime hors de la release attendue" >&2
    return 1
  }
}

garbage_collect_releases() {
  local q_previous
  q_previous=$(quote_remote "$PREVIOUS_TARGET")
  ssh_vm "set -eu
active=\$(readlink -f -- $Q_CURRENT)
previous=$q_previous
kept=0
find $Q_RELEASE_ROOT -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\\n' \
  | sort -rn \
  | while IFS=' ' read -r _ path; do
      name=\${path##*/}
      case \"\$name\" in
        *[!0-9a-f]*|'') continue ;;
      esac
      [ \"\${#name}\" -eq 40 ] || continue
      [ -f \"\$path/.ava-ready\" ] || continue
      [ -f \"\$path/.ava-release\" ] || continue
      if [ \"\$path\" = \"\$active\" ] || [ \"\$path\" = \"\$previous\" ]; then
        continue
      fi
      kept=\$((kept + 1))
      if [ \"\$kept\" -gt $RELEASE_KEEP ]; then
        chmod -R u+w -- \"\$path\"
        rm -rf -- \"\$path\"
      fi
    done"
}

# La retention est une maintenance post-deploiement : elle ne doit etre appelee
# qu'apres la sante et la confirmation du dead-man. A ce stade, son echec ne rend
# pas la release active malsaine et ne doit donc ni simuler un echec de livraison
# ni provoquer un rollback. Le warning conserve l'echec visible pour intervention.
garbage_collect_releases_after_confirmation() {
  if garbage_collect_releases; then
    return 0
  fi
  printf '%s\n' \
    "   ! AVERTISSEMENT: release active et saine conservee; retention des anciennes releases en echec, aucun rollback (nettoyage manuel requis)" >&2 \
    || true
  return 0
}

rollback() {
  local result
  if [[ "$BOOTSTRAP_WITHOUT_PREVIOUS" -eq 1 ]]; then
    echo "   ! echec du premier demarrage : retour a aucun service actif" >&2
    result=$(ssh_vm "sudo $Q_DEADMAN_HELPER rollback --candidate $Q_RELEASE" 2>/dev/null || true)
    if [[ "$result" == "bootstrap-stopped" ]]; then
      # Un retour positif du helper signifie que son etat root-owned ET le verrou
      # ont deja ete retires. Les drapeaux locaux doivent le refleter avant tout
      # controle supplementaire, meme si celui-ci echoue ensuite.
      DEADMAN_ARMED=0
      LOCK_HELD=0
      if ! ssh_vm "test -e $Q_CURRENT" \
        && [[ "$(ssh_vm "systemctl is-active $Q_SERVICE" 2>/dev/null || true)" =~ ^(failed|inactive)$ ]] \
        && [[ "$(ssh_vm "systemctl is-active openjarvis-relay.service" 2>/dev/null || true)" =~ ^(failed|inactive)$ ]] \
        && [[ "$(ssh_vm "systemctl is-active openjarvis-relay-tls.service" 2>/dev/null || true)" =~ ^(failed|inactive)$ ]]; then
        echo "   + bootstrap replie; candidate conservee pour diagnostic: $RELEASE_PATH" >&2
      else
        echo "   x CRITIQUE: bootstrap replie mais etat arrete non confirme; candidate conservee sans dead-man ni verrou" >&2
      fi
    else
      echo "   x CRITIQUE: bootstrap non confirme; le dead-man distant reste autoritaire" >&2
    fi
    SWITCHED=0
    return
  fi
  echo "   ! echec apres bascule : restauration de $PREVIOUS_TARGET" >&2
  result=$(ssh_vm "sudo $Q_DEADMAN_HELPER rollback --candidate $Q_RELEASE" 2>/dev/null || true)
  if [[ "$result" == "rolled-back" ]]; then
    # Comme pour le bootstrap, le helper ne renvoie rolled-back qu'apres avoir
    # supprime son etat et le verrou. Ne jamais annoncer ensuite qu'ils existent.
    DEADMAN_ARMED=0
    LOCK_HELD=0
    sleep "$HEALTH_DELAY_SECONDS"
    # Le helper n'efface son etat qu'apres les trois chemins HTTP. Le controle local
    # ajoute le PID/cwd, l'import Rust et la telemetrie pour ne jamais annoncer un
    # rollback sain sur la seule reponse de systemd.
    if health_check "$PREVIOUS_TARGET"; then
      echo "   + rollback confirme sur la cible precedente" >&2
    else
      KEEP_FAILED_CANDIDATE=1
      echo "   x CRITIQUE: cible precedente restauree mais sante etendue non confirmee; candidate conservee sans dead-man ni verrou" >&2
    fi
  else
    echo "   x CRITIQUE: rollback distant non confirme; le dead-man distant reste autoritaire" >&2
  fi
  SWITCHED=0
}

cleanup() {
  local status=$? current_target
  trap - EXIT HUP INT TERM
  set +e
  if [[ "$status" -ne 0 && "$SWITCHED" -eq 1 ]]; then
    rollback
  fi
  if [[ "$status" -ne 0 && "$RELEASE_OWNED" -eq 1 \
    && "$KEEP_FAILED_CANDIDATE" -eq 0 && "$DEADMAN_ARMED" -eq 0 ]]; then
    current_target=$(ssh_vm "readlink -f -- $Q_CURRENT" 2>/dev/null || true)
    if [[ "$current_target" != "$RELEASE_PATH" ]]; then
      # RELEASE_PATH est toujours RELEASE_ROOT/<SHA-40> et les deux composants ont ete
      # valides avant le premier SSH. Ne jamais elargir cette suppression.
      ssh_vm "chmod -R u+w -- $Q_RELEASE 2>/dev/null || true; rm -rf -- $Q_RELEASE" || true
    else
      echo "   x release en echec encore active; suppression refusee" >&2
    fi
  fi
  if [[ "$LOCK_HELD" -eq 1 && "$DEADMAN_ARMED" -eq 0 ]]; then
    ssh_vm "rmdir -- $Q_LOCK" >/dev/null 2>&1 || true
  elif [[ "$LOCK_HELD" -eq 1 ]]; then
    echo "   ! verrou et candidate conserves : le dead-man distant reste arme" >&2
  fi
  rm -rf -- "$LOCAL_TMP"
  exit "$status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

titre "1/7 Verrou et bootstrap du pointeur current"
acquire_deployment_lock

# Le bootstrap Ansible doit avoir installe le helper, le timer persistant et l'unite
# OpenJarvis avant que ce script accepte de creer une premiere cible. Une session SSH
# n'est jamais elle-meme le mecanisme de rollback.
ssh_vm "set -eu
test \"\$(sudo $Q_DEADMAN_HELPER probe)\" = ready
systemctl is-enabled --quiet ava-deploy-deadman.timer
systemctl is-active --quiet ava-deploy-deadman.timer
systemctl is-enabled --quiet $Q_SERVICE
systemctl is-enabled --quiet openjarvis-relay.service
systemctl is-enabled --quiet openjarvis-relay-tls.service
test -r $Q_RELAY_CA" \
  || fatal "garde dead-man ou unite OpenJarvis non convergee par Ansible"

if ssh_vm "test -L $Q_CURRENT"; then
  PREVIOUS_TARGET=$(ssh_vm "readlink -f -- $Q_CURRENT")
elif ssh_vm "test -e $Q_CURRENT"; then
  fatal "$CURRENT_LINK existe mais n'est pas un lien symbolique"
elif previous_target_is_safe "$LEGACY_ROOT"; then
  # Ne pas creer `current` tant que le checkout legacy n'est pas une cible de
  # rollback conforme. Cette premiere validation evite aussi de construire une
  # candidate qui ne pourrait jamais etre livree en securite.
  PREVIOUS_TARGET="$LEGACY_ROOT"
  INITIALIZE_CURRENT_FROM_LEGACY=1
else
  service_state=$(ssh_vm "systemctl is-active $Q_SERVICE" 2>/dev/null || true)
  [[ "$service_state" =~ ^(failed|inactive)$ ]] \
    || fatal "premiere livraison refusee tant que $SERVICE n'est pas arrete"
  BOOTSTRAP_WITHOUT_PREVIOUS=1
  echo "   + premiere livraison sans previous; service confirme arrete"
fi
if [[ "$BOOTSTRAP_WITHOUT_PREVIOUS" -eq 0 ]]; then
  [[ -n "$PREVIOUS_TARGET" ]] && remote_path_is_safe "$PREVIOUS_TARGET" \
    || fatal "cible precedente de current invalide"
  previous_target_is_safe "$PREVIOUS_TARGET" \
    || fatal "cible precedente de current hors contrat Ava"
  validate_runtime_policy "$PREVIOUS_TARGET" \
    || fatal "cible precedente de current hors politique runtime Ava"
  if [[ "$INITIALIZE_CURRENT_FROM_LEGACY" -eq 1 ]]; then
    atomic_link "$PREVIOUS_TARGET"
    [[ "$(ssh_vm "readlink -f -- $Q_CURRENT")" == "$PREVIOUS_TARGET" ]] \
      || fatal "initialisation de current vers le checkout legacy non confirmee"
    echo "   + current initialise vers le checkout legacy conforme"
  fi
  [[ "$PREVIOUS_TARGET" != "$RELEASE_PATH" ]] \
    || echo "   + la release demandee est deja la cible active"
fi

titre "2/7 Preparation de la release ${ATTENDU:0:12}"
expected_manifest=$(printf '%s\n' \
  'format=ava-release-v1' \
  "git_sha=$ATTENDU" \
  "source_tree_sha256=$SOURCE_TREE_SHA256" \
  "rust_tree_sha256=$RUST_TREE_SHA256" \
  "wheel_sha256=$WHEEL_SHA256" \
  "wheel_filename=$WHEEL_FILENAME" \
  "attestation_sha256=$ATTESTATION_SHA256" \
  "evolutions_sha256=$EVOLUTIONS_SHA256")
if ssh_vm "test -e $Q_RELEASE"; then
  if ! ssh_vm "test -d $Q_RELEASE && test ! -L $Q_RELEASE"; then
    fatal "chemin de release existant non regulier ou symbolique"
  fi
  if ssh_vm "test -f $Q_RELEASE/.ava-ready"; then
    if ! ssh_vm "set -eu
test -d $Q_RELEASE
test ! -L $Q_RELEASE
for path in $Q_RELEASE/.ava-ready $Q_RELEASE/.ava-release $Q_REMOTE_SOURCE_ARCHIVE $Q_REMOTE_RUST_ARCHIVE $Q_REMOTE_WHEEL $Q_REMOTE_ATTESTATION $Q_REMOTE_EVOLUTIONS; do
  test -f \"\$path\"
  test ! -L \"\$path\"
done
test \"\$(cat -- $Q_RELEASE/.ava-ready)\" = $ATTENDU
if find $Q_RELEASE -xdev -perm /0222 -print -quit | grep -q .; then exit 76; fi"; then
      fatal "release same-SHA incomplete, symbolique ou redevenue modifiable"
    fi
    manifest=$(ssh_vm "cat -- $Q_RELEASE/.ava-release")
    [[ "$manifest" == "$expected_manifest" ]] \
      || fatal "release existante immutable mais manifeste divergent"
    remote_source_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_SOURCE_ARCHIVE | awk '{print \$1}'")
    remote_rust_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_RUST_ARCHIVE | awk '{print \$1}'")
    remote_wheel_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_WHEEL | awk '{print \$1}'")
    remote_attestation_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_ATTESTATION | awk '{print \$1}'")
    remote_evolutions_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_EVOLUTIONS | awk '{print \$1}'")
    [[ "$remote_source_hash" == "$SOURCE_TREE_SHA256" \
      && "$remote_rust_hash" == "$RUST_TREE_SHA256" \
      && "$remote_wheel_hash" == "$WHEEL_SHA256" \
      && "$remote_attestation_hash" == "$ATTESTATION_SHA256" \
      && "$remote_evolutions_hash" == "$EVOLUTIONS_SHA256" ]] \
      || fatal "artefact d'une release same-SHA divergent"
    echo "   + release immutable existante reutilisee"
  else
    # Un repertoire sans marqueur ready est un staging abandonne sous le verrou
    # exclusif. Il ne peut pas etre actif : le verifier avant sa suppression ciblee.
    [[ "$PREVIOUS_TARGET" != "$RELEASE_PATH" ]] \
      || fatal "release incomplete actuellement referencee par current"
    ssh_vm "chmod -R u+w -- $Q_RELEASE 2>/dev/null || true; rm -rf -- $Q_RELEASE"
  fi
fi

if ! ssh_vm "test -f $Q_RELEASE/.ava-ready"; then
  ssh_vm "set -eu
mkdir -- $Q_RELEASE
mkdir -- $Q_ARTIFACT_DIR
printf '%s\n' $ATTENDU > $Q_RELEASE/.ava-building"
  RELEASE_OWNED=1

  titre "3/7 Transfert source et artefacts attestes"
  git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" \
    | ssh_vm "cat > $Q_REMOTE_SOURCE_ARCHIVE"
  git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" rust \
    | ssh_vm "cat > $Q_REMOTE_RUST_ARCHIVE"
  cat -- "$RUST_WHEEL" | ssh_vm "cat > $Q_REMOTE_WHEEL"
  cat -- "$RUST_ATTESTATION" | ssh_vm "cat > $Q_REMOTE_ATTESTATION"
  cat -- "$LOCAL_EVOLUTIONS" | ssh_vm "cat > $Q_REMOTE_EVOLUTIONS"

  remote_source_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_SOURCE_ARCHIVE | awk '{print \$1}'")
  remote_rust_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_RUST_ARCHIVE | awk '{print \$1}'")
  remote_wheel_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_WHEEL | awk '{print \$1}'")
  remote_attestation_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_ATTESTATION | awk '{print \$1}'")
  remote_evolutions_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_EVOLUTIONS | awk '{print \$1}'")
  [[ "$remote_source_hash" == "$SOURCE_TREE_SHA256" ]] \
    || fatal "archive source alteree pendant le transfert"
  [[ "$remote_rust_hash" == "$RUST_TREE_SHA256" ]] \
    || fatal "archive rust alteree pendant le transfert"
  [[ "$remote_wheel_hash" == "$WHEEL_SHA256" ]] \
    || fatal "wheel Rust alteree pendant le transfert"
  [[ "$remote_attestation_hash" == "$ATTESTATION_SHA256" ]] \
    || fatal "attestation Rust alteree pendant le transfert"
  [[ "$remote_evolutions_hash" == "$EVOLUTIONS_SHA256" ]] \
    || fatal "export evolutions altere pendant le transfert"
  ssh_vm "tar -xf $Q_REMOTE_SOURCE_ARCHIVE -C $Q_RELEASE"
  echo "   + source, sous-arbre rust, wheel et journal evolutions verifies avant installation"

  titre "4/7 Construction isolee Python et frontend"
  EXTRA_ARGS=()
  for extra in "${EXTRAS[@]}"; do
    EXTRA_ARGS+=(--extra "$extra")
  done
  printf -v Q_EXTRA_ARGS ' %q' "${EXTRA_ARGS[@]}"
  ssh_vm "set -eu
cd $Q_RELEASE/frontend
npm ci --silent
npm run build
rm -rf -- node_modules
cd $Q_RELEASE
$Q_UV sync --frozen$Q_EXTRA_ARGS
$Q_UV pip install --python .venv/bin/python --no-deps $Q_REMOTE_WHEEL"

  titre "5/7 Validations hors ligne avant bascule"
  ssh_vm "set -eu
export OPENJARVIS_NO_ANALYTICS=1 DO_NOT_TRACK=1 PYTHONDONTWRITEBYTECODE=1
cd /tmp
AVA_EXPECTED_RELEASE=$Q_RELEASE $Q_RELEASE/.venv/bin/python -I -c \"import os; from pathlib import Path; import anthropic, ava_extensions, openjarvis, openjarvis_rust; from openjarvis._rust_bridge import get_rust_module; root=Path(os.environ['AVA_EXPECTED_RELEASE']).resolve(); assert Path(openjarvis.__file__).resolve().is_relative_to(root); assert Path(ava_extensions.__file__).resolve().is_relative_to(root); get_rust_module()\"
AVA_EXPECTED_RELEASE=$Q_RELEASE $Q_RELEASE/.venv/bin/python -I -c \"import ava_extensions.boot; from openjarvis.core.registry import ToolRegistry, TTSRegistry, SpeechRegistry; expected_tools={'avalon_status','home_assistant'}; missing=expected_tools-set(ToolRegistry.keys()); assert not missing, missing; assert 'memoire' not in ToolRegistry.keys(); assert 'kokoro-fr' in TTSRegistry.keys(); assert 'openai_ava' in SpeechRegistry.keys()\"
AVA_RELEASE_HISTORY_FILE=$Q_REMOTE_EVOLUTIONS $Q_RELEASE/.venv/bin/python -I -c \"from ava_extensions.skills.evolutions import _lignes_git; assert _lignes_git('', 3) is not None\"
$Q_RELEASE/.venv/bin/jarvis --help >/dev/null
cd $Q_RELEASE
$Q_RELEASE/.venv/bin/python -m pytest -q -p no:cacheprovider tests/deployment/test_packaging.py
test -s src/openjarvis/server/static/index.html
test \"\$(sha256sum -- $Q_REMOTE_SOURCE_ARCHIVE | awk '{print \$1}')\" = $SOURCE_TREE_SHA256
test \"\$(sha256sum -- $Q_REMOTE_WHEEL | awk '{print \$1}')\" = $WHEEL_SHA256
test \"\$(sha256sum -- $Q_REMOTE_ATTESTATION | awk '{print \$1}')\" = $ATTESTATION_SHA256
test \"\$(sha256sum -- $Q_REMOTE_RUST_ARCHIVE | awk '{print \$1}')\" = $RUST_TREE_SHA256
test \"\$(sha256sum -- $Q_REMOTE_EVOLUTIONS | awk '{print \$1}')\" = $EVOLUTIONS_SHA256"

  validate_runtime_policy "$RELEASE_PATH" \
    || fatal "persona ou politique runtime hors de la candidate immuable"

  printf '%s\n' "$expected_manifest" | ssh_vm "cat > $Q_RELEASE/.ava-release"
  ssh_vm "set -eu
mv -- $Q_RELEASE/.ava-building $Q_RELEASE/.ava-ready
chmod -R a-w -- $Q_RELEASE"
else
  titre "3-5/7 Validation de la release immutable existante"
  ssh_vm "set -eu
export OPENJARVIS_NO_ANALYTICS=1 DO_NOT_TRACK=1 PYTHONDONTWRITEBYTECODE=1
cd /tmp
AVA_EXPECTED_RELEASE=$Q_RELEASE $Q_RELEASE/.venv/bin/python -I -c \"import os; from pathlib import Path; import anthropic, ava_extensions, openjarvis, openjarvis_rust; from openjarvis._rust_bridge import get_rust_module; root=Path(os.environ['AVA_EXPECTED_RELEASE']).resolve(); assert Path(openjarvis.__file__).resolve().is_relative_to(root); assert Path(ava_extensions.__file__).resolve().is_relative_to(root); get_rust_module()\"
AVA_RELEASE_HISTORY_FILE=$Q_REMOTE_EVOLUTIONS $Q_RELEASE/.venv/bin/python -I -c \"from ava_extensions.skills.evolutions import _lignes_git; assert _lignes_git('', 3) is not None\"
$Q_RELEASE/.venv/bin/jarvis --help >/dev/null
test -s $Q_RELEASE/src/openjarvis/server/static/index.html
test \"\$(sha256sum -- $Q_REMOTE_SOURCE_ARCHIVE | awk '{print \$1}')\" = $SOURCE_TREE_SHA256
test \"\$(sha256sum -- $Q_REMOTE_WHEEL | awk '{print \$1}')\" = $WHEEL_SHA256
test \"\$(sha256sum -- $Q_REMOTE_ATTESTATION | awk '{print \$1}')\" = $ATTESTATION_SHA256
test \"\$(sha256sum -- $Q_REMOTE_RUST_ARCHIVE | awk '{print \$1}')\" = $RUST_TREE_SHA256
test \"\$(sha256sum -- $Q_REMOTE_EVOLUTIONS | awk '{print \$1}')\" = $EVOLUTIONS_SHA256"

  validate_runtime_policy "$RELEASE_PATH" \
    || fatal "persona ou politique runtime hors de la release existante"
fi

titre "6/7 Bascule atomique et redemarrage"
if [[ "$PREVIOUS_TARGET" == "$RELEASE_PATH" ]]; then
  if health_check; then
    confirm_deadman
    echo "   + release deja active et saine; aucun redemarrage"
    garbage_collect_releases_after_confirmation
    printf '\n\033[1;32mRelease %s deja deployee et verifiee.\033[0m\n' "${ATTENDU:0:12}"
    exit 0
  fi
  fatal "release demandee deja active mais malsaine; aucune cible precedente distincte"
fi
if [[ "$PREVIOUS_TARGET" != "$RELEASE_PATH" ]]; then
  # Revalider la cible de retour immediatement avant l'armement : une longue
  # construction ne doit jamais laisser le dead-man pointer vers une release dont
  # la politique runtime a derive depuis le preflight initial.
  if [[ "$BOOTSTRAP_WITHOUT_PREVIOUS" -eq 0 ]]; then
    if ! previous_target_is_safe "$PREVIOUS_TARGET" \
      || ! validate_runtime_policy "$PREVIOUS_TARGET"; then
      fatal "cible de rollback hors contrat ou politique runtime avant armement"
    fi
  fi
  # L'etat root-owned et son timer sont ensuite armes AVANT l'ecriture distante. Ils
  # ne dependent plus de cette session SSH apres la bascule.
  arm_deadman
  SWITCHED=1
  if [[ "$BOOTSTRAP_WITHOUT_PREVIOUS" -eq 1 ]]; then
    KEEP_FAILED_CANDIDATE=1
  fi
  atomic_link "$RELEASE_PATH"
fi
ssh_vm "set -eu
sudo systemctl restart $Q_SERVICE
sudo systemctl restart openjarvis-relay.service openjarvis-relay-tls.service"

titre "7/7 Sante et confirmation"
sleep "$HEALTH_DELAY_SECONDS"
if ! health_check; then
  fatal "la nouvelle release n'est pas saine"
fi

confirm_deadman
SWITCHED=0
garbage_collect_releases_after_confirmation
echo "   + service, HTTP, relay, SDK, Rust et telemetrie verifies"
printf '\n\033[1;32mRelease %s deployee et verifiee.\033[0m\n' "${ATTENDU:0:12}"
