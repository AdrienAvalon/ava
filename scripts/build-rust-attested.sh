#!/usr/bin/env bash
# Construit dans un conteneur epingle la wheel Rust d'un commit Ava propre.
#
# L'attestation produite est volontairement un manifeste de checksums NON SIGNE.
# Elle prouve la coherence locale artefact/source/builder, pas l'identite d'un
# auteur ni l'integrite face a un operateur capable de reecrire ces trois objets.
set -Eeuo pipefail
umask 077

ROOT=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
DOCKER_BIN="${AVA_DOCKER_BIN:-docker}"
SAFE_EXEC_PATH='/usr/local/bin:/usr/bin:/bin'
ENV_BIN='/usr/bin/env'
GIT_BIN='/usr/bin/git'
DOCKERFILE_REL='deploy/docker/Dockerfile.rust-builder'
CANONICALIZER_REL='deploy/docker/canonicalize-rust-wheel.py'
TARGET_PLATFORM='linux/amd64'
PYTHON_IMAGE='python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf'
RUST_IMAGE='rust:1.88.0-bookworm@sha256:4727898c104ecd2e22d780925832502faee9fe4e70581b8572af081370b315a0'
PYTHON_VERSION='3.12.13'
RUST_VERSION='1.88.0'
MATURIN_VERSION='1.14.1'

fatal() {
  printf 'ERREUR: %s\n' "$*" >&2
  exit 1
}

[[ -x "$ENV_BIN" && -f "$ENV_BIN" && ! -L "$ENV_BIN" ]] \
  || fatal "env systeme hermetique introuvable"
[[ -x "$GIT_BIN" && -f "$GIT_BIN" && ! -L "$GIT_BIN" ]] \
  || fatal "git systeme hermetique introuvable"

# Aucun GIT_* de l'appelant, fichier de configuration global/systeme, HOME ou
# XDG reel ne doit pouvoir rediriger le checkout, les objets ou les refs lus.
run_git() {
  "$ENV_BIN" -i \
    PATH="$SAFE_EXEC_PATH" \
    HOME=/dev/null/ava-git-home-does-not-exist \
    XDG_CONFIG_HOME=/dev/null/ava-git-xdg-does-not-exist \
    LC_ALL=C \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_NO_REPLACE_OBJECTS=1 \
    "$GIT_BIN" "$@"
}

command -v "$DOCKER_BIN" >/dev/null 2>&1 \
  || fatal "docker introuvable ou non executable"
for prerequisite in \
  sha256sum tar awk find install cmp basename dirname id readlink stat python3 mktemp
do
  command -v "$prerequisite" >/dev/null 2>&1 \
    || fatal "$prerequisite introuvable"
done
CURRENT_UID=$(id -u)

