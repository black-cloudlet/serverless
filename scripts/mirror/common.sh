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

# The Paketo images the chart actually references: the stack's build and run
# images, and every ClusterStore source.
#
# Read from the chart rather than listed here because the ClusterStore names
# individual component buildpackages, not the language composites - a missing id
# leaves its Builder permanently not-Ready, and mirroring a composite instead
# would satisfy nothing.
#
# Emits "repository<TAB>version", where an empty version means the chart floats
# it and the caller should resolve the newest.
#
# $1: path to the serverless-api values.yaml
chart_images() {
    python3 -c '
import sys, yaml
build = (yaml.safe_load(open(sys.argv[1])) or {}).get("build") or {}
stack = build.get("stack") or {}
entries = [stack.get("buildImage"), stack.get("runImage")]
entries += (build.get("store") or {}).get("sources") or []
for e in entries:
    if e and e.get("repository"):
        print("\t".join([e["repository"], e.get("version") or ""]))
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
