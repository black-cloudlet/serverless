#!/usr/bin/env bash
# Load the mirrored artefacts into the airgapped environment.
#
# Run INSIDE the airgapped network, with the tars produced by pull-images.sh and
# pull-runtimes.sh. Nothing here reaches the internet.
#
# Images keep their upstream repository path under the target organization, so
# ghcr.io/buildpacks-community/kpack/controller becomes
# <registry>/<org>/buildpacks-community/kpack/controller. Keeping the path makes
# the internal copy traceable to its origin, and matches what the charts expect.
#
# Runtime files are uploaded preserving their <host>/<path> layout, which is what
# BP_DEPENDENCY_MIRROR resolves against when set with {originalHost}.
#
# Usage:
#   ./push-airgapped.sh -r registry.internal [-o org] [-m https://artifactory/.../mirror]
#                       [-i images.tar.gz] [-t runtime-python.tar.gz,...] [-n]
#
#   -n  dry run: print what would be pushed and change nothing.

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
. "${here}/common.sh"

REGISTRY=
ORG=
MIRROR=
IMAGES_TAR=images.tar.gz
RUNTIME_TARS=
DRY=0

while getopts ':r:o:m:i:t:nh' opt; do
    case $opt in
        r) REGISTRY=$OPTARG ;;
        o) ORG=$OPTARG ;;
        m) MIRROR=$OPTARG ;;
        i) IMAGES_TAR=$OPTARG ;;
        t) RUNTIME_TARS=$OPTARG ;;
        n) DRY=1 ;;
        h) sed -n '2,20p' "$0"; exit 0 ;;
        *) die "unknown option: -$OPTARG" ;;
    esac
done

[ -n "$REGISTRY" ] || die "-r <registry> is required"
need skopeo tar
[ -n "$MIRROR" ] && need curl

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

if [ -f "$IMAGES_TAR" ]; then
    step "Images from ${IMAGES_TAR}"
    work=$(mktemp -d)
    tar xzf "$IMAGES_TAR" -C "$work"
    [ -f "${work}/images.list" ] || die "images.list missing from ${IMAGES_TAR}"

    while read -r name ref; do
        [ -n "$name" ] || continue
        dest=$(target_ref "$ref")
        log "${ref}  ->  ${dest}"
        run skopeo copy --all --quiet \
            "oci:${work}/images:${name}" "docker://${dest}"
    done < "${work}/images.list"
    rm -rf "$work"
else
    log "no ${IMAGES_TAR}; skipping images"
fi

if [ -n "$RUNTIME_TARS" ]; then
    [ -n "$MIRROR" ] || die "-m <mirror base url> is required to upload runtime files"
    step "Runtime files -> ${MIRROR}"
    for tarball in ${RUNTIME_TARS//,/ }; do
        [ -f "$tarball" ] || die "not found: ${tarball}"
        work=$(mktemp -d)
        tar xzf "$tarball" -C "$work"
        # manifest.tsv records what was mirrored; it is not itself an artefact.
        rm -f "${work}/manifest.tsv"
        while IFS= read -r file; do
            rel=${file#"${work}/"}
            log "${rel}"
            run curl -fsS --retry 3 -T "$file" "${MIRROR%/}/${rel}"
        done < <(find "$work" -type f | sort)
        rm -rf "$work"
    done
fi

step "Done"
log "point the chart at the internal copies:"
log "  registry.url=${REGISTRY}${ORG:+  registry.organization=${ORG}}"
[ -n "$MIRROR" ] && log "  BP_DEPENDENCY_MIRROR=${MIRROR%/}/{originalHost}"