private_directory_is_sealed() {
  local candidate=$1
  local owner mode

  [[ "$candidate" == /* && "$candidate" != / \
    && -d "$candidate" && ! -L "$candidate" \
    && "$(readlink -f -- "$candidate")" == "$candidate" ]] \
    || return 1
  owner=$(stat -c '%u' -- "$candidate") || return 1
  mode=$(stat -c '%a' -- "$candidate") || return 1
  [[ "$owner" == "$CURRENT_UID" && "$mode" =~ ^[0-7]{3,4}$ ]] \
    || return 1
  (( (8#$mode & 0022) == 0 ))
}

sticky_temporary_anchor_is_sealed() {
  local candidate=$1

  [[ "$candidate" == /tmp || "$candidate" == /var/tmp ]] || return 1
  [[ -d "$candidate" && ! -L "$candidate" \
    && "$(readlink -f -- "$candidate")" == "$candidate" \
    && "$(stat -c '%u' -- "$candidate")" == 0 \
    && "$(stat -c '%a' -- "$candidate")" == 1777 ]]
}

validate_private_directory_chain() {
  local candidate=$1
  local anchor=$2
  local anchor_policy=$3

  case "$candidate" in
    "$anchor"|"$anchor"/*) ;;
    *) return 1 ;;
  esac
  while [[ "$candidate" != "$anchor" ]]; do
    private_directory_is_sealed "$candidate" || return 1
    candidate=$(dirname -- "$candidate")
  done
  if [[ "$anchor_policy" == sticky ]]; then
    sticky_temporary_anchor_is_sealed "$anchor"
  else
    private_directory_is_sealed "$anchor"
  fi
}

run_git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fatal "le script doit etre execute depuis un checkout Git Ava"
if ! run_git -C "$ROOT" diff --quiet --ignore-submodules -- \
  || ! run_git -C "$ROOT" diff --cached --quiet --ignore-submodules -- \
  || [[ -n "$(run_git -C "$ROOT" ls-files --others --exclude-standard)" ]]; then
  fatal "le checkout Ava doit etre integralement propre avant la construction"
fi

GIT_SHA=$(run_git -C "$ROOT" rev-parse --verify HEAD)
[[ "$GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "SHA Git inattendu"
run_git -C "$ROOT" cat-file -e "${GIT_SHA}:rust" \
  || fatal "le commit ne contient pas le sous-arbre rust"
run_git -C "$ROOT" cat-file -e "${GIT_SHA}:${DOCKERFILE_REL}" \
  || fatal "le commit ne contient pas le Dockerfile du builder"
run_git -C "$ROOT" cat-file -e "${GIT_SHA}:${CANONICALIZER_REL}" \
  || fatal "le commit ne contient pas le canonicalizer de wheel"

OUTPUT_ROOT="${AVA_RUST_ARTIFACT_DIR:-$ROOT/dist/ava-rust/$GIT_SHA}"
[[ "$OUTPUT_ROOT" == /* && "$OUTPUT_ROOT" != / && "$OUTPUT_ROOT" != /home ]] \
  || fatal "repertoire de sortie absolu et borne requis"
OUTPUT_PARENT=$(dirname -- "$OUTPUT_ROOT")
OUTPUT_LEAF=$(basename -- "$OUTPUT_ROOT")
[[ "$OUTPUT_LEAF" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ \
  && "$OUTPUT_ROOT" == "$OUTPUT_PARENT/$OUTPUT_LEAF" ]] \
  || fatal "leaf de sortie non canonique"
[[ -d "$OUTPUT_PARENT" && ! -L "$OUTPUT_PARENT" ]] \
  || fatal "parent de sortie existant, direct et regulier requis"
CANONICAL_OUTPUT_PARENT=$(readlink -f -- "$OUTPUT_PARENT")
[[ "$CANONICAL_OUTPUT_PARENT" == "$OUTPUT_PARENT" ]] \
  || fatal "parent de sortie indirect ou non canonique"
[[ "$(readlink -m -- "$OUTPUT_ROOT")" == "$OUTPUT_ROOT" ]] \
  || fatal "sortie indirecte ou non canonique"

OUTPUT_ANCHOR=''
OUTPUT_ANCHOR_POLICY=''
case "$OUTPUT_PARENT" in
  "$ROOT"|"$ROOT"/*)
    OUTPUT_ANCHOR=$ROOT
    OUTPUT_ANCHOR_POLICY=private
    ;;
  /tmp/*)
    OUTPUT_ANCHOR=/tmp
    OUTPUT_ANCHOR_POLICY=sticky
    ;;
  /var/tmp/*)
    OUTPUT_ANCHOR=/var/tmp
    OUTPUT_ANCHOR_POLICY=sticky
    ;;
esac
if [[ -z "$OUTPUT_ANCHOR" && -n "${RUNNER_TEMP:-}" \
  && "$RUNNER_TEMP" == /* && "$RUNNER_TEMP" != / \
  && -d "$RUNNER_TEMP" && ! -L "$RUNNER_TEMP" \
  && "$(readlink -f -- "$RUNNER_TEMP")" == "$RUNNER_TEMP" ]]; then
    case "$OUTPUT_PARENT" in
      "$RUNNER_TEMP"|"$RUNNER_TEMP"/*)
        OUTPUT_ANCHOR=$RUNNER_TEMP
        OUTPUT_ANCHOR_POLICY=private
        ;;
    esac
fi
[[ -n "$OUTPUT_ANCHOR" \
  && -n "$OUTPUT_ANCHOR_POLICY" ]] \
  || fatal "parent de sortie hors des racines de travail autorisees"
validate_private_directory_chain \
  "$OUTPUT_PARENT" "$OUTPUT_ANCHOR" "$OUTPUT_ANCHOR_POLICY" \
  || fatal "chaine de parents de sortie non scellee"
OUTPUT_PARENT_ID=$(stat -c '%d:%i' -- "$OUTPUT_PARENT")
[[ ! -e "$OUTPUT_ROOT" && ! -L "$OUTPUT_ROOT" ]] \
  || fatal "leaf final deja present ou indirect"

TEMP_PARENT=${TMPDIR:-/tmp}
[[ "$TEMP_PARENT" == /* && "$TEMP_PARENT" != / \
  && -d "$TEMP_PARENT" && ! -L "$TEMP_PARENT" ]] \
  || fatal "parent temporaire absolu, direct et regulier requis"
CANONICAL_TEMP_PARENT=$(readlink -f -- "$TEMP_PARENT")
[[ "$CANONICAL_TEMP_PARENT" == "$TEMP_PARENT" ]] \
  || fatal "parent temporaire indirect ou non canonique"
TEMP_PARENT_UID=$(stat -c '%u' -- "$TEMP_PARENT")
TEMP_PARENT_MODE=$(stat -c '%a' -- "$TEMP_PARENT")
[[ "$TEMP_PARENT_MODE" =~ ^[0-7]{3,4}$ ]] \
  || fatal "mode du parent temporaire inattendu"
case "$TEMP_PARENT" in
  /tmp|/var/tmp)
    [[ "$TEMP_PARENT_UID" == 0 && "$TEMP_PARENT_MODE" == 1777 ]] \
      || fatal "parent temporaire partage sans sticky-bit sur"
    ;;
  *)
    [[ "$TEMP_PARENT_UID" == "$CURRENT_UID" ]] \
      || fatal "parent temporaire detenu par un autre utilisateur"
    (( (8#$TEMP_PARENT_MODE & 0022) == 0 )) \
      || fatal "parent temporaire prive modifiable par un tiers"
    ;;
esac
TEMP_PARENT_ID=$(stat -c '%d:%i' -- "$TEMP_PARENT")
TMP=$(mktemp -d "$TEMP_PARENT/ava-rust-build.XXXXXX")
[[ "$TMP" == "$TEMP_PARENT"/ava-rust-build.* \
  && "$(dirname -- "$TMP")" == "$TEMP_PARENT" \
  && -d "$TMP" && ! -L "$TMP" \
  && "$(readlink -f -- "$TMP")" == "$TMP" \
  && ! -L "$TEMP_PARENT" \
  && "$(readlink -f -- "$TEMP_PARENT")" == "$TEMP_PARENT" \
  && "$(stat -c '%d:%i' -- "$TEMP_PARENT")" == "$TEMP_PARENT_ID" \
  && "$(stat -c '%u' -- "$TMP")" == "$CURRENT_UID" \
  && "$(stat -c '%a' -- "$TMP")" == 700 ]] \
  || fatal "mktemp a retourne un repertoire inattendu"
TMP_ID=$(stat -c '%d:%i' -- "$TMP")
declare -a CONTAINER_IDS=()
declare -a IMAGE_REFS=()
PUBLISH_STAGING=''
cleanup() {
  local container_id image_ref
  for container_id in "${CONTAINER_IDS[@]}"; do
    "$DOCKER_BIN" rm -f "$container_id" >/dev/null 2>&1 || true
  done
  for image_ref in "${IMAGE_REFS[@]}"; do
    "$DOCKER_BIN" image rm -f "$image_ref" >/dev/null 2>&1 || true
  done
  if [[ -n "$TMP" && "$TMP" == "$TEMP_PARENT"/ava-rust-build.* \
    && -d "$TMP" && ! -L "$TMP" \
    && ! -L "$TEMP_PARENT" \
    && "$(readlink -f -- "$TEMP_PARENT" 2>/dev/null || true)" == "$TEMP_PARENT" \
    && "$(stat -c '%d:%i' -- "$TEMP_PARENT" 2>/dev/null || true)" == "$TEMP_PARENT_ID" \
    && "$(stat -c '%d:%i' -- "$TMP" 2>/dev/null || true)" == "$TMP_ID" ]]; then
    rm -rf -- "$TMP"
  fi
  if [[ -n "$PUBLISH_STAGING" \
    && "$PUBLISH_STAGING" == "$OUTPUT_PARENT/.${OUTPUT_LEAF}.tmp."* \
    && -d "$PUBLISH_STAGING" && ! -L "$PUBLISH_STAGING" \
    && -d "$OUTPUT_PARENT" && ! -L "$OUTPUT_PARENT" \
    && "$(readlink -f -- "$OUTPUT_PARENT" 2>/dev/null || true)" == "$OUTPUT_PARENT" \
    && "$(stat -c '%d:%i' -- "$OUTPUT_PARENT" 2>/dev/null || true)" == "$OUTPUT_PARENT_ID" ]]; then
    rm -rf -- "$PUBLISH_STAGING"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$TMP/context"
# Le contexte ne provient jamais du working tree : il est reconstruit depuis le
# commit et borne au Dockerfile, au canonicalizer et au sous-arbre Rust.
run_git -C "$ROOT" archive --format=tar "$GIT_SHA" \
  "$DOCKERFILE_REL" "$CANONICALIZER_REL" rust \
  | tar -xf - -C "$TMP/context"

# Les tags incluent le numero d'essai et le PID : le nettoyage concurrent reste
# borne et chacun des deux builds est force a reconstruire toutes ses couches.
LAST_BUILDER_IMAGE_ID=''
LAST_WHEEL=''
build_once() {
  local build_number=$1
  local export_dir=$2
  local image_ref builder_image_id builder_revision builder_os builder_arch
  local container_id wheel wheel_name
  local -a entries=()

  image_ref="ava-rust-builder:${GIT_SHA}-${build_number}-$$"
  IMAGE_REFS+=("$image_ref")
  "$DOCKER_BIN" build \
    --no-cache \
    --pull \
    --platform "$TARGET_PLATFORM" \
    --file "$TMP/context/$DOCKERFILE_REL" \
    --build-arg "AVA_GIT_SHA=$GIT_SHA" \
    --tag "$image_ref" \
    "$TMP/context"

  builder_image_id=$("$DOCKER_BIN" image inspect --format '{{.Id}}' "$image_ref")
  [[ "$builder_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fatal "identifiant immuable du builder inattendu"
  builder_revision=$("$DOCKER_BIN" image inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image_ref")
  [[ "$builder_revision" == "$GIT_SHA" ]] \
    || fatal "le builder ne porte pas le commit attendu"
  builder_os=$("$DOCKER_BIN" image inspect --format '{{.Os}}' "$image_ref")
  builder_arch=$("$DOCKER_BIN" image inspect --format '{{.Architecture}}' "$image_ref")
  [[ "$builder_os/$builder_arch" == "$TARGET_PLATFORM" ]] \
    || fatal "plateforme builder inattendue: $builder_os/$builder_arch"

  # Le stage final scratch n'a volontairement aucun runtime. Une commande
  # inerte explicite permet seulement `docker create` puis `docker cp`.
  container_id=$("$DOCKER_BIN" create \
    --platform "$TARGET_PLATFORM" "$image_ref" /ava-artifact-export-only)
  [[ "$container_id" =~ ^[0-9a-f]{12,64}$ ]] \
    || fatal "identifiant de conteneur inattendu"
  CONTAINER_IDS+=("$container_id")
  mkdir -p -- "$export_dir"
  "$DOCKER_BIN" cp "$container_id:/artifacts/." "$export_dir/"

  mapfile -d '' entries < <(
    find "$export_dir" -mindepth 1 -maxdepth 1 -print0
  )
  [[ ${#entries[@]} -eq 1 && -f "${entries[0]}" && ! -L "${entries[0]}" ]] \
    || fatal "le builder doit produire exactement une wheel reguliere"
  wheel=${entries[0]}
  wheel_name=$(basename -- "$wheel")
  [[ "$wheel_name" =~ ^[A-Za-z0-9._-]+-cp312-cp312-manylinux_2_36_x86_64\.whl$ ]] \
    || fatal "nom ou compatibilite de wheel inattendu: $wheel_name"
  LAST_BUILDER_IMAGE_ID=$builder_image_id
  LAST_WHEEL=$wheel
}

atomic_publish_directory() {
  local source=$1
  local destination=$2
  python3 - "$source" "$destination" <<'PY'
import ctypes
import errno
import os
import stat
import sys

source, destination = sys.argv[1:]
source_fd = os.open(
    source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
)
try:
    names = os.listdir(source_fd)
    if len(names) != 2:
        raise SystemExit("publication Rust incomplete")
    for name in names:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=source_fd
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SystemExit("publication Rust contient un fichier indirect")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.fsync(source_fd)
finally:
    os.close(source_fd)

libc = ctypes.CDLL(None, use_errno=True)
try:
    renameat2 = libc.renameat2
except AttributeError as exc:
    raise SystemExit("renameat2 indisponible; publication refusee") from exc
renameat2.argtypes = (
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
)
renameat2.restype = ctypes.c_int
if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise SystemExit("leaf final apparu pendant la publication")
    raise SystemExit(f"renameat2 RENAME_NOREPLACE refuse: {os.strerror(error)}")

parent = os.path.dirname(destination)
parent_fd = os.open(
    parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
)
try:
    os.fsync(parent_fd)
finally:
    os.close(parent_fd)
PY
}

build_once 1 "$TMP/export-1"
FIRST_BUILDER_IMAGE_ID=$LAST_BUILDER_IMAGE_ID
FIRST_WHEEL=$LAST_WHEEL
build_once 2 "$TMP/export-2"
SECOND_WHEEL=$LAST_WHEEL

FIRST_WHEEL_NAME=$(basename -- "$FIRST_WHEEL")
SECOND_WHEEL_NAME=$(basename -- "$SECOND_WHEEL")
[[ "$FIRST_WHEEL_NAME" == "$SECOND_WHEEL_NAME" ]] \
  || fatal "les deux builds Rust divergent sur le nom de wheel"
cmp --silent -- "$FIRST_WHEEL" "$SECOND_WHEEL" \
  || fatal "les deux builds Rust ne produisent pas les memes octets"
FIRST_WHEEL_SHA256=$(sha256sum -- "$FIRST_WHEEL" | awk '{print $1}')
SECOND_WHEEL_SHA256=$(sha256sum -- "$SECOND_WHEEL" | awk '{print $1}')
[[ "$FIRST_WHEEL_SHA256" == "$SECOND_WHEEL_SHA256" ]] \
  || fatal "les deux builds Rust divergent sur le SHA-256"

BUILDER_IMAGE_ID=$FIRST_BUILDER_IMAGE_ID
WHEEL=$FIRST_WHEEL
WHEEL_NAME=$FIRST_WHEEL_NAME

RUST_TREE_SHA256=$(run_git -C "$ROOT" archive --format=tar "$GIT_SHA" rust \
  | sha256sum | awk '{print $1}')
DOCKERFILE_SHA256=$(run_git -C "$ROOT" show "${GIT_SHA}:${DOCKERFILE_REL}" \
  | sha256sum | awk '{print $1}')
WHEEL_SHA256=$FIRST_WHEEL_SHA256
for checksum in "$RUST_TREE_SHA256" "$DOCKERFILE_SHA256" "$WHEEL_SHA256"; do
  [[ "$checksum" =~ ^[0-9a-f]{64}$ ]] || fatal "checksum SHA-256 inattendu"
done

[[ ! -e "$OUTPUT_ROOT" && ! -L "$OUTPUT_ROOT" ]] \
  || fatal "leaf final apparu pendant la construction"
[[ -d "$OUTPUT_PARENT" && ! -L "$OUTPUT_PARENT" \
  && "$(readlink -f -- "$OUTPUT_PARENT")" == "$OUTPUT_PARENT" \
  && "$(stat -c '%d:%i' -- "$OUTPUT_PARENT")" == "$OUTPUT_PARENT_ID" ]] \
  || fatal "parent de sortie remplace pendant la construction"
validate_private_directory_chain \
  "$OUTPUT_PARENT" "$OUTPUT_ANCHOR" "$OUTPUT_ANCHOR_POLICY" \
  || fatal "chaine de parents de sortie modifiee pendant la construction"
PUBLISH_STAGING=$(mktemp -d "$OUTPUT_PARENT/.${OUTPUT_LEAF}.tmp.XXXXXXXX")
[[ "$PUBLISH_STAGING" == "$OUTPUT_PARENT/.${OUTPUT_LEAF}.tmp."* \
  && -d "$PUBLISH_STAGING" && ! -L "$PUBLISH_STAGING" ]] \
  || fatal "staging de publication inattendu"
DESTINATION="$PUBLISH_STAGING/$WHEEL_NAME"
ATTESTATION="${DESTINATION}.attestation"
install -m 0600 -- "$WHEEL" "$DESTINATION"
{
  printf 'format=ava-rust-wheel-attestation-v1\n'
  printf 'attestation_type=unsigned-checksum-manifest\n'
  printf 'signature=none\n'
  printf 'git_sha=%s\n' "$GIT_SHA"
  printf 'rust_tree_sha256=%s\n' "$RUST_TREE_SHA256"
  printf 'wheel_sha256=%s\n' "$WHEEL_SHA256"
  printf 'wheel_filename=%s\n' "$WHEEL_NAME"
  printf 'builder_image_id=%s\n' "$BUILDER_IMAGE_ID"
  printf 'builder_dockerfile_sha256=%s\n' "$DOCKERFILE_SHA256"
  printf 'builder_platform=%s\n' "$TARGET_PLATFORM"
  printf 'builder_python_image=%s\n' "$PYTHON_IMAGE"
  printf 'builder_rust_image=%s\n' "$RUST_IMAGE"
  printf 'python_version=%s\n' "$PYTHON_VERSION"
  printf 'rust_version=%s\n' "$RUST_VERSION"
  printf 'maturin_version=%s\n' "$MATURIN_VERSION"
  printf 'wheel_compatibility=manylinux_2_36_x86_64\n'
} > "$ATTESTATION"
chmod 0600 -- "$ATTESTATION"
atomic_publish_directory "$PUBLISH_STAGING" "$OUTPUT_ROOT" \
  || fatal "publication atomique Rust refusee"
PUBLISH_STAGING=''
DESTINATION="$OUTPUT_ROOT/$WHEEL_NAME"
ATTESTATION="${DESTINATION}.attestation"

printf 'AVA_RUST_WHEEL=%s\n' "$DESTINATION"
printf 'AVA_RUST_ATTESTATION=%s\n' "$ATTESTATION"
