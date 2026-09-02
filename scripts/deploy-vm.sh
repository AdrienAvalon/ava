#!/usr/bin/env bash
# Livre un commit Ava comme une release immuable, puis bascule atomiquement le service.
#
# Le script est lance depuis un checkout local propre. Il ne fait ni pull, ni uv sync,
# ni build npm dans le checkout actuellement servi par la VM. Le code du commit local
# est transfere par `git archive` dans un repertoire neuf nomme par son SHA. La release
# ne devient active qu'apres scellement root-owned et bascule par le dead-man
# autoritaire. Le compte `avalon` ne peut jamais ecrire dans `/var/lib/ava`.
#
# Avant la bascule, toute cible de retour scellee passe le meme contrat de
# persona/politique que la candidate, puis un dead-man persistant root-owned est arme
# sur la VM. L'exception legacy 809 n'est qu'un token CAS de premiere migration : un
# echec arrete main et relais, il ne remet jamais ce runtime user-owned en service.
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
# `scripts/deploy-vm.sh --prepare-only` s'arrete apres avoir materialise et rendu
# immutable la release : il ne touche ni au pointeur current, ni aux services, ni au
# dead-man et ne lance pas la retention.
set -Eeuo pipefail
umask 077

VM_JUMP="${AVA_JUMP:-avalon@192.168.2.40}"
VM="${AVA_VM:-avalon@192.168.100.15}"
STAGING_ROOT="${AVA_STAGING_ROOT:-/home/avalon/ava-releases}"
AUTHORITATIVE_RELEASE_ROOT="${AVA_AUTHORITATIVE_RELEASE_ROOT:-/var/lib/ava/releases}"
CURRENT_LINK="${AVA_CURRENT_LINK:-/var/lib/ava/current}"
LEGACY_GIT_SHA='809ade530fedc53424f6fe93a22320d129237e62'
SERVICE="${AVA_SERVICE:-openjarvis}"
HEALTH_DELAY_SECONDS="${AVA_HEALTH_DELAY_SECONDS:-12}"
LOCK_STALE_SECONDS="${AVA_DEPLOY_LOCK_STALE_SECONDS:-21600}"
SSH_BIN="${AVA_SSH_BIN:-ssh}"
RACINE_LOCALE=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
RUST_WHEEL="${AVA_RUST_WHEEL:-}"
RUST_ATTESTATION="${AVA_RUST_ATTESTATION:-${RUST_WHEEL:+${RUST_WHEEL}.attestation}}"
RUST_BUILDER_DOCKERFILE='deploy/docker/Dockerfile.rust-builder'
RUST_BUILDER_PLATFORM='linux/amd64'
RUST_BUILDER_PYTHON_IMAGE='python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf'
RUST_BUILDER_RUST_IMAGE='rust:1.88.0-bookworm@sha256:4727898c104ecd2e22d780925832502faee9fe4e70581b8572af081370b315a0'
FRONTEND_BUILDER_DOCKERFILE='deploy/docker/Dockerfile.frontend-builder'
FRONTEND_BUILDER_BASE_IMAGE='node:22.23.0-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2'
FRONTEND_BUILDER_PLATFORM='linux/amd64'
FRONTEND_BUILDER_NODE_VERSION='22.23.0'
FRONTEND_BUILDER_NPM_VERSION='10.9.8'
DOCKER_BIN="${AVA_DOCKER_BIN:-docker}"
DEADMAN_HELPER='/usr/local/libexec/avalon/ava-deploy-deadman.py'
SEAL_HELPER='/usr/local/libexec/avalon/ava-release-seal.py'
UV_BIN='/usr/local/bin/ava-uv'
UV_VERSION='uv 0.12.5 (x86_64-unknown-linux-gnu)'
UV_SHA256='b65f23a420c4acc96427efb30e5ed9bc0f7e25d2d712000f6ede77c1a0de5f46'
PYTHON_RUNTIME_ARCHIVE="${AVA_PYTHON_RUNTIME_ARCHIVE:-/var/cache/ava/python/cpython-3.12.13+20260510-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz}"
PYTHON_RUNTIME_SHA256="${AVA_PYTHON_RUNTIME_SHA256:-d480f5d5878910ecbae212bf23bd7c25d7b209eb8cf5e98823c977384d272e88}"
RUNTIME_REQUIREMENTS_PATH='deploy/runtime/ava-runtime-requirements.v1.txt'
RUNTIME_WHEELHOUSE_MANIFEST_PATH='deploy/runtime/ava-runtime-wheelhouse.v1.json'
PYTHON_RUNTIME_SOURCE_PATH='deploy/runtime/ava-python-runtime.v1.json'
RUNTIME_WHEEL_PATH='deploy/runtime/wheels/docopt-0.6.2-py2.py3-none-any.whl'
RUNTIME_WHEEL_FILENAME='docopt-0.6.2-py2.py3-none-any.whl'
RUNTIME_WHEEL_SHA256='6d6eabf5974d0b72899f74ecb6ae84f0d436ca0f7b3037ffc7b9a8a0790a6813'
TREATMENT_PATH='ava_extensions/identity/relationship_guard_treatment.py'
BASELINE_TREATMENT='shadow-baseline-only-v1'
RUNTIME_TREATMENT='runtime-enforced-v1'
DEADMAN_TTL_SECONDS="${AVA_DEADMAN_TTL_SECONDS:-180}"
RELAY_CA="${AVA_RELAY_CA:-/etc/ssl/certs/avalon-internal-ca.crt}"
RELAY_TLS_NAME="${AVA_RELAY_TLS_NAME:-192.168.100.15}"
EVOLUTIONS_EXPORT_LIMIT=100
EVOLUTIONS_FILENAME='evolutions-v1.json'

fatal() {
  printf 'ERREUR: %s\n' "$*" >&2
  exit 1
}

titre() {
  printf '\n\033[1m-- %s\033[0m\n' "$1"
}

PREPARE_ONLY=0
case $# in
  0) ;;
  1)
    [[ "$1" == "--prepare-only" ]] || fatal "argument inconnu: $1"
    PREPARE_ONLY=1
    ;;
  *) fatal "un seul argument optionnel est accepte: --prepare-only" ;;
esac

remote_path_is_safe() {
  local path=$1
  [[ "$path" =~ ^/[A-Za-z0-9._+/-]+$ ]] || return 1
  [[ "$path" != "/" && "$path" != "/home" && "$path" != "/home/avalon" ]] || return 1
  [[ "/$path/" != *"/../"* && "/$path/" != *"/./"* ]]
}

