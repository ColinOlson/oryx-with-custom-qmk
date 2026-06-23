#!/usr/bin/env bash
set -euo pipefail

layout_id="6RAGr"
layout_geometry="voyager"
fetch_oryx=1
update_submodule=1
build_docker_image=1
keep_generated=0
docker_image="qmk"

usage() {
  cat <<'USAGE'
Usage: scripts/build-vial-local.sh [options]

Fetch an Oryx layout, adapt it for Vial QMK, and build local Voyager firmware.

Options:
  --layout-id ID          Oryx layout id. Default: 6RAGr
  --geometry GEOMETRY     QMK keyboard geometry. Default: voyager
  --image NAME            Docker image tag. Default: qmk
  --no-fetch              Use the checked-in layout directory without fetching Oryx.
  --no-submodule-update   Use the current qmk_firmware submodule checkout.
  --no-docker-build       Reuse an existing Docker image.
  --keep-generated        Leave generated qmk_firmware/keymaps/vial files in place.
  -h, --help              Show this help.

The script does not commit or push. Review and commit resulting layout or
submodule changes yourself after a successful build.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --layout-id)
      layout_id="${2:?missing value for --layout-id}"
      shift 2
      ;;
    --geometry)
      layout_geometry="${2:?missing value for --geometry}"
      shift 2
      ;;
    --image)
      docker_image="${2:?missing value for --image}"
      shift 2
      ;;
    --no-fetch)
      fetch_oryx=0
      shift
      ;;
    --no-submodule-update)
      update_submodule=0
      shift
      ;;
    --no-docker-build)
      build_docker_image=0
      shift
      ;;
    --keep-generated)
      keep_generated=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command git
require_command python3
require_command docker

if [[ "${fetch_oryx}" -eq 1 ]]; then
  require_command curl
  require_command jq
  require_command unzip
fi

vial_keymap="qmk_firmware/keyboards/zsa/${layout_geometry}/keymaps/vial"
vial_keymap_rel="keyboards/zsa/${layout_geometry}/keymaps/vial"

cleanup_generated_keymap() {
  if [[ "${keep_generated}" -eq 1 ]]; then
    return
  fi

  if git -C qmk_firmware rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C qmk_firmware restore -- "${vial_keymap_rel}" >/dev/null 2>&1 || true
    git -C qmk_firmware clean -fd -- "${vial_keymap_rel}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_generated_keymap EXIT

fetch_latest_oryx_layout() {
  local tmp_dir response hash_id firmware_version change_description query variables
  tmp_dir="$(mktemp -d)"

  query='query getLayout($hashId: String!, $revisionId: String!, $geometry: String) {layout(hashId: $hashId, geometry: $geometry, revisionId: $revisionId) { revision { hashId, qmkVersion, title }}}'
  variables="$(jq -cn \
    --arg hashId "${layout_id}" \
    --arg geometry "${layout_geometry}" \
    '{hashId: $hashId, geometry: $geometry, revisionId: "latest"}')"

  response="$(curl --fail --silent --show-error --location \
    'https://oryx.zsa.io/graphql' \
    --header 'Content-Type: application/json' \
    --data "$(jq -cn --arg query "${query}" --argjson variables "${variables}" '{query: $query, variables: $variables}')")"

  hash_id="$(jq -r '.data.layout.revision.hashId // empty' <<<"${response}")"
  firmware_version="$(jq -r '.data.layout.revision.qmkVersion // empty' <<<"${response}")"
  change_description="$(jq -r '.data.layout.revision.title // empty' <<<"${response}")"

  if [[ -z "${hash_id}" ]]; then
    echo "Could not find latest Oryx revision for ${layout_id}/${layout_geometry}" >&2
    echo "${response}" >&2
    exit 1
  fi

  echo "Oryx revision: ${hash_id}"
  if [[ -n "${firmware_version}" ]]; then
    echo "Oryx QMK firmware version: ${firmware_version}"
  fi
  if [[ -n "${change_description}" ]]; then
    echo "Oryx change description: ${change_description}"
  fi

  curl --fail --location "https://oryx.zsa.io/source/${hash_id}" -o "${tmp_dir}/source.zip"
  mkdir -p "${layout_id}"
  unzip -oqj "${tmp_dir}/source.zip" '*_source/*' -d "${layout_id}"
  rm -rf "${tmp_dir}"
}

update_vial_qmk() {
  git submodule sync qmk_firmware
  git submodule update --init --remote --depth=1 --no-single-branch qmk_firmware
  git -C qmk_firmware checkout -B vial origin/vial
  git -C qmk_firmware submodule update --init --recursive
}

if [[ "${fetch_oryx}" -eq 1 ]]; then
  echo "Fetching latest Oryx layout into ${layout_id}/"
  fetch_latest_oryx_layout
else
  echo "Skipping Oryx fetch; using existing ${layout_id}/"
fi

if [[ ! -f "${layout_id}/vial.json" ]]; then
  echo "Missing ${layout_id}/vial.json. This repo needs the Voyager Vial definition checked into the layout folder." >&2
  exit 1
fi

if [[ "${update_submodule}" -eq 1 ]]; then
  echo "Updating Vial QMK submodule"
  update_vial_qmk
else
  echo "Skipping qmk_firmware submodule update"
fi

if [[ "${build_docker_image}" -eq 1 ]]; then
  echo "Building Docker image ${docker_image}"
  docker build -t "${docker_image}" .
else
  echo "Skipping Docker image build; using ${docker_image}"
fi

echo "Preparing Vial keymap at ${vial_keymap}"
python3 scripts/prepare-vial-keymap.py "${layout_id}" "${vial_keymap}"

echo "Building zsa/${layout_geometry}:vial"
docker run -v ./qmk_firmware:/root --rm "${docker_image}" /bin/sh -c "make zsa/${layout_geometry}:vial"

normalized_layout_geometry="${layout_geometry//\//_}"
built_files=()
while IFS= read -r built_file; do
  built_files+=("${built_file}")
done < <(find ./qmk_firmware -maxdepth 1 -type f \( -name "*${normalized_layout_geometry}*.bin" -o -name "*${normalized_layout_geometry}*.hex" \) | sort)

if [[ "${#built_files[@]}" -eq 0 ]]; then
  echo "Build completed, but no .bin or .hex artifact was found in qmk_firmware/" >&2
  exit 1
fi

echo "Built firmware artifact(s):"
printf '  %s\n' "${built_files[@]}"
