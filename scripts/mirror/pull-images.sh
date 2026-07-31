#!/usr/bin/env bash
# Pull every container image the build pipeline needs into one portable tar.
#
# Run on a host WITH internet access. Produces images.tar.gz, which
# push-airgapped.sh loads into the internal registry.
#
# Images land in a single OCI layout so one tar carries them all, plus
# images.list mapping each entry back to the reference it came from - that file
# is what the push side replays, and what records which versions were mirrored.
#
# Tags are resolved to the newest release tag, never "latest" (see newest_tag).
# The kpack images are the exception: build-init, completion and the rest run
# alongside the controller and must all be the SAME version, so the version is
# resolved once from the controller and applied to the whole set. Mixing them
# produces a cluster that installs cleanly and fails at the first build.
#
# Usage:
#   ./pull-images.sh [-o out.tar.gz] [-k kpack-version] [-w workdir]
#
#   -k  pin the kpack version instead of resolving the newest (match the
#       kpack chart's appVersion when it pins one).

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
. "${here}/common.sh"

OUT=images.tar.gz
KPACK_VERSION=
WORKDIR=

while getopts ':o:k:w:h' opt; do
    case $opt in
        o) OUT=$OPTARG ;;
        k) KPACK_VERSION=$OPTARG ;;
        w) WORKDIR=$OPTARG ;;
        h) sed -n '2,20p' "$0"; exit 0 ;;
        *) die "unknown option: -$OPTARG" ;;
    esac
done

need skopeo jq tar

KPACK_REGISTRY=${KPACK_REGISTRY:-ghcr.io/buildpacks-community/kpack}
PAKETO_REGISTRY=${PAKETO_REGISTRY:-docker.io/paketobuildpacks}

# Every image kpack itself runs (docs/BUILDING.md - Airgapped Mirror Inventory).
KPACK_IMAGES=(controller webhook build-init build-waiter rebase completion lifecycle)
# The stack and the buildpackages the ClusterStore serves.
PAKETO_IMAGES=(build-jammy-base run-jammy-base go nodejs python)

WORKDIR=${WORKDIR:-$(mktemp -d)}
LAYOUT="${WORKDIR}/images"
mkdir -p "$LAYOUT"
LIST="${WORKDIR}/images.list"
: > "$LIST"

# $1: full source reference (repo:tag)
copy_in() {
    local ref=$1 name
    # One OCI tag per image: ':' and '/' are not allowed in it.
    name=$(printf '%s' "$ref" | tr '/:' '__')
    log "$ref"
    # --all takes every architecture in the manifest list; a single-arch copy
    # silently produces an image the cluster's nodes may not be able to run.
    skopeo copy --all --quiet "docker://${ref}" "oci:${LAYOUT}:${name}"
    printf '%s %s\n' "$name" "$ref" >> "$LIST"
}

step "Resolving kpack version"
if [ -z "$KPACK_VERSION" ]; then
    KPACK_VERSION=$(newest_tag "${KPACK_REGISTRY}/controller")
fi
log "kpack ${KPACK_VERSION} (applied to all kpack images)"

step "kpack platform images"
for image in "${KPACK_IMAGES[@]}"; do
    copy_in "${KPACK_REGISTRY}/${image}:${KPACK_VERSION}"
done

step "Paketo stack and buildpackages"
for image in "${PAKETO_IMAGES[@]}"; do
    copy_in "${PAKETO_REGISTRY}/${image}:$(newest_tag "${PAKETO_REGISTRY}/${image}")"
done

step "Packing ${OUT}"
tar czf "$OUT" -C "$WORKDIR" images images.list
log "$(wc -l < "$LIST") images, $(du -h "$OUT" | cut -f1)"
log "mirrored versions:"
sed 's/^/    /' "$LIST" >&2
