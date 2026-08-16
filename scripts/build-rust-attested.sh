#!/usr/bin/env bash
# Construit dans un conteneur epingle la wheel Rust d'un commit Ava propre.
#
# L'attestation produite est volontairement un manifeste de checksums NON SIGNE.
# Elle prouve la coherence locale artefact/source/builder, pas l'identite d'un
# auteur ni l'integrite face a un operateur capable de reecrire ces trois objets.
set -Eeuo pipefail
umask 077

ROOT=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DOCKER_BIN="${AVA_DOCKER_BIN:-docker}"
DOCKERFILE_REL='deploy/docker/Dockerfile.rust-builder'
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

command -v "$DOCKER_BIN" >/dev/null 2>&1 \
  || fatal "docker introuvable ou non executable"
for prerequisite in git sha256sum tar awk find install; do
  command -v "$prerequisite" >/dev/null 2>&1 \
    || fatal "$prerequisite introuvable"
done

git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fatal "le script doit etre execute depuis un checkout Git Ava"
if ! git -C "$ROOT" diff --quiet --ignore-submodules -- \
  || ! git -C "$ROOT" diff --cached --quiet --ignore-submodules -- \
  || [[ -n "$(git -C "$ROOT" ls-files --others --exclude-standard)" ]]; then
  fatal "le checkout Ava doit etre integralement propre avant la construction"
fi

GIT_SHA=$(git -C "$ROOT" rev-parse --verify HEAD)
[[ "$GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "SHA Git inattendu"
git -C "$ROOT" cat-file -e "${GIT_SHA}:rust" \
  || fatal "le commit ne contient pas le sous-arbre rust"
git -C "$ROOT" cat-file -e "${GIT_SHA}:${DOCKERFILE_REL}" \
  || fatal "le commit ne contient pas le Dockerfile du builder"

OUTPUT_ROOT="${AVA_RUST_ARTIFACT_DIR:-$ROOT/dist/ava-rust/$GIT_SHA}"
[[ "$OUTPUT_ROOT" == /* && "$OUTPUT_ROOT" != / && "$OUTPUT_ROOT" != /home ]] \
  || fatal "repertoire de sortie absolu et borne requis"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/ava-rust-build.XXXXXX")
CONTAINER_ID=''
IMAGE_REF=''
cleanup() {
  if [[ -n "$CONTAINER_ID" ]]; then
    "$DOCKER_BIN" rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$IMAGE_REF" ]]; then
    "$DOCKER_BIN" image rm -f "$IMAGE_REF" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$TMP"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$TMP/context" "$TMP/export"
# Le contexte ne provient jamais du working tree : il est reconstruit depuis le
# commit et borne au Dockerfile ainsi qu'au sous-arbre Rust.
git -C "$ROOT" archive --format=tar "$GIT_SHA" "$DOCKERFILE_REL" rust \
  | tar -xf - -C "$TMP/context"

# Le suffixe processus evite qu'un cleanup concurrent retire l'image d'un autre
# build du meme SHA sur un runner partage. Le manifeste atteste l'ID immuable,
# jamais ce tag jetable.
IMAGE_REF="ava-rust-builder:${GIT_SHA}-$$"
"$DOCKER_BIN" build \
  --pull \
  --platform "$TARGET_PLATFORM" \
  --file "$TMP/context/$DOCKERFILE_REL" \
  --build-arg "AVA_GIT_SHA=$GIT_SHA" \
  --tag "$IMAGE_REF" \
  "$TMP/context"

BUILDER_IMAGE_ID=$("$DOCKER_BIN" image inspect --format '{{.Id}}' "$IMAGE_REF")
[[ "$BUILDER_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || fatal "identifiant immuable du builder inattendu"
BUILDER_REVISION=$("$DOCKER_BIN" image inspect \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE_REF")
[[ "$BUILDER_REVISION" == "$GIT_SHA" ]] \
  || fatal "le builder ne porte pas le commit attendu"
BUILDER_OS=$("$DOCKER_BIN" image inspect --format '{{.Os}}' "$IMAGE_REF")
BUILDER_ARCH=$("$DOCKER_BIN" image inspect --format '{{.Architecture}}' "$IMAGE_REF")
[[ "$BUILDER_OS/$BUILDER_ARCH" == "$TARGET_PLATFORM" ]] \
  || fatal "plateforme builder inattendue: $BUILDER_OS/$BUILDER_ARCH"

CONTAINER_ID=$("$DOCKER_BIN" create --platform "$TARGET_PLATFORM" "$IMAGE_REF")
[[ "$CONTAINER_ID" =~ ^[0-9a-f]{12,64}$ ]] || fatal "identifiant de conteneur inattendu"
"$DOCKER_BIN" cp "$CONTAINER_ID:/artifacts/." "$TMP/export/"

mapfile -t WHEELS < <(find "$TMP/export" -maxdepth 1 -type f -name '*.whl' -print)
[[ ${#WHEELS[@]} -eq 1 ]] || fatal "le builder doit produire exactement une wheel"
WHEEL=${WHEELS[0]}
WHEEL_NAME=$(basename -- "$WHEEL")
[[ "$WHEEL_NAME" =~ ^[A-Za-z0-9._-]+-cp312-cp312-manylinux_2_36_x86_64\.whl$ ]] \
  || fatal "nom ou compatibilite de wheel inattendu: $WHEEL_NAME"

RUST_TREE_SHA256=$(git -C "$ROOT" archive --format=tar "$GIT_SHA" rust \
  | sha256sum | awk '{print $1}')
DOCKERFILE_SHA256=$(git -C "$ROOT" show "${GIT_SHA}:${DOCKERFILE_REL}" \
  | sha256sum | awk '{print $1}')
WHEEL_SHA256=$(sha256sum -- "$WHEEL" | awk '{print $1}')
for checksum in "$RUST_TREE_SHA256" "$DOCKERFILE_SHA256" "$WHEEL_SHA256"; do
  [[ "$checksum" =~ ^[0-9a-f]{64}$ ]] || fatal "checksum SHA-256 inattendu"
done

mkdir -p -- "$OUTPUT_ROOT"
chmod 0700 -- "$OUTPUT_ROOT"
DESTINATION="$OUTPUT_ROOT/$WHEEL_NAME"
ATTESTATION="${DESTINATION}.attestation"
[[ ! -e "$DESTINATION" && ! -e "$ATTESTATION" ]] \
  || fatal "artefact deja present; aucune reecriture implicite"

STAGED_WHEEL="$OUTPUT_ROOT/.${WHEEL_NAME}.tmp.$$"
STAGED_ATTESTATION="${STAGED_WHEEL}.attestation"
install -m 0600 -- "$WHEEL" "$STAGED_WHEEL"
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
} > "$STAGED_ATTESTATION"
chmod 0600 -- "$STAGED_ATTESTATION"
mv -- "$STAGED_ATTESTATION" "$ATTESTATION"
mv -- "$STAGED_WHEEL" "$DESTINATION"

printf 'AVA_RUST_WHEEL=%s\n' "$DESTINATION"
printf 'AVA_RUST_ATTESTATION=%s\n' "$ATTESTATION"
