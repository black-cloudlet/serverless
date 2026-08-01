#!/usr/bin/env bash
# Load the mirrored images into the airgapped registry.
#
# Run INSIDE the airgapped network, with the tar produced by pull-images.sh.
# Nothing here reaches the internet.
#
# Images keep their upstream repository path under the target organization, so
# ghcr.io/buildpacks-community/kpack/controller becomes
# <registry>/<org>/buildpacks-community/kpack/controller. Keeping the path makes
# the internal copy traceable to its origin, and matches what the charts expect.
#
# Registry content only. The runtime tarballs from pull-runtimes.sh are artifact
# server content, not registry content, and are published separately - see
# README.md ("Runtime files"). Uploading them is deliberately not this script's
# job: the two halves land in different systems, with different credentials, and
# a partial run of one should not look like a failure of the other.
#
# Usage:
#   ./push-airgapped.sh -r registry.internal [-o org] [-i images.tar.gz] [-n]
#
#   -n  dry run: print what would be pushed and change nothing.

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
. "${here}/common.sh"

REGISTRY=
ORG=
IMAGES_TAR=images.tar.gz
DRY=0

while getopts ':r:o:i:nh' opt; do
    case $opt in
        r) REGISTRY=$OPTARG ;;
        o) ORG=$OPTARG ;;
        i) IMAGES_TAR=$OPTARG ;;
        n) DRY=1 ;;
        h) sed -n '2,22p' "$0"; exit 0 ;;
        *) die "unknown option: -$OPTARG" ;;
    esac
done

[ -n "$REGISTRY" ] || die "-r <registry> is required"
need skopeo tar

run() {
    if [ "$DRY" = 1 ]; then
        printf '  would: %s\n' "$*" >&2
    else
        "$@"
    fi
}

# <registry>/<org>/<upstream path>, dropping the upstream registry host.
target_ref() {
    local ref=$1 path
    path=${ref#*/}                       # strip ghcr.io/ or docker.io/
    [ -n "$ORG" ] && path="${ORG}/${path}"
    printf '%s/%s' "$REGISTRY" "$path"
}

# Pushing images is the whole job now, so a missing tar is a failure, not a
# shrug: skipping it would leave the script reporting success having done nothing.
[ -f "$IMAGES_TAR" ] || die "not found: ${IMAGES_TAR} (produced by pull-images.sh; override with -i)"

step "Images from ${IMAGES_TAR}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar xzf "$IMAGES_TAR" -C "$work"
[ -f "${work}/images.list" ] || die "images.list missing from ${IMAGES_TAR}"

while read -r name ref; do
    [ -n "$name" ] || continue
    dest=$(target_ref "$ref")
    log "${ref}  ->  ${dest}"
    run skopeo copy --all --quiet \
        "oci:${work}/images:${name}" "docker://${dest}"
done < "${work}/images.list"

step "Done"
log "point the chart at the internal copies:"
log "  registry.url=${REGISTRY}${ORG:+  registry.organization=${ORG}}"
log "runtime tarballs are published to the artifact server separately - see README.md"