ssh_destination_is_safe() {
  local destination=$1
  [[ "$destination" =~ ^([A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$ ]]
}

for remote_path in \
  "$STAGING_ROOT" "$AUTHORITATIVE_RELEASE_ROOT" "$CURRENT_LINK" \
  "$UV_BIN" "$PYTHON_RUNTIME_ARCHIVE" "$DEADMAN_HELPER" "$SEAL_HELPER" "$RELAY_CA"; do
  remote_path_is_safe "$remote_path" || fatal "chemin distant refuse: $remote_path"
  [[ "$remote_path" != */ && "$remote_path" != *//* ]] \
    || fatal "chemin distant non canonique: $remote_path"
done
[[ "$STAGING_ROOT" != "$AUTHORITATIVE_RELEASE_ROOT" \
  && "$AUTHORITATIVE_RELEASE_ROOT" != "$CURRENT_LINK" ]] \
  || fatal "racines distantes non distinctes"
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
[[ "$PYTHON_RUNTIME_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "pin du runtime Python invalide"
[[ "$LEGACY_GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "token legacy non exact"
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
for required_path in \
  uv.lock "$RUNTIME_REQUIREMENTS_PATH" "$RUNTIME_WHEELHOUSE_MANIFEST_PATH" \
  "$PYTHON_RUNTIME_SOURCE_PATH" \
  "$RUNTIME_WHEEL_PATH" "$TREATMENT_PATH" ava_extensions/runtime_bootstrap.py \
  "$FRONTEND_BUILDER_DOCKERFILE" frontend/package.json frontend/package-lock.json; do
  git -C "$RACINE_LOCALE" cat-file -e "${ATTENDU}:${required_path}" \
    || fatal "le commit ne contient pas ${required_path}"
done
for reserved_path in \
  .ava-artifacts .ava-building .ava-files-manifest.jsonl .ava-python .python .ava-ready \
  .ava-release .ava-runtime-manifest.jsonl .ava-seal.json .ava-source-manifest.jsonl \
  .venv src/openjarvis/server/static; do
  if git -C "$RACINE_LOCALE" cat-file -e "${ATTENDU}:${reserved_path}" 2>/dev/null; then
    fatal "le commit contient le chemin interne reserve ${reserved_path}"
  fi
done

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

# Materialiser une seule fois les octets de l'archive Git. Cette meme archive est
# ensuite le contexte source du builder, le transfert et le pin du manifeste.
LOCAL_SOURCE_ARCHIVE="$LOCAL_TMP/source-tree.tar"
git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" > "$LOCAL_SOURCE_ARCHIVE"
SOURCE_TREE_SHA256=$(sha256sum -- "$LOCAL_SOURCE_ARCHIVE" | awk '{print $1}')
RUST_TREE_SHA256=$(git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" rust \
  | sha256sum | awk '{print $1}')
RUST_BUILDER_DOCKERFILE_SHA256=$(git -C "$RACINE_LOCALE" show \
  "${ATTENDU}:${RUST_BUILDER_DOCKERFILE}" | sha256sum | awk '{print $1}')
FRONTEND_BUILDER_DOCKERFILE_SHA256=$(git -C "$RACINE_LOCALE" show \
  "${ATTENDU}:${FRONTEND_BUILDER_DOCKERFILE}" | sha256sum | awk '{print $1}')
WHEEL_SHA256=$(sha256sum -- "$RUST_WHEEL" | awk '{print $1}')
ATTESTATION_SHA256=$(sha256sum -- "$RUST_ATTESTATION" | awk '{print $1}')
UV_LOCK_SHA256=$(git -C "$RACINE_LOCALE" show "${ATTENDU}:uv.lock" | sha256sum | awk '{print $1}')
RUNTIME_REQUIREMENTS_SHA256=$(git -C "$RACINE_LOCALE" show \
  "${ATTENDU}:${RUNTIME_REQUIREMENTS_PATH}" | sha256sum | awk '{print $1}')
RUNTIME_WHEELHOUSE_MANIFEST_SHA256=$(git -C "$RACINE_LOCALE" show \
  "${ATTENDU}:${RUNTIME_WHEELHOUSE_MANIFEST_PATH}" | sha256sum | awk '{print $1}')
PYTHON_RUNTIME_SOURCE_SHA256=$(git -C "$RACINE_LOCALE" show \
  "${ATTENDU}:${PYTHON_RUNTIME_SOURCE_PATH}" | sha256sum | awk '{print $1}')
COMMITTED_RUNTIME_WHEEL_SHA256=$(git -C "$RACINE_LOCALE" show \
  "${ATTENDU}:${RUNTIME_WHEEL_PATH}" | sha256sum | awk '{print $1}')
for checksum in \
  "$SOURCE_TREE_SHA256" "$RUST_TREE_SHA256" "$WHEEL_SHA256" "$ATTESTATION_SHA256" \
  "$UV_LOCK_SHA256" "$RUNTIME_REQUIREMENTS_SHA256" \
  "$RUNTIME_WHEELHOUSE_MANIFEST_SHA256" \
  "$PYTHON_RUNTIME_SOURCE_SHA256" "$COMMITTED_RUNTIME_WHEEL_SHA256" \
  "$FRONTEND_BUILDER_DOCKERFILE_SHA256"; do
  [[ "$checksum" =~ ^[0-9a-f]{64}$ ]] || fatal "checksum local de release invalide"
done
[[ "$COMMITTED_RUNTIME_WHEEL_SHA256" == "$RUNTIME_WHEEL_SHA256" ]] \
  || fatal "wheel runtime source divergente du pin operateur"

command -v python3 >/dev/null 2>&1 || fatal "python3 local introuvable"
TREATMENT=$(git -C "$RACINE_LOCALE" show "${ATTENDU}:${TREATMENT_PATH}" | python3 -c '
import ast
import sys

module = ast.parse(sys.stdin.read())
values = []
for node in module.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "RELATIONSHIP_GUARD_TREATMENT" for target in targets):
            value = ast.literal_eval(node.value)
            values.append(value)
if len(values) != 1 or not isinstance(values[0], str):
    raise SystemExit(2)
sys.stdout.write(values[0])
') || fatal "traitement relationnel du commit illisible"
if [[ "$PREPARE_ONLY" -eq 1 ]]; then
  [[ "$TREATMENT" == "$BASELINE_TREATMENT" || "$TREATMENT" == "$RUNTIME_TREATMENT" ]] \
    || fatal "traitement relationnel inconnu dans la preparation"
else
  [[ "$TREATMENT" == "$RUNTIME_TREATMENT" ]] \
    || fatal "activation refusee: le traitement baseline A n'est jamais servable"
fi

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
LOCK_TOKEN=$(printf '%s\n' "${ATTENDU}:$$:${LOCAL_TMP}" | sha256sum | awk '{print $1}')
[[ "$LOCK_TOKEN" =~ ^[0-9a-f]{64}$ ]] || fatal "token local de verrou invalide"
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

# Construire deux fois le frontend depuis l'unique archive Git locale. Chaque build
# Docker est frais et sans cache, dans la base immuable du depot. Le second build ne
# sert pas a choisir un pin : il doit produire exactement les memes octets et la meme
# chaine Node/npm, sinon la livraison s'arrete avant tout acces distant.
build_frontend_archive() {
  command -v "$DOCKER_BIN" >/dev/null 2>&1 || fatal "Docker local introuvable"
  LOCAL_BUILD_ROOT="$LOCAL_TMP/source"
  mkdir -- "$LOCAL_BUILD_ROOT"
  tar -xf "$LOCAL_SOURCE_ARCHIVE" -C "$LOCAL_BUILD_ROOT"
  [[ ! -e "$LOCAL_BUILD_ROOT/.git" && ! -L "$LOCAL_BUILD_ROOT/.git" ]] \
    || fatal "contexte frontend contenant des metadata Git"
  [[ "$(sha256sum -- "$LOCAL_BUILD_ROOT/$FRONTEND_BUILDER_DOCKERFILE" | awk '{print $1}')" \
    == "$FRONTEND_BUILDER_DOCKERFILE_SHA256" ]] \
    || fatal "Dockerfile frontend divergent de l'archive source"

  local source_values="$LOCAL_TMP/frontend-source-values"
  python3 - "$LOCAL_SOURCE_ARCHIVE" > "$source_values" <<'PY'
import hashlib
import json
import stat
import sys
import tarfile
from pathlib import PurePosixPath


def canonical_path(raw: str) -> str:
    path = raw.rstrip("/")
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise SystemExit("chemin frontend source non canonique")
    return path


rows = []
seen = set()
package_payloads = {}
with tarfile.open(sys.argv[1], mode="r:") as archive:
    for member in archive.getmembers():
        name = canonical_path(member.name)
        if not name.startswith("frontend/"):
            continue
        if name in seen:
            raise SystemExit("chemin frontend source duplique")
        seen.add(name)
        if member.isdir():
            rows.append({"mode": "0555", "path": name, "type": "directory"})
            continue
        if not member.isreg():
            raise SystemExit("type frontend source interdit")
        stream = archive.extractfile(member)
        if stream is None:
            raise SystemExit("fichier frontend source illisible")
        payload = stream.read()
        if len(payload) != member.size:
            raise SystemExit("taille frontend source incoherente")
        rows.append(
            {
                "mode": "0555" if stat.S_IMODE(member.mode) & 0o111 else "0444",
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "type": "file",
            }
        )
        if name in {"frontend/package.json", "frontend/package-lock.json"}:
            package_payloads[name] = payload

if not rows or set(package_payloads) != {"frontend/package.json", "frontend/package-lock.json"}:
    raise SystemExit("sous-arbre ou manifests frontend incomplets")
rows.sort(key=lambda row: row["path"])
encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
print(hashlib.sha256(encoded).hexdigest())
print(hashlib.sha256(package_payloads["frontend/package.json"]).hexdigest())
print(hashlib.sha256(package_payloads["frontend/package-lock.json"]).hexdigest())
PY
  readarray -t frontend_source_values < "$source_values"
  [[ "${#frontend_source_values[@]}" -eq 3 ]] \
    || fatal "metadata du sous-arbre frontend incompletes"
  FRONTEND_SOURCE_MAP_SHA256=${frontend_source_values[0]}
  FRONTEND_PACKAGE_JSON_SHA256=${frontend_source_values[1]}
  FRONTEND_PACKAGE_LOCK_SHA256=${frontend_source_values[2]}

  local build_number output
  local -a frontend_outputs=()
  for build_number in 1 2; do
    output="$LOCAL_TMP/frontend-build-${build_number}"
    mkdir -- "$output"
    "$DOCKER_BIN" build \
      --pull \
      --no-cache \
      --platform "$FRONTEND_BUILDER_PLATFORM" \
      --file "$LOCAL_BUILD_ROOT/$FRONTEND_BUILDER_DOCKERFILE" \
      --output "type=local,dest=$output" \
      "$LOCAL_BUILD_ROOT" \
      || fatal "build frontend reproductible ${build_number}/2 refuse"
    if find "$output" -xdev \( -type l -o ! -type d ! -type f \) -print -quit | grep -q .; then
      fatal "sortie du builder frontend contenant un type special"
    fi
    [[ -f "$output/frontend-static.tar" && ! -L "$output/frontend-static.tar" \
      && -f "$output/frontend-toolchain.json" && ! -L "$output/frontend-toolchain.json" \
      && "$(find "$output" -mindepth 1 -maxdepth 1 -type f | wc -l)" -eq 2 \
      && "$(find "$output" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 0 ]] \
      || fatal "sortie du builder frontend hors contrat"
    frontend_outputs+=("$output")
  done

  cmp -s -- "${frontend_outputs[0]}/frontend-static.tar" \
    "${frontend_outputs[1]}/frontend-static.tar" \
    || fatal "les deux builds frontend ne sont pas octet-identiques"
  cmp -s -- "${frontend_outputs[0]}/frontend-toolchain.json" \
    "${frontend_outputs[1]}/frontend-toolchain.json" \
    || fatal "la chaine Node/npm a diverge entre les deux builds frontend"

  LOCAL_FRONTEND_TAR="${frontend_outputs[0]}/frontend-static.tar"
  local toolchain_values="$LOCAL_TMP/frontend-toolchain-values"
  python3 - "${frontend_outputs[0]}/frontend-toolchain.json" \
    "$FRONTEND_BUILDER_NODE_VERSION" "$FRONTEND_BUILDER_NPM_VERSION" \
    > "$toolchain_values" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = path.read_bytes()
try:
    document = json.loads(payload)
except (UnicodeDecodeError, json.JSONDecodeError) as error:
    raise SystemExit("metadata toolchain frontend invalides") from error
if not isinstance(document, dict) or set(document) != {"node_version", "npm_version"}:
    raise SystemExit("schema toolchain frontend invalide")
canonical = (json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
if payload != canonical:
    raise SystemExit("metadata toolchain frontend non canoniques")
if document != {"node_version": sys.argv[2], "npm_version": sys.argv[3]}:
    raise SystemExit("chaine Node/npm frontend inattendue")
print(document["npm_version"])
PY
  readarray -t frontend_toolchain_values < "$toolchain_values"
  [[ "${#frontend_toolchain_values[@]}" -eq 1 ]] \
    || fatal "metadata toolchain frontend incompletes"
  FRONTEND_NPM_VERSION=${frontend_toolchain_values[0]}

  local archive_values="$LOCAL_TMP/frontend-archive-values"
  python3 - "$LOCAL_FRONTEND_TAR" > "$archive_values" <<'PY'
import hashlib
import json
import sys
import tarfile
from pathlib import Path, PurePosixPath


def canonical_path(raw: str) -> str:
    path = raw.rstrip("/")
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise SystemExit("chemin archive frontend non canonique")
    return path


archive_path = Path(sys.argv[1])
payload = archive_path.read_bytes()
if len(payload) < 512 or payload[257:263] != b"ustar\0":
    raise SystemExit("archive frontend hors format USTAR")
rows = []
seen = set()
logical_end = 0
with tarfile.open(archive_path, mode="r:") as archive:
    members = archive.getmembers()
    for member in members:
        if (
            member.offset != logical_end
            or member.offset_data != member.offset + 512
            or payload[member.offset + 257 : member.offset + 263] != b"ustar\0"
        ):
            raise SystemExit("structure de blocs USTAR frontend non canonique")
        name = canonical_path(member.name)
        if name in seen:
            raise SystemExit("chemin archive frontend duplique")
        seen.add(name)
        if (
            member.pax_headers
            or member.uid != 0
            or member.gid != 0
            or member.uname
            or member.gname
            or member.mtime != 0
        ):
            raise SystemExit("metadata archive frontend non canoniques")
        if member.isdir():
            if member.mode != 0o755 or member.size != 0 or member.linkname:
                raise SystemExit("mode repertoire frontend non canonique")
            rows.append({"mode": "0555", "path": name, "type": "directory"})
            logical_end = member.offset_data
            continue
        if not member.isreg() or member.mode != 0o644 or member.linkname:
            raise SystemExit("type ou mode fichier frontend non canonique")
        stream = archive.extractfile(member)
        if stream is None:
            raise SystemExit("fichier archive frontend illisible")
        content = stream.read()
        if len(content) != member.size:
            raise SystemExit("taille archive frontend incoherente")
        rows.append(
            {
                "mode": "0444",
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "type": "file",
            }
        )
        logical_end = member.offset_data + ((member.size + 511) // 512) * 512
if (
    len(payload) % 512 != 0
    or logical_end > len(payload)
    or len(payload) - logical_end < 1024
    or any(payload[logical_end:])
):
    raise SystemExit("remplissage final USTAR frontend non canonique")
if not rows or [row["path"] for row in rows] != sorted(row["path"] for row in rows):
    raise SystemExit("ordre archive frontend non canonique")
if "index.html" not in seen:
    raise SystemExit("archive frontend vide ou sans index")
encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
print(hashlib.sha256(payload).hexdigest())
print(hashlib.sha256(encoded).hexdigest())
PY
  readarray -t frontend_archive_values < "$archive_values"
  [[ "${#frontend_archive_values[@]}" -eq 2 ]] \
    || fatal "metadata de l'archive frontend incompletes"
  FRONTEND_STATIC_SHA256=${frontend_archive_values[0]}
  FRONTEND_ARCHIVE_MAP_SHA256=${frontend_archive_values[1]}

  LOCAL_FRONTEND_BUILD_ATTESTATION="$LOCAL_TMP/frontend-build-attestation.json"
  python3 - \
    "$LOCAL_FRONTEND_BUILD_ATTESTATION" \
    "$ATTENDU" \
    "$SOURCE_TREE_SHA256" \
    "$FRONTEND_SOURCE_MAP_SHA256" \
    "$FRONTEND_PACKAGE_JSON_SHA256" \
    "$FRONTEND_PACKAGE_LOCK_SHA256" \
    "$FRONTEND_BUILDER_DOCKERFILE_SHA256" \
    "$FRONTEND_BUILDER_BASE_IMAGE" \
    "$FRONTEND_BUILDER_PLATFORM" \
    "$FRONTEND_BUILDER_NODE_VERSION" \
    "$FRONTEND_NPM_VERSION" \
    "$FRONTEND_STATIC_SHA256" \
    "$FRONTEND_ARCHIVE_MAP_SHA256" <<'PY'
import json
import sys
from pathlib import Path

(
    destination,
    git_sha,
    source_archive_sha256,
    frontend_source_map_sha256,
    package_json_sha256,
    package_lock_sha256,
    dockerfile_sha256,
    base_image,
    platform,
    node_version,
    npm_version,
    archive_sha256,
    archive_map_sha256,
) = sys.argv[1:]
document = {
    "builder": {
        "base_image": base_image,
        "dockerfile_path": "deploy/docker/Dockerfile.frontend-builder",
        "dockerfile_sha256": f"sha256:{dockerfile_sha256}",
        "node_version": node_version,
        "npm_version": npm_version,
        "platform": platform,
    },
    "frontend_source_map_sha256": f"sha256:{frontend_source_map_sha256}",
    "git_sha": git_sha,
    "output": {
        "archive_map_sha256": f"sha256:{archive_map_sha256}",
        "archive_path": "frontend-static.tar",
        "archive_sha256": f"sha256:{archive_sha256}",
        "build_count": 2,
    },
    "package_json_sha256": f"sha256:{package_json_sha256}",
    "package_lock_sha256": f"sha256:{package_lock_sha256}",
    "schema_version": "ava.frontend.build-attestation/v1",
    "source_archive_sha256": f"sha256:{source_archive_sha256}",
}
payload = (json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
Path(destination).write_bytes(payload)
PY
  FRONTEND_BUILD_ATTESTATION_SHA256=$(sha256sum -- \
    "$LOCAL_FRONTEND_BUILD_ATTESTATION" | awk '{print $1}')
  for checksum in \
    "$FRONTEND_SOURCE_MAP_SHA256" "$FRONTEND_PACKAGE_JSON_SHA256" \
    "$FRONTEND_PACKAGE_LOCK_SHA256" "$FRONTEND_STATIC_SHA256" \
    "$FRONTEND_ARCHIVE_MAP_SHA256" "$FRONTEND_BUILD_ATTESTATION_SHA256"; do
    [[ "$checksum" =~ ^[0-9a-f]{64}$ ]] || fatal "SHA-256 frontend invalide"
  done
}

validate_runtime_wheelhouse_manifest() {
  python3 - \
    "$LOCAL_BUILD_ROOT/$RUNTIME_WHEELHOUSE_MANIFEST_PATH" \
    "$RUNTIME_WHEELHOUSE_MANIFEST_SHA256" \
    "$RUNTIME_REQUIREMENTS_SHA256" \
    "$UV_LOCK_SHA256" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{1,180}\.whl$")


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"cle JSON dupliquee: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"constante JSON interdite: {value}")


path = Path(sys.argv[1])
expected_sha256, requirements_sha256, uv_lock_sha256 = sys.argv[2:]
payload = path.read_bytes()
if hashlib.sha256(payload).hexdigest() != expected_sha256:
    raise SystemExit("manifeste wheelhouse divergent de l'archive Git")
try:
    document = json.loads(
        payload.decode("ascii"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
except (UnicodeError, json.JSONDecodeError, ValueError) as error:
    raise SystemExit("manifeste wheelhouse JSON invalide") from error
canonical = (
    json.dumps(document, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    + "\n"
).encode("ascii")
if payload != canonical or type(document) is not dict or set(document) != {
    "entries",
    "requirements_sha256",
    "schema_version",
    "target",
    "uv_lock_sha256",
}:
    raise SystemExit("manifeste wheelhouse non canonique ou incomplet")
if (
    document["schema_version"] != "ava.runtime-wheelhouse/v1"
    or document["target"]
    != {
        "implementation": "cpython",
        "platform": "linux_x86_64",
        "python_version": "3.12.13",
    }
    or document["requirements_sha256"] != f"sha256:{requirements_sha256}"
    or document["uv_lock_sha256"] != f"sha256:{uv_lock_sha256}"
):
    raise SystemExit("manifeste wheelhouse divergent du target, requirements ou uv.lock")
entries = document["entries"]
if type(entries) is not list or not 1 <= len(entries) <= 1000:
    raise SystemExit("manifeste wheelhouse vide ou hors borne")
names = []
digests = []
for entry in entries:
    if (
        type(entry) is not dict
        or set(entry) != {"filename", "sha256"}
        or type(entry["filename"]) is not str
        or WHEEL_RE.fullmatch(entry["filename"]) is None
        or type(entry["sha256"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", entry["sha256"]) is None
    ):
        raise SystemExit("entree du manifeste wheelhouse invalide")
    names.append(entry["filename"])
    digests.append(entry["sha256"])
if (
    names != sorted(names, key=lambda value: value.encode("ascii"))
    or len(names) != len(set(names))
    or len(names) != len({name.casefold() for name in names})
    or len(digests) != len(set(digests))
):
    raise SystemExit("entrees du manifeste wheelhouse non uniques ou non triees")
PY
}

# Aucun verrou ni systeme actif ne doit etre touche avant que les deux builds locaux
# et leur attestation aient prouve leur reproductibilite.
build_frontend_archive
validate_runtime_wheelhouse_manifest

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
AVA_EXPECTED_RELEASE=$q_expected_target $q_expected_target/.venv/bin/python -I -S -c \"import os,sys; from pathlib import Path; root=Path(os.environ['AVA_EXPECTED_RELEASE']).resolve(); sys.path[:0]=[str(root),str(root/'src'),str(root/'.venv/lib/python3.12/site-packages')]; from ava_extensions.identity.relationship_guard_treatment import RELATIONSHIP_GUARD_TREATMENT; from ava_extensions.patches.system_prompt_loader import _DEFAULT_PERSONA; from openjarvis.analytics.identity import is_analytics_enabled; from openjarvis.core.config import load_config; persona=_DEFAULT_PERSONA.resolve(); config=load_config(); assert RELATIONSHIP_GUARD_TREATMENT == 'runtime-enforced-v1'; assert config.agent.system_prompt_path == ''; assert persona.is_relative_to(root); assert config.agent.default_system_prompt == persona.read_text(encoding='utf-8').strip(); assert not is_analytics_enabled(config.analytics)\""
}

previous_target_is_safe() {
  local target=$1 suffix q_target
  [[ "$target" == "$AUTHORITATIVE_RELEASE_ROOT/"* ]] || return 1
  suffix=${target#"$AUTHORITATIVE_RELEASE_ROOT/"}
  [[ "$suffix" =~ ^[0-9a-f]{40}$ ]] || return 1
  q_target=$(quote_remote "$target")
  ssh_vm "set -eu
test -d $q_target
test ! -L $q_target
test -f $q_target/.ava-ready
test ! -L $q_target/.ava-ready
test -f $q_target/.ava-release
test ! -L $q_target/.ava-release
test -f $q_target/.ava-seal.json
test ! -L $q_target/.ava-seal.json
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
grep -q '\"format\":\"ava-sealed-release-v1\"' $q_target/.ava-seal.json
test -x $q_target/.venv/bin/python
test -f $q_target/ava_extensions/runtime_bootstrap.py
test -s $q_target/src/openjarvis/server/static/index.html"
}

STAGING_PATH="${STAGING_ROOT}/${ATTENDU}"
RELEASE_PATH="${AUTHORITATIVE_RELEASE_ROOT}/${ATTENDU}"
LOCK_PATH="${STAGING_ROOT}/.deploy-lock"
LOCK_MARKER_PATH="${LOCK_PATH}/.ava-deploy-owner"
LOCK_OWNER_XATTR='user.ava_deploy_token'
LOCK_TEMP_PATH="${STAGING_ROOT}/.deploy-lock.${LOCK_TOKEN}.tmp"
LOCK_CANCEL_PATH="${STAGING_ROOT}/.deploy-lock.${LOCK_TOKEN}.cancel"
LOCK_CONTROL_PATH="${STAGING_ROOT}/.deploy-lock.${LOCK_TOKEN}.control"
LOCK_RELEASE_PATH="${STAGING_ROOT}/.deploy-lock.${LOCK_TOKEN}.release"
LOCK_TEMP_MARKER_PATH="${LOCK_TEMP_PATH}/.ava-deploy-owner"
ARTIFACT_DIR="${STAGING_PATH}/.ava-artifacts"
REMOTE_WHEEL="${ARTIFACT_DIR}/${WHEEL_FILENAME}"
REMOTE_ATTESTATION="${ARTIFACT_DIR}/${WHEEL_FILENAME}.attestation"
REMOTE_SOURCE_ARCHIVE="${ARTIFACT_DIR}/source-tree.tar"
REMOTE_RUST_ARCHIVE="${ARTIFACT_DIR}/rust-tree.tar"
REMOTE_EVOLUTIONS="${ARTIFACT_DIR}/${EVOLUTIONS_FILENAME}"
REMOTE_FRONTEND_ARCHIVE="${ARTIFACT_DIR}/frontend-static.tar"
REMOTE_FRONTEND_BUILD_ATTESTATION="${ARTIFACT_DIR}/frontend-build-attestation.json"
REMOTE_WHEELHOUSE="${ARTIFACT_DIR}/python-wheelhouse"
REMOTE_REQUIREMENTS_TMP="${ARTIFACT_DIR}/.runtime-requirements.tmp"
REMOTE_DOWNLOADER="${STAGING_PATH}/.python-wheel-downloader"
CURRENT_PARENT=$(dirname -- "$CURRENT_LINK")

Q_RELEASE_ROOT=$(quote_remote "$STAGING_ROOT")
Q_AUTHORITATIVE_ROOT=$(quote_remote "$AUTHORITATIVE_RELEASE_ROOT")
Q_STAGING=$(quote_remote "$STAGING_PATH")
Q_RELEASE=$(quote_remote "$RELEASE_PATH")
Q_LOCK=$(quote_remote "$LOCK_PATH")
Q_LOCK_MARKER=$(quote_remote "$LOCK_MARKER_PATH")
Q_LOCK_TOKEN=$(quote_remote "$LOCK_TOKEN")
Q_LOCK_OWNER_XATTR=$(quote_remote "$LOCK_OWNER_XATTR")
Q_LOCK_TEMP=$(quote_remote "$LOCK_TEMP_PATH")
Q_LOCK_CANCEL=$(quote_remote "$LOCK_CANCEL_PATH")
Q_LOCK_CONTROL=$(quote_remote "$LOCK_CONTROL_PATH")
Q_LOCK_RELEASE=$(quote_remote "$LOCK_RELEASE_PATH")
Q_LOCK_TEMP_MARKER=$(quote_remote "$LOCK_TEMP_MARKER_PATH")
Q_CURRENT=$(quote_remote "$CURRENT_LINK")
Q_CURRENT_PARENT=$(quote_remote "$CURRENT_PARENT")
Q_ARTIFACT_DIR=$(quote_remote "$ARTIFACT_DIR")
Q_REMOTE_WHEEL=$(quote_remote "$REMOTE_WHEEL")
Q_REMOTE_ATTESTATION=$(quote_remote "$REMOTE_ATTESTATION")
Q_REMOTE_SOURCE_ARCHIVE=$(quote_remote "$REMOTE_SOURCE_ARCHIVE")
Q_REMOTE_RUST_ARCHIVE=$(quote_remote "$REMOTE_RUST_ARCHIVE")
Q_REMOTE_EVOLUTIONS=$(quote_remote "$REMOTE_EVOLUTIONS")
Q_REMOTE_FRONTEND_ARCHIVE=$(quote_remote "$REMOTE_FRONTEND_ARCHIVE")
Q_REMOTE_FRONTEND_BUILD_ATTESTATION=$(quote_remote "$REMOTE_FRONTEND_BUILD_ATTESTATION")
Q_REMOTE_WHEELHOUSE=$(quote_remote "$REMOTE_WHEELHOUSE")
Q_REMOTE_REQUIREMENTS_TMP=$(quote_remote "$REMOTE_REQUIREMENTS_TMP")
Q_REMOTE_DOWNLOADER=$(quote_remote "$REMOTE_DOWNLOADER")
Q_PYTHON_RUNTIME_ARCHIVE=$(quote_remote "$PYTHON_RUNTIME_ARCHIVE")
Q_SEAL_HELPER=$(quote_remote "$SEAL_HELPER")
Q_SERVICE=$(quote_remote "$SERVICE")
Q_DEADMAN_HELPER=$(quote_remote "$DEADMAN_HELPER")
Q_RELAY_CA=$(quote_remote "$RELAY_CA")

validate_remote_runtime_wheelhouse() {
  ssh_vm "set -eu
/usr/bin/python3 - $Q_REMOTE_SOURCE_ARCHIVE $Q_REMOTE_WHEELHOUSE \
  $RUNTIME_WHEELHOUSE_MANIFEST_SHA256 $RUNTIME_REQUIREMENTS_SHA256 \
  $UV_LOCK_SHA256 <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
import tarfile

WHEEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{1,180}\.whl$')


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('cle JSON dupliquee')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f'constante JSON interdite: {value}')


source_archive, wheelhouse, expected_sha256, requirements_sha256, uv_lock_sha256 = sys.argv[1:]
try:
    with tarfile.open(source_archive, mode='r:') as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name == 'deploy/runtime/ava-runtime-wheelhouse.v1.json'
        ]
        if len(members) != 1 or not members[0].isreg() or not 1 <= members[0].size <= 2 * 1024 * 1024:
            raise ValueError('manifeste source absent ou non regulier')
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError('manifeste source illisible')
        payload = stream.read()
except (OSError, tarfile.TarError, ValueError) as error:
    raise SystemExit('archive source sans manifeste wheelhouse exact') from error
if len(payload) != members[0].size or hashlib.sha256(payload).hexdigest() != expected_sha256:
    raise SystemExit('manifeste wheelhouse divergent du pin du commit')
try:
    document = json.loads(
        payload.decode('ascii'),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
except (UnicodeError, json.JSONDecodeError, ValueError) as error:
    raise SystemExit('manifeste wheelhouse JSON invalide') from error
canonical = (
    json.dumps(document, allow_nan=False, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
    + '\n'
).encode('ascii')
if payload != canonical or type(document) is not dict or set(document) != {
    'entries',
    'requirements_sha256',
    'schema_version',
    'target',
    'uv_lock_sha256',
}:
    raise SystemExit('manifeste wheelhouse distant non canonique')
if (
    document['schema_version'] != 'ava.runtime-wheelhouse/v1'
    or document['target']
    != {
        'implementation': 'cpython',
        'platform': 'linux_x86_64',
        'python_version': '3.12.13',
    }
    or document['requirements_sha256'] != f'sha256:{requirements_sha256}'
    or document['uv_lock_sha256'] != f'sha256:{uv_lock_sha256}'
):
    raise SystemExit('manifeste wheelhouse distant divergent des sources runtime')
entries = document['entries']
if type(entries) is not list or not 1 <= len(entries) <= 1000:
    raise SystemExit('manifeste wheelhouse distant vide ou hors borne')
expected = {}
digests = set()
for entry in entries:
    if (
        type(entry) is not dict
        or set(entry) != {'filename', 'sha256'}
        or type(entry['filename']) is not str
        or WHEEL_RE.fullmatch(entry['filename']) is None
        or type(entry['sha256']) is not str
        or re.fullmatch(r'sha256:[0-9a-f]{64}', entry['sha256']) is None
        or entry['filename'] in expected
        or entry['filename'].casefold() in {name.casefold() for name in expected}
        or entry['sha256'] in digests
    ):
        raise SystemExit('entree du manifeste wheelhouse distant invalide')
    expected[entry['filename']] = entry['sha256']
    digests.add(entry['sha256'])
if list(expected) != sorted(expected, key=lambda value: value.encode('ascii')):
    raise SystemExit('manifeste wheelhouse distant non trie')

flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
descriptor = os.open(wheelhouse, flags)
try:
    directory_before = os.fstat(descriptor)
    names = sorted(os.listdir(descriptor), key=lambda value: value.encode('ascii'))
    if names != list(expected):
        raise SystemExit('set de noms du wheelhouse distant divergent du manifeste')
    total = 0
    for filename in names:
        wheel_descriptor = os.open(
            filename,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        try:
            before = os.fstat(wheel_descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 1 <= before.st_size <= 512 * 1024 * 1024
            ):
                raise SystemExit('wheel distante non reguliere ou hors taille')
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(wheel_descriptor, 1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(wheel_descriptor)
            identity = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if size != before.st_size or identity(before) != identity(after):
                raise SystemExit('wheel distante modifiee pendant lecture')
            observed = f'sha256:{digest.hexdigest()}'
            if observed != expected[filename]:
                raise SystemExit('SHA du wheelhouse distant divergent du manifeste')
            total += size
            if total > 2 * 1024 * 1024 * 1024:
                raise SystemExit('wheelhouse distant hors taille')
        finally:
            os.close(wheel_descriptor)
    directory_after = os.fstat(descriptor)
    if identity(directory_before) != identity(directory_after):
        raise SystemExit('wheelhouse distant modifie pendant lecture')
finally:
    os.close(descriptor)
PY" || fatal "wheelhouse distant divergent du manifeste source canonique"
}

LOCK_HELD=0
LOCK_MAYBE_OWNED=0
SWITCHED=0
DEADMAN_ARMED=0
PREVIOUS_TARGET=""
CURRENT_EXPECTATION=""
STOP_ON_FAILURE=0
DEFERRED_SIGNAL_STATUS=0

defer_termination_signals() {
  DEFERRED_SIGNAL_STATUS=0
  trap 'DEFERRED_SIGNAL_STATUS=129' HUP
  trap 'DEFERRED_SIGNAL_STATUS=130' INT
  trap 'DEFERRED_SIGNAL_STATUS=143' TERM
}

restore_termination_signals() {
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

exit_on_deferred_signal() {
  local status=$DEFERRED_SIGNAL_STATUS
  DEFERRED_SIGNAL_STATUS=0
  [[ "$status" -eq 0 ]] || exit "$status"
}

create_deployment_lock_remote() {
  ssh_vm "set -eu
# ava-lock-publish-v2
/usr/bin/python3 -c \"import ctypes
import errno
import fcntl
import os
import stat
import sys

temporary, cancelled, final, control, marker, attribute, token = sys.argv[1:]
marker_name = os.path.basename(marker)
AT_FDCWD = -100
RENAME_NOREPLACE = 1

def rename_noreplace(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, 'renameat2', None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, 'renameat2 indisponible')
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source, destination)

def cleanup_staging(path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    try:
        owner = os.getxattr(path, attribute).decode('ascii')
    except OSError as error:
        if error.errno == errno.ENOENT:
            return True
        if error.errno != errno.ENODATA:
            return False
        owner = ''
    if owner not in ('', token):
        return False
    try:
        entries = os.listdir(path)
    except FileNotFoundError:
        return True
    if entries:
        if entries != [marker_name] or owner != token:
            return False
        marker_path = os.path.join(path, marker_name)
        try:
            descriptor = os.open(marker_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return not os.path.lexists(path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            marker_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_size != 0:
                return False
        finally:
            os.close(descriptor)
        try:
            os.unlink(marker_path)
        except FileNotFoundError:
            pass
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    return True

def control_matches(descriptor):
    try:
        named = os.stat(control, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(descriptor)
    return (stat.S_ISREG(named.st_mode) and named.st_dev == opened.st_dev
            and named.st_ino == opened.st_ino)

def read_control(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    return os.read(descriptor, 4096)

def write_control(descriptor, state):
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    os.write(descriptor, (state + ':' + token + '\\n').encode('ascii'))
    os.fsync(descriptor)

def unlink_control(descriptor):
    if control_matches(descriptor):
        os.unlink(control)

marker_descriptor = None
directory_descriptor = None
control_descriptor = None
control_owned = False
try:
    control_descriptor = os.open(control, os.O_RDWR | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(control_descriptor).st_mode):
        raise OSError(errno.EPERM, 'controle de verrou non regulier')
    fcntl.flock(control_descriptor, fcntl.LOCK_EX)
    state = read_control(control_descriptor)
    if state == ('cancel:' + token + '\\n').encode('ascii'):
        raise SystemExit(125)
    control_owner = os.getxattr(control_descriptor, attribute).decode('ascii')
    if (state != ('ready:' + token + '\\n').encode('ascii')
            or control_owner != token or not control_matches(control_descriptor)):
        raise OSError(errno.EPERM, 'controle de verrou invalide')
    control_owned = True
    directory_descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_metadata = os.fstat(directory_descriptor)
    named_metadata = os.stat(temporary, follow_symlinks=False)
    if (not stat.S_ISDIR(named_metadata.st_mode)
            or named_metadata.st_dev != directory_metadata.st_dev
            or named_metadata.st_ino != directory_metadata.st_ino
            or os.getxattr(directory_descriptor, attribute).decode('ascii') != token
            or os.listdir(directory_descriptor) != [marker_name]):
        raise OSError(errno.EPERM, 'staging de verrou invalide')
    marker_descriptor = os.open(marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
    fcntl.flock(marker_descriptor, fcntl.LOCK_EX)
    marker_metadata = os.fstat(marker_descriptor)
    if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_size != 0:
        raise OSError(errno.EPERM, 'marqueur de verrou invalide')
    rename_noreplace(temporary, final)
    published = os.stat(final, follow_symlinks=False)
    if (published.st_dev != directory_metadata.st_dev
            or published.st_ino != directory_metadata.st_ino):
        # Le chemin final a ete remplace apres notre publication. Ne jamais deplacer ce
        # nouvel inode etranger pour tenter de restaurer le notre.
        raise OSError(errno.EPERM, 'publication de verrou remplacee')
    try:
        os.unlink(marker_name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        pass
    write_control(control_descriptor, 'published')
    unlink_control(control_descriptor)
except SystemExit:
    raise
except OSError as error:
    if marker_descriptor is not None:
        os.close(marker_descriptor)
        marker_descriptor = None
    if directory_descriptor is not None:
        os.close(directory_descriptor)
        directory_descriptor = None
    cleaned_temporary = cleanup_staging(temporary)
    cleaned_cancelled = cleanup_staging(cancelled)
    cleaned = cleaned_temporary and cleaned_cancelled
    if control_descriptor is not None and control_owned:
        try:
            write_control(control_descriptor, 'failed')
            if cleaned:
                unlink_control(control_descriptor)
        except OSError:
            pass
    if getattr(error, 'errno', None) == errno.EEXIST:
        raise SystemExit(17)
    raise
finally:
    if marker_descriptor is not None:
        os.close(marker_descriptor)
    if directory_descriptor is not None:
        os.close(directory_descriptor)
    if control_descriptor is not None:
        os.close(control_descriptor)
\" $Q_LOCK_TEMP $Q_LOCK_CANCEL $Q_LOCK $Q_LOCK_CONTROL $Q_LOCK_TEMP_MARKER $Q_LOCK_OWNER_XATTR $Q_LOCK_TOKEN"
}

cancel_or_observe_lock_publication() {
  ssh_vm "set -eu
# ava-lock-cancel-v1
/usr/bin/python3 -c \"import ctypes
import errno
import os
import stat
import sys

temporary, cancelled, final, control, marker_name, attribute, token = sys.argv[1:]
AT_FDCWD = -100
RENAME_NOREPLACE = 1

def rename_noreplace(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, 'renameat2', None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, 'renameat2 indisponible')
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source, destination)

def read_owner(path):
    try:
        return os.getxattr(path, attribute).decode('ascii')
    except OSError as error:
        if error.errno == errno.ENODATA:
            return ''
        raise

def cleanup_staging(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        try:
            named = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        opened = os.fstat(descriptor)
        if (not stat.S_ISDIR(named.st_mode) or named.st_dev != opened.st_dev
                or named.st_ino != opened.st_ino):
            return False
        try:
            owner = os.getxattr(descriptor, attribute).decode('ascii')
        except OSError as error:
            if error.errno != errno.ENODATA:
                return False
            owner = ''
        if owner not in ('', token):
            return False
        entries = os.listdir(descriptor)
        if entries:
            if entries != [marker_name] or owner != token:
                return False
            marker_descriptor = os.open(marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                marker_metadata = os.fstat(marker_descriptor)
                if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_size != 0:
                    return False
            finally:
                os.close(marker_descriptor)
            os.unlink(marker_name, dir_fd=descriptor)
        try:
            named = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return os.fstat(descriptor).st_nlink == 0
        if named.st_dev != opened.st_dev or named.st_ino != opened.st_ino:
            return False
        os.rmdir(path)
        return not os.path.lexists(path) and os.fstat(descriptor).st_nlink == 0
    finally:
        os.close(descriptor)

def inspect_final():
    try:
        metadata = os.lstat(final)
    except FileNotFoundError:
        return 'absent'
    if not stat.S_ISDIR(metadata.st_mode):
        return 'invalid'
    try:
        owner = read_owner(final)
        entries = os.listdir(final)
    except FileNotFoundError:
        return 'absent'
    if entries:
        marker = os.path.join(final, marker_name)
        if entries != [marker_name] or os.path.islink(marker) or not os.path.isfile(marker):
            return 'invalid'
        try:
            payload = open(marker, 'rb').read()
        except OSError:
            return 'invalid'
        if not owner or payload not in (b'', (owner + '\\n').encode('ascii')):
            return 'invalid'
        return 'owned' if owner == token else 'foreign'
    if not owner:
        return 'foreign'
    if len(owner) != 64 or any(character not in '0123456789abcdef' for character in owner):
        return 'invalid'
    return 'owned' if owner == token else 'foreign'

def cleanup_control():
    try:
        descriptor = os.open(control, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        opened = os.fstat(descriptor)
        try:
            named = os.stat(control, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if (not stat.S_ISREG(named.st_mode) or named.st_dev != opened.st_dev
                or named.st_ino != opened.st_ino):
            return False
        try:
            owner = os.getxattr(descriptor, attribute).decode('ascii')
        except OSError as error:
            if error.errno != errno.ENODATA or opened.st_size != 0:
                return False
            owner = ''
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, 4096)
        # L'xattr est pose atomiquement avant le premier write. Une coupure peut
        # donc tronquer ready/published/failed sans rendre l'objet ambigu : inode
        # et owner tokennes suffisent. Sans owner, seul le fichier strictement vide
        # cree avant l'xattr est recuperable.
        if owner == token:
            pass
        elif owner == '' and not payload and opened.st_size == 0:
            pass
        else:
            return False
        os.unlink(control)
        return True
    finally:
        os.close(descriptor)

if not cleanup_staging(cancelled):
    print('invalid', end='')
    raise SystemExit(0)

cancelled_source = False
if os.path.lexists(temporary):
    try:
        rename_noreplace(temporary, cancelled)
        cancelled_source = True
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise

if cancelled_source and not cleanup_staging(cancelled):
    print('invalid', end='')
    raise SystemExit(0)

final_state = inspect_final()
if final_state != 'invalid' and not cleanup_control():
    final_state = 'invalid'
print('cancelled' if final_state == 'absent' else final_state, end='')
\" $Q_LOCK_TEMP $Q_LOCK_CANCEL $Q_LOCK $Q_LOCK_CONTROL .ava-deploy-owner $Q_LOCK_OWNER_XATTR $Q_LOCK_TOKEN"
}

prepare_deployment_lock_control_remote() {
  ssh_vm "set -eu
# ava-lock-control-v1
/usr/bin/python3 -c \"import ctypes
import errno
import fcntl
import os
import stat
import sys

temporary, marker_name, control, attribute, token = sys.argv[1:]
payload = ('ready:' + token + '\\n').encode('ascii')
AT_FDCWD = -100
RENAME_NOREPLACE = 1

def require_rename_noreplace():
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, 'renameat2', None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, 'renameat2 indisponible')
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    # Des noms vides ne peuvent rien muter. ENOENT prouve toutefois que le noyau et
    # le filtre de syscalls acceptent bien RENAME_NOREPLACE avant tout staging.
    if renameat2(AT_FDCWD, b'', AT_FDCWD, b'', RENAME_NOREPLACE) == 0:
        raise OSError(errno.EIO, 'probe renameat2 inattendu')
    error = ctypes.get_errno()
    if error != errno.ENOENT:
        raise OSError(error, os.strerror(error))

require_rename_noreplace()
try:
    os.mkdir(temporary, 0o700)
except FileExistsError:
    pass
directory_descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    opened_directory = os.fstat(directory_descriptor)
    named_directory = os.stat(temporary, follow_symlinks=False)
    if (not stat.S_ISDIR(named_directory.st_mode)
            or named_directory.st_dev != opened_directory.st_dev
            or named_directory.st_ino != opened_directory.st_ino):
        raise SystemExit(81)
    try:
        owner = os.getxattr(directory_descriptor, attribute).decode('ascii')
    except OSError as error:
        if error.errno != errno.ENODATA:
            raise
        os.setxattr(directory_descriptor, attribute, token.encode('ascii'), os.XATTR_CREATE)
        owner = token
    if owner != token:
        raise SystemExit(81)
    try:
        marker_descriptor = os.open(
            marker_name,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
    except FileExistsError:
        marker_descriptor = os.open(
            marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor
        )
    try:
        marker_metadata = os.fstat(marker_descriptor)
        if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_size != 0:
            raise SystemExit(81)
        os.fsync(marker_descriptor)
    finally:
        os.close(marker_descriptor)
    if os.listdir(directory_descriptor) != [marker_name]:
        raise SystemExit(81)
    os.fsync(directory_descriptor)

    flags = os.O_RDWR | os.O_NOFOLLOW
    try:
        descriptor = os.open(control, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(control, flags)
        created = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SystemExit(81)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if created:
            os.setxattr(descriptor, attribute, token.encode('ascii'), os.XATTR_CREATE)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        else:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if (os.getxattr(descriptor, attribute).decode('ascii') != token
                    or os.read(descriptor, 4096) != payload):
                raise SystemExit(81)
        named = os.stat(control, follow_symlinks=False)
        if named.st_dev != metadata.st_dev or named.st_ino != metadata.st_ino:
            raise SystemExit(81)
    finally:
        os.close(descriptor)
    named_directory = os.stat(temporary, follow_symlinks=False)
    if (named_directory.st_dev != opened_directory.st_dev
            or named_directory.st_ino != opened_directory.st_ino):
        raise SystemExit(81)
finally:
    os.close(directory_descriptor)
print('ready', end='')
\" $Q_LOCK_TEMP .ava-deploy-owner $Q_LOCK_CONTROL $Q_LOCK_OWNER_XATTR $Q_LOCK_TOKEN"
}

settle_deployment_lock() {
  local state
  for _ in 1 2; do
    if state=$(cancel_or_observe_lock_publication); then
      printf '%s' "$state"
      return 0
    fi
  done
  return 1
}

prepare_lock_for_reclaim() {
  ssh_vm "set -eu
if [ ! -e $Q_LOCK ] && [ ! -L $Q_LOCK ]; then
  exit 0
fi
test -d $Q_LOCK
test ! -L $Q_LOCK
owner=\$(/usr/bin/python3 -c \"import os,sys; sys.stdout.write(os.getxattr(sys.argv[1], sys.argv[2]).decode('ascii'))\" $Q_LOCK $Q_LOCK_OWNER_XATTR 2>/dev/null || true)
if [ -n \"\$owner\" ]; then
  case \"\$owner\" in *[!0-9a-f]*|'') exit 81 ;; esac
  [ \"\${#owner}\" -eq 64 ] || exit 81
fi
if [ -e $Q_LOCK_MARKER ] || [ -L $Q_LOCK_MARKER ]; then
  test -n \"\$owner\"
  test -f $Q_LOCK_MARKER
  test ! -L $Q_LOCK_MARKER
  marker_payload=\$(cat -- $Q_LOCK_MARKER)
  [ -z \"\$marker_payload\" ] || [ \"\$marker_payload\" = \"\$owner\" ]
  /usr/bin/python3 -c \"import os,sys; lock=sys.argv[1]; marker=sys.argv[2]; metadata=os.stat(lock, follow_symlinks=False); os.unlink(marker); os.utime(lock, ns=(metadata.st_atime_ns, metadata.st_mtime_ns), follow_symlinks=False)\" $Q_LOCK $Q_LOCK_MARKER
fi"
}

release_deployment_lock() {
  local result
  result=$(ssh_vm "set -eu
# ava-lock-release-v2
# La suppression porte sur un tombstone tokenne. Le descripteur ouvert avant le
# rename doit designer le meme inode apres celui-ci ; un replacement est restaure
# sans ecrasement et classe foreign, jamais supprime ni annonce comme succes.
/usr/bin/python3 -c \"import ctypes
import errno
import os
import stat
import sys

final, tombstone, marker_name, attribute, token = sys.argv[1:]
AT_FDCWD = -100
RENAME_NOREPLACE = 1

def rename_noreplace(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, 'renameat2', None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, 'renameat2 indisponible')
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source, destination)

def same_inode(path, descriptor):
    try:
        named = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(descriptor)
    return (stat.S_ISDIR(named.st_mode) and named.st_dev == opened.st_dev
            and named.st_ino == opened.st_ino)

def open_owned_empty(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    try:
        if not same_inode(path, descriptor):
            raise OSError(errno.EPERM, 'inode de verrou remplace')
        if os.getxattr(descriptor, attribute).decode('ascii') != token:
            raise OSError(errno.EPERM, 'proprietaire de verrou etranger')
        entries = os.listdir(descriptor)
        if entries:
            if entries != [marker_name]:
                raise OSError(errno.ENOTEMPTY, 'verrou non vide')
            marker_descriptor = os.open(marker_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                marker = os.fstat(marker_descriptor)
                if not stat.S_ISREG(marker.st_mode):
                    raise OSError(errno.EPERM, 'marqueur de verrou non regulier')
                payload = os.read(marker_descriptor, 4096)
                if payload not in (b'', (token + '\\n').encode('ascii')):
                    raise OSError(errno.EPERM, 'marqueur de verrou etranger')
            finally:
                os.close(marker_descriptor)
            os.unlink(marker_name, dir_fd=descriptor)
        if os.listdir(descriptor):
            raise OSError(errno.ENOTEMPTY, 'verrou non vide apres marqueur')
        return descriptor
    except (OSError, UnicodeError):
        os.close(descriptor)
        return None

def remove_exact(path, descriptor):
    if not same_inode(path, descriptor):
        return False
    os.rmdir(path)
    return not os.path.lexists(path) and os.fstat(descriptor).st_nlink == 0

def restore_foreign():
    try:
        rename_noreplace(tombstone, final)
    except OSError:
        return False
    return True

if os.path.lexists(tombstone):
    abandoned = open_owned_empty(tombstone)
    if abandoned is None:
        print('foreign', end='')
        raise SystemExit(0)
    try:
        if not remove_exact(tombstone, abandoned):
            print('foreign', end='')
            raise SystemExit(0)
    finally:
        os.close(abandoned)

if not os.path.lexists(final):
    print('absent', end='')
    raise SystemExit(0)

descriptor = open_owned_empty(final)
if descriptor is None:
    print('foreign', end='')
    raise SystemExit(0)
try:
    try:
        rename_noreplace(final, tombstone)
    except OSError:
        print('foreign', end='')
        raise SystemExit(0)
    if not same_inode(tombstone, descriptor):
        restore_foreign()
        print('foreign', end='')
        raise SystemExit(0)
    if not remove_exact(tombstone, descriptor):
        print('foreign', end='')
        raise SystemExit(0)
finally:
    os.close(descriptor)
print('removed', end='')
\" $Q_LOCK $Q_LOCK_RELEASE .ava-deploy-owner $Q_LOCK_OWNER_XATTR $Q_LOCK_TOKEN") || return 1
  [[ "$result" == "removed" || "$result" == "absent" ]]
}

try_acquire_deployment_lock() {
  local attempt_failed=0 preflight state
  defer_termination_signals
  # Le staging tokenne existe avant tout creator. Meme si ce preflight revient de
  # facon ambigue, l'annulation dispose deja de la source atomique a neutraliser.
  LOCK_MAYBE_OWNED=1
  if ! preflight=$(prepare_deployment_lock_control_remote) || [[ "$preflight" != "ready" ]]; then
    if state=$(settle_deployment_lock) && [[ "$state" == "cancelled" || "$state" == "foreign" ]]; then
      LOCK_MAYBE_OWNED=0
    fi
    restore_termination_signals
    exit_on_deferred_signal
    fatal "preflight du controle de verrou Ava non confirme"
  fi
  # Creator et annuleur publient depuis exactement le meme nom/inode temporaire ;
  # RENAME_NOREPLACE garantit qu'un seul peut gagner vers final ou cancel.
  create_deployment_lock_remote || attempt_failed=1
  if ! state=$(settle_deployment_lock); then
    restore_termination_signals
    exit_on_deferred_signal
    fatal "annulation ou observation atomique du verrou Ava indeterminee"
  fi
  case "$state" in
    owned)
      LOCK_HELD=1
      LOCK_MAYBE_OWNED=0
      ;;
    foreign)
      LOCK_MAYBE_OWNED=0
      ;;
    cancelled)
      LOCK_MAYBE_OWNED=0
      restore_termination_signals
      exit_on_deferred_signal
      if [[ "$attempt_failed" -eq 1 ]]; then
        fatal "publication du verrou Ava annulee apres un echec distant"
      fi
      fatal "publication du verrou Ava annulee sans resultat distant coherent"
      ;;
    *)
      restore_termination_signals
      exit_on_deferred_signal
      fatal "etat distant du verrou Ava invalide"
      ;;
  esac
  restore_termination_signals
  exit_on_deferred_signal
  [[ "$state" == "owned" ]]
}

cleanup_maybe_owned_lock() {
  local state
  [[ "$LOCK_MAYBE_OWNED" -eq 1 ]] || return 0
  state=$(settle_deployment_lock) || return 1
  case "$state" in
    owned)
      LOCK_HELD=1
      LOCK_MAYBE_OWNED=0
      if release_deployment_lock; then
        LOCK_HELD=0
        return 0
      fi
      return 1
      ;;
    cancelled|foreign)
      LOCK_MAYBE_OWNED=0
      return 0
      ;;
    *) return 1 ;;
  esac
}

inspect_release_state() {
  ssh_vm "set -eu
# ava-release-state-v1
if [ ! -e $Q_RELEASE ] && [ ! -L $Q_RELEASE ]; then
  printf absent
elif [ -d $Q_RELEASE ] && [ ! -L $Q_RELEASE ] \
  && [ -f $Q_RELEASE/.ava-ready ] && [ ! -L $Q_RELEASE/.ava-ready ]; then
  printf ready
else
  printf incomplete
fi"
}

inspect_staging_state() {
  ssh_vm "set -eu
# ava-staging-state-v1
if [ ! -e $Q_STAGING ] && [ ! -L $Q_STAGING ]; then
  printf absent
elif [ -d $Q_STAGING ] && [ ! -L $Q_STAGING ] \
  && [ -f $Q_STAGING/.ava-ready ] && [ ! -L $Q_STAGING/.ava-ready ] \
  && ! find $Q_STAGING -xdev -perm /0222 -print -quit | grep -q .; then
  printf ready
else
  printf incomplete
fi"
}

inspect_current_state() {
  ssh_vm "sudo -n $Q_DEADMAN_HELPER inspect-current"
}

arm_deadman() {
  local result q_expect_current
  q_expect_current=$(quote_remote "$CURRENT_EXPECTATION")
  result=$(ssh_vm "sudo -n $Q_DEADMAN_HELPER arm --candidate $Q_RELEASE --expect-current $q_expect_current --ttl $DEADMAN_TTL_SECONDS") \
    || fatal "impossible d'armer le dead-man distant"
  [[ "$result" == "armed" ]] || fatal "reponse d'armement dead-man inattendue"
  DEADMAN_ARMED=1
}

activate_deadman() {
  local result
  result=$(ssh_vm "sudo -n $Q_DEADMAN_HELPER activate --candidate $Q_RELEASE") \
    || fatal "activation authoritative refusee par le dead-man"
  [[ "$result" == "activated" ]] || fatal "reponse d'activation dead-man inattendue"
}

confirm_deadman() {
  local result
  result=$(ssh_vm "sudo -n $Q_DEADMAN_HELPER confirm --candidate $Q_RELEASE") \
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
  result=$(ssh_vm "sudo -n $Q_DEADMAN_HELPER cancel --candidate $Q_RELEASE") || return 1
  [[ "$result" == "cancelled" || "$result" == "not-armed" ]] || return 1
  DEADMAN_ARMED=0
}

acquire_deployment_lock() {
  local result
  ssh_vm "set -eu
mkdir -p -- $Q_RELEASE_ROOT
test -d $Q_RELEASE_ROOT
test ! -L $Q_RELEASE_ROOT
test -d $Q_AUTHORITATIVE_ROOT
test ! -L $Q_AUTHORITATIVE_ROOT
test -d $Q_CURRENT_PARENT
test ! -L $Q_CURRENT_PARENT"
  if ! try_acquire_deployment_lock; then
    if [[ "$PREPARE_ONLY" -eq 1 ]]; then
      fatal "un autre deploiement ou une autre preparation Ava detient deja le verrou"
    fi
    prepare_lock_for_reclaim \
      || fatal "proprietaire du verrou existant invalide avant reclamation"
    result=$(ssh_vm "sudo -n $Q_DEADMAN_HELPER reclaim-lock --stale-after $LOCK_STALE_SECONDS" 2>/dev/null || true)
    case "$result" in
      reclaimed|not-locked)
        try_acquire_deployment_lock \
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
  if ssh_vm "find $Q_RELEASE_ROOT -mindepth 1 -maxdepth 1 -type d -name '.backup-use.*' -print -quit | grep -q ."; then
    if release_deployment_lock; then
      LOCK_HELD=0
    fi
    fatal "une sauvegarde utilise actuellement les releases Ava"
  fi
}

health_check() {
  local expected_target=${1:-$RELEASE_PATH}
  local service_state relay_state tls_relay_state code_direct code_relay code_tls
  local code_engine_direct code_engine_relay code_engine_tls active_target main_pid main_cwd
  local main_exe expected_exe q_expected_target current_state expected_sha
  q_expected_target=$(quote_remote "$expected_target")
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
  [[ "$expected_target" == "$AUTHORITATIVE_RELEASE_ROOT/"* ]] || {
    echo "   x health refuse hors racine authoritative" >&2
    return 1
  }
  expected_sha=${expected_target##*/}
  [[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
  current_state=$(inspect_current_state 2>/dev/null || true)
  active_target=$(ssh_vm "readlink -f -- $Q_CURRENT" 2>/dev/null || true)
  [[ "$current_state" == "sealed:${expected_sha}" \
    && "$active_target" == "$expected_target" ]] || {
    echo "   x current authoritative ne designe pas la release attendue" >&2
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
  main_exe=$(ssh_vm "readlink -f -- /proc/$main_pid/exe" 2>/dev/null || true)
  expected_exe=$(ssh_vm "readlink -f -- $q_expected_target/.venv/bin/python" 2>/dev/null || true)
  [[ -n "$expected_exe" && "$main_exe" == "$expected_exe" ]] || {
    echo "   x le processus actif n'utilise pas l'interpreteur de la release attendue" >&2
    return 1
  }
  ssh_vm "set -eu
AVA_EXPECTED_RELEASE=$q_expected_target AVA_MAIN_PID=$main_pid /usr/bin/python3 -I -S -c \"import os; from pathlib import Path; root=Path(os.environ['AVA_EXPECTED_RELEASE']); pid=int(os.environ['AVA_MAIN_PID']); argv=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0'); expected=[str(root/'.venv/bin/python').encode(),b'-I',b'-S',str(root/'ava_extensions/runtime_bootstrap.py').encode(),b'serve']; assert argv[:5] == expected\"
test -f $q_expected_target/.ava-seal.json
test ! -L $q_expected_target/.ava-seal.json
grep -q '\"format\":\"ava-sealed-release-v1\"' $q_expected_target/.ava-seal.json
grep -q '\"git_sha\":\"${expected_target##*/}\"' $q_expected_target/.ava-seal.json" || {
    echo "   x le processus actif ne passe pas par le bootstrap scelle attendu" >&2
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
$q_expected_target/.venv/bin/python -I -S -c \"import sys; from pathlib import Path; root=Path('$expected_target'); sys.path[:0]=[str(root),str(root/'src'),str(root/'.venv/lib/python3.12/site-packages')]; import anthropic, openjarvis_rust; from openjarvis._rust_bridge import get_rust_module; get_rust_module()\"" >/dev/null
  validate_runtime_policy "$expected_target" || {
    echo "   x persona ou politique runtime hors de la release attendue" >&2
    return 1
  }
}

garbage_collect_releases() {
  # Les releases scellees sont root-owned. Leur retention appartient au role
  # Ansible/helper autoritaire, jamais a ce deployeur non privilegie.
  return 0
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
  local expected_result result stop_reason
  if [[ "$STOP_ON_FAILURE" -eq 1 ]]; then
    if [[ "$CURRENT_EXPECTATION" == "absent" ]]; then
      expected_result=bootstrap-stopped
      stop_reason="echec du premier demarrage"
    else
      expected_result=legacy-stopped
      stop_reason="echec de la migration legacy; 809 ne sera jamais resservie"
    fi
    echo "   ! ${stop_reason} : retour a aucun service actif" >&2
    result=$(ssh_vm "sudo -n $Q_DEADMAN_HELPER rollback --candidate $Q_RELEASE" 2>/dev/null || true)
    if [[ "$result" == "$expected_result" ]]; then
      # Un retour positif du helper signifie que son etat root-owned ET le verrou
      # ont deja ete retires. Les drapeaux locaux doivent le refleter avant tout
      # controle supplementaire, meme si celui-ci echoue ensuite.
      DEADMAN_ARMED=0
      LOCK_HELD=0
      if ssh_vm "test ! -e $Q_CURRENT && test ! -L $Q_CURRENT" \
        && [[ "$(ssh_vm "systemctl is-active $Q_SERVICE" 2>/dev/null || true)" =~ ^(failed|inactive)$ ]] \
        && [[ "$(ssh_vm "systemctl is-active openjarvis-relay.service" 2>/dev/null || true)" =~ ^(failed|inactive)$ ]] \
        && [[ "$(ssh_vm "systemctl is-active openjarvis-relay-tls.service" 2>/dev/null || true)" =~ ^(failed|inactive)$ ]]; then
        echo "   + repli fail-closed confirme; candidate conservee pour diagnostic: $RELEASE_PATH" >&2
      else
        echo "   x CRITIQUE: repli annonce mais etat arrete non confirme; candidate conservee sans dead-man ni verrou" >&2
      fi
    else
      echo "   x CRITIQUE: repli fail-closed non confirme; le dead-man distant reste autoritaire" >&2
    fi
    SWITCHED=0
    return
  fi
  echo "   ! echec apres bascule : restauration de $PREVIOUS_TARGET" >&2
  result=$(ssh_vm "sudo -n $Q_DEADMAN_HELPER rollback --candidate $Q_RELEASE" 2>/dev/null || true)
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
      echo "   x CRITIQUE: cible precedente restauree mais sante etendue non confirmee; candidate conservee sans dead-man ni verrou" >&2
    fi
  else
    echo "   x CRITIQUE: rollback distant non confirme; le dead-man distant reste autoritaire" >&2
  fi
  SWITCHED=0
}

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  set +e
  if [[ "$status" -ne 0 && "$SWITCHED" -eq 1 ]]; then
    rollback
  fi
  if [[ "$LOCK_MAYBE_OWNED" -eq 1 && "$DEADMAN_ARMED" -eq 0 ]]; then
    cleanup_maybe_owned_lock >/dev/null 2>&1 \
      || echo "   x etat final du verrou potentiellement acquis non confirme" >&2
  fi
  if [[ "$LOCK_HELD" -eq 1 && "$DEADMAN_ARMED" -eq 0 ]]; then
    release_deployment_lock >/dev/null 2>&1 || true
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

if [[ "$PREPARE_ONLY" -eq 1 ]]; then
  titre "1/7 Verrou de preparation sans activation"
else
  titre "1/7 Verrou et bootstrap du pointeur current"
fi
acquire_deployment_lock

# La baseline preparee ne consulte jamais current, systemd ou le dead-man. Le mode
# complet obtient au contraire un token root-owned, reutilise sans reinterpretation
# lors de l'armement afin de fermer la fenetre TOCTOU.
if [[ "$PREPARE_ONLY" -eq 0 ]]; then
  ssh_vm "set -eu
test \"\$(sudo -n $Q_DEADMAN_HELPER probe)\" = ready
systemctl is-enabled --quiet ava-deploy-deadman.timer
systemctl is-active --quiet ava-deploy-deadman.timer
systemctl is-enabled --quiet $Q_SERVICE
systemctl is-enabled --quiet openjarvis-relay.service
systemctl is-enabled --quiet openjarvis-relay-tls.service
test -r $Q_RELAY_CA" \
    || fatal "garde dead-man ou unite OpenJarvis non convergee par Ansible"
  CURRENT_EXPECTATION=$(inspect_current_state) \
    || fatal "inspection authoritative du pointeur current indeterminee"
  case "$CURRENT_EXPECTATION" in
    absent)
      STOP_ON_FAILURE=1
      ;;
    legacy:*)
      legacy_sha=${CURRENT_EXPECTATION#legacy:}
      [[ "$legacy_sha" =~ ^[0-9a-f]{40}$ ]] \
        || fatal "token current legacy invalide"
      [[ "$legacy_sha" == "$LEGACY_GIT_SHA" ]] \
        || fatal "token current legacy divergent de l'exception exacte"
      # Le token legacy ferme uniquement le CAS de la premiere activation B. Le
      # runtime 809 est user-owned et ne constitue jamais une cible de rollback.
      STOP_ON_FAILURE=1
      ;;
    sealed:*)
      previous_sha=${CURRENT_EXPECTATION#sealed:}
      [[ "$previous_sha" =~ ^[0-9a-f]{40}$ ]] \
        || fatal "token current scelle invalide"
      PREVIOUS_TARGET="${AUTHORITATIVE_RELEASE_ROOT}/${previous_sha}"
      previous_target_is_safe "$PREVIOUS_TARGET" \
        || fatal "cible precedente scellee hors contrat Ava"
      validate_runtime_policy "$PREVIOUS_TARGET" \
        || fatal "cible precedente scellee hors politique runtime Ava"
      ;;
    *) fatal "etat authoritative non canonique du pointeur current" ;;
  esac
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
LOCAL_RELEASE_MANIFEST="$LOCAL_TMP/.ava-release"
printf '%s\n' "$expected_manifest" > "$LOCAL_RELEASE_MANIFEST"
RELEASE_MANIFEST_SHA256=$(sha256sum -- "$LOCAL_RELEASE_MANIFEST" | awk '{print $1}')
[[ "$RELEASE_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "SHA-256 manifeste release invalide"
if ! release_state=$(inspect_release_state); then
  fatal "inspection distante de la release same-SHA indeterminee"
fi
case "$release_state" in
  ready)
    echo "   + release immutable existante reutilisee et reverifiee par le helper authoritative"
    ;;
  incomplete)
    fatal "release authoritative same-SHA incomplete ou non scellee"
    ;;
  absent) ;;
  *) fatal "etat distant non canonique de la release same-SHA" ;;
esac

if [[ "$release_state" == "absent" ]]; then
  staging_state=$(inspect_staging_state) \
    || fatal "inspection distante du staging same-SHA indeterminee"
  case "$staging_state" in
    ready) echo "   + staging immutable existant reutilise" ;;
    incomplete) fatal "staging same-SHA incomplet conserve; intervention manuelle requise" ;;
    absent) ;;
    *) fatal "etat distant non canonique du staging same-SHA" ;;
  esac
else
  staging_state=not-required
fi

if [[ "$staging_state" == "absent" ]]; then
  ssh_vm "set -eu
mkdir -- $Q_STAGING
mkdir -- $Q_ARTIFACT_DIR
mkdir -- $Q_REMOTE_WHEELHOUSE
printf '%s\n' $ATTENDU > $Q_STAGING/.ava-building"

  titre "3/7 Transfert source et artefacts attestes"
  cat -- "$LOCAL_SOURCE_ARCHIVE" | ssh_vm "cat > $Q_REMOTE_SOURCE_ARCHIVE"
  git -C "$RACINE_LOCALE" archive --format=tar "$ATTENDU" rust \
    | ssh_vm "cat > $Q_REMOTE_RUST_ARCHIVE"
  cat -- "$RUST_WHEEL" | ssh_vm "cat > $Q_REMOTE_WHEEL"
  cat -- "$RUST_ATTESTATION" | ssh_vm "cat > $Q_REMOTE_ATTESTATION"
  cat -- "$LOCAL_EVOLUTIONS" | ssh_vm "cat > $Q_REMOTE_EVOLUTIONS"
  cat -- "$LOCAL_FRONTEND_TAR" | ssh_vm "cat > $Q_REMOTE_FRONTEND_ARCHIVE"
  cat -- "$LOCAL_FRONTEND_BUILD_ATTESTATION" \
    | ssh_vm "cat > $Q_REMOTE_FRONTEND_BUILD_ATTESTATION"
  cat -- "$LOCAL_BUILD_ROOT/$RUNTIME_REQUIREMENTS_PATH" \
    | ssh_vm "cat > $Q_REMOTE_REQUIREMENTS_TMP"
  cat -- "$LOCAL_BUILD_ROOT/$RUNTIME_WHEEL_PATH" \
    | ssh_vm "cat > $Q_REMOTE_WHEELHOUSE/$RUNTIME_WHEEL_FILENAME"

  remote_source_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_SOURCE_ARCHIVE | awk '{print \$1}'")
  remote_rust_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_RUST_ARCHIVE | awk '{print \$1}'")
  remote_wheel_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_WHEEL | awk '{print \$1}'")
  remote_attestation_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_ATTESTATION | awk '{print \$1}'")
  remote_evolutions_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_EVOLUTIONS | awk '{print \$1}'")
  remote_frontend_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_FRONTEND_ARCHIVE | awk '{print \$1}'")
  remote_frontend_attestation_hash=$(ssh_vm \
    "sha256sum -- $Q_REMOTE_FRONTEND_BUILD_ATTESTATION | awk '{print \$1}'")
  remote_requirements_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_REQUIREMENTS_TMP | awk '{print \$1}'")
  remote_runtime_wheel_hash=$(ssh_vm "sha256sum -- $Q_REMOTE_WHEELHOUSE/$RUNTIME_WHEEL_FILENAME | awk '{print \$1}'")
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
  [[ "$remote_frontend_hash" == "$FRONTEND_STATIC_SHA256" ]] \
    || fatal "archive frontend alteree pendant le transfert"
  [[ "$remote_frontend_attestation_hash" == "$FRONTEND_BUILD_ATTESTATION_SHA256" ]] \
    || fatal "attestation frontend alteree pendant le transfert"
  [[ "$remote_requirements_hash" == "$RUNTIME_REQUIREMENTS_SHA256" ]] \
    || fatal "requirements runtime alterees pendant le transfert"
  [[ "$remote_runtime_wheel_hash" == "$RUNTIME_WHEEL_SHA256" ]] \
    || fatal "wheel runtime source alteree pendant le transfert"
  echo "   + source, frontend, wheels source et attestations verifies avant scellement"

  titre "4/7 Prefill wheelhouse Python 3.12 sans cache implicite"
  ssh_vm "set -eu
# ava-wheelhouse-prefill-v1
test \"\$(sha256sum -- $Q_PYTHON_RUNTIME_ARCHIVE | awk '{print \$1}')\" = $PYTHON_RUNTIME_SHA256
mkdir -- $Q_REMOTE_DOWNLOADER
tar -xf $Q_PYTHON_RUNTIME_ARCHIVE -C $Q_REMOTE_DOWNLOADER
$Q_REMOTE_DOWNLOADER/python/bin/python3.12 -I -m ensurepip --upgrade >/dev/null
$Q_REMOTE_DOWNLOADER/python/bin/python3.12 -I -m pip --isolated download \
  --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: \
  --no-deps --index-url https://pypi.org/simple \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --dest $Q_REMOTE_WHEELHOUSE --find-links $Q_REMOTE_WHEELHOUSE \
  -r $Q_REMOTE_REQUIREMENTS_TMP
find $Q_REMOTE_WHEELHOUSE -mindepth 1 -maxdepth 1 ! -type f -print -quit | grep -q . && exit 78
find $Q_REMOTE_WHEELHOUSE -mindepth 1 -maxdepth 1 -type f ! -name '*.whl' -print -quit | grep -q . && exit 79
test \"\$(find $Q_REMOTE_WHEELHOUSE -mindepth 1 -maxdepth 1 -type f -name '*.whl' | wc -l)\" -gt 1
if find $Q_REMOTE_WHEELHOUSE -mindepth 1 -maxdepth 1 -type f -printf '%f\\n' \
  | grep -Ei '^(triton|nvidia[-_])' \
  | grep -Eiv '^nvidia[-_]ml[-_]py-'; then
  exit 80
fi
rm -rf -- $Q_REMOTE_DOWNLOADER
rm -f -- $Q_REMOTE_REQUIREMENTS_TMP"

  cat -- "$LOCAL_RELEASE_MANIFEST" | ssh_vm "cat > $Q_STAGING/.ava-release"
  ssh_vm "set -eu
printf '%s\n' $ATTENDU > $Q_STAGING/.ava-ready
rm -f -- $Q_STAGING/.ava-building
chmod -R a-w -- $Q_STAGING"
fi

validate_remote_runtime_wheelhouse
echo "   + wheelhouse distant identique au manifeste source canonique"

titre "5/7 Scellement root-owned et verification same-SHA"
Q_UV_VERSION=$(quote_remote "$UV_VERSION")
seal_result=$(ssh_vm "sudo -n $Q_SEAL_HELPER seal \
  --git-sha $ATTENDU \
  --release-manifest-sha256 $RELEASE_MANIFEST_SHA256 \
  --source-tree-sha256 $SOURCE_TREE_SHA256 \
  --frontend-static-sha256 $FRONTEND_STATIC_SHA256 \
  --frontend-build-attestation-sha256 $FRONTEND_BUILD_ATTESTATION_SHA256 \
  --rust-tree-sha256 $RUST_TREE_SHA256 \
  --rust-wheel-sha256 $WHEEL_SHA256 \
  --rust-attestation-sha256 $ATTESTATION_SHA256 \
  --evolutions-sha256 $EVOLUTIONS_SHA256 \
  --uv-lock-sha256 $UV_LOCK_SHA256 \
  --runtime-requirements-sha256 $RUNTIME_REQUIREMENTS_SHA256 \
  --runtime-wheelhouse-manifest-sha256 $RUNTIME_WHEELHOUSE_MANIFEST_SHA256 \
  --runtime-wheel-pin $RUNTIME_WHEEL_FILENAME=$RUNTIME_WHEEL_SHA256 \
  --python-runtime-archive $Q_PYTHON_RUNTIME_ARCHIVE \
  --python-runtime-source-sha256 $PYTHON_RUNTIME_SOURCE_SHA256 \
  --python-runtime-sha256 $PYTHON_RUNTIME_SHA256 \
  --uv-sha256 $UV_SHA256 \
  --uv-version $Q_UV_VERSION") \
  || fatal "scellement root-owned refuse"
[[ "$seal_result" == "sealed" || "$seal_result" == "already-sealed" ]] \
  || fatal "reponse de scellement inattendue"
ssh_vm "set -eu
test -d $Q_RELEASE
test ! -L $Q_RELEASE
test -f $Q_RELEASE/.ava-seal.json
test ! -L $Q_RELEASE/.ava-seal.json
test \"\$(cat -- $Q_RELEASE/.ava-ready)\" = $ATTENDU
if find $Q_RELEASE -xdev -perm /0222 -print -quit | grep -q .; then exit 76; fi"

if [[ "$PREPARE_ONLY" -eq 1 ]]; then
  release_deployment_lock || fatal "verrou de preparation distant non libere"
  LOCK_HELD=0
  printf '\n\033[1;32mRelease %s preparee, immutable et non activee.\033[0m\n' "${ATTENDU:0:12}"
  exit 0
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
  if [[ "$STOP_ON_FAILURE" -eq 0 ]]; then
    previous_target_is_safe "$PREVIOUS_TARGET" \
      || fatal "cible de rollback hors contrat avant armement"
    validate_runtime_policy "$PREVIOUS_TARGET" \
      || fatal "politique de la cible scellee de rollback invalide avant armement"
  fi
  # L'etat root-owned et son timer sont ensuite armes AVANT l'ecriture distante. Ils
  # ne dependent plus de cette session SSH apres la bascule.
  arm_deadman
  SWITCHED=1
  activate_deadman
fi
ssh_vm "set -eu
sudo -n systemctl restart $Q_SERVICE
sudo -n systemctl restart openjarvis-relay.service openjarvis-relay-tls.service"

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
