#!/usr/bin/env bash
# Shared helpers for the airgap mirror scripts.
#
# Source this; do not run it.

set -euo pipefail

log()  { printf '  %s\n' "$*" >&2; }
step() { printf '\n== %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

need() {
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not installed"
    done
}

# The newest release tag of a repository, ignoring "latest" and pre-releases.
#
# "latest" is a moving pointer: mirroring it records no version, so the airgapped
# copy cannot be reproduced or compared later. Only X.Y and X.Y.Z tags are
# considered, so -rc, -beta and floating names like "jammy" are skipped.
#
# $1: repository, e.g. docker.io/paketobuildpacks/python
newest_tag() {
    local repo=$1 tag
    tag=$(skopeo list-tags "docker://${repo}" \
        | jq -r '.Tags[]' \
        | grep -E '^v?[0-9]+\.[0-9]+(\.[0-9]+)?$' \
        | sed 's/^v//' \
        | sort -V \
        | tail -1) || true
    [ -n "$tag" ] || die "no release tag found for ${repo}"
    printf '%s' "$tag"
}

# Host-qualify a repository: chart entries are written relative to the registry
# the chart prefixes at deploy time, so an unqualified one is upstream Paketo. A
# reference that already carries a host (a dot or a port in its first segment)
# is left alone.
#
# $1: repository, e.g. paketobuildpacks/cpython
qualify() {
    case ${1%%/*} in
        *.*|*:*|localhost) printf '%s' "$1" ;;
        *) printf '%s/%s' "${PAKETO_HOST:-docker.io}" "$1" ;;
    esac
}

# A pull reference from a "repository<TAB>version" pair, host-qualified and
# joined with "@" for a digest, ":" for a tag.
#
# $1: repository  $2: version (tag or sha256:... digest)
image_ref() {
    case $2 in
        sha256:*) printf '%s@%s' "$(qualify "$1")" "$2" ;;
        *) printf '%s:%s' "$(qualify "$1")" "$2" ;;
    esac
}

# The buildpack-content images the cluster actually references: every
# ClusterStack's build and run image, and every ClusterStore source.
#
# These live in the kpack chart (`clusterBuild.stacks` / `clusterBuild.stores`),
# which owns everything cluster-scoped. Read from those values rather than
# listed here because a store names individual component buildpackages, not the
# language composites - a missing id leaves its Builder permanently not-Ready,
# and mirroring a composite instead would satisfy nothing.
#
# Emits "repository<TAB>version", where an empty version means the values float
# it and the caller should resolve the newest. A digest-pinned entry emits the
# digest as its version, and a source written as a plain reference string is
# split on its last tag separator.
#
# $1: path to the kpack chart values (or the platform overlay setting
#     clusterBuild) that defines the stacks and stores
buildpack_images() {
    python3 -c '
import sys, yaml
cb = (yaml.safe_load(open(sys.argv[1])) or {}).get("clusterBuild") or {}
entries = []
for stack in cb.get("stacks") or []:
    entries += [stack.get("buildImage"), stack.get("runImage")]
for store in cb.get("stores") or []:
    entries += store.get("sources") or []
for e in entries:
    if isinstance(e, str):
        # A verbatim reference. Split off the digest, or the tag - only when the
        # ":" is in the last path segment, so a registry port is not mistaken
        # for one.
        if "@" in e:
            repo, version = e.split("@", 1)
        elif ":" in e.rsplit("/", 1)[-1]:
            repo, version = e.rsplit(":", 1)
        else:
            repo, version = e, ""
        print("\t".join([repo, version]))
    elif e and e.get("repository"):
        print("\t".join([e["repository"], e.get("digest") or e.get("tag") or ""]))
' "$1"
}

# The store repositories whose buildpacks download a runtime, per language, as
# "<repository> <dependency id>". Only buildpacks that *provide* a tool download
# anything; the ones that consume it (pip-install, npm-install, go-build, the
# *-start buildpacks) are pure logic and fetch nothing.
runtime_sources() {
    case $1 in
        python) printf 'cpython python\npip pip\npoetry poetry\nwatchexec watchexec\n' ;;
        node)   printf 'node-engine node\n' ;;
        go)     printf 'go-dist go\n' ;;
        *)      die "unknown language: $1" ;;
    esac
}

# Advertised runtime versions for one language, read from the chart so the
# mirror can never offer a version the API does not, or miss one it does.
#
# $1: runtime name (python|go|node)
# $2: path to the serverless-api values.yaml
advertised_versions() {
    python3 -c '
import sys, yaml
name, path = sys.argv[1], sys.argv[2]
runtimes = yaml.safe_load(open(path)).get("runtimes") or []
for r in runtimes:
    if r.get("name") == name:
        print(" ".join(str(v) for v in r.get("versions") or []))
        break
else:
    sys.exit(f"runtime {name!r} is not in {path}")
' "$1" "$2"
}
