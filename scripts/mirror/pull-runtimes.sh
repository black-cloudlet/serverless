#!/usr/bin/env bash
# Pull the language runtime distributions into one tar per language.
#
# Run on a host WITH internet access. Produces runtime-python.tar.gz,
# runtime-node.tar.gz and runtime-go.tar.gz.
#
# These are the artefacts most airgap setups miss, because they are files rather
# than registry content and nothing fails until the build phase of the first real
# build. A Paketo buildpackage ships buildpack logic only; the interpreter or
# toolchain is fetched at build time from the uri in its buildpack.toml, which
# points at the public internet.
#
# The buildpackage image is therefore the authority for what to download - not a
# list kept here, which would drift the moment a buildpackage is bumped. Each
# image's buildpack.toml files are read and their dependency entries filtered to
# the versions the chart actually advertises.
#
# Files are stored as <host>/<path>, the layout BP_DEPENDENCY_MIRROR expects when
# it is set with the {originalHost} placeholder - the dependencies come from five
# different upstream hosts, so a flat prefix cannot address them.
#
# Usage:
#   ./pull-runtimes.sh [-l python,node,go] [-v values.yaml] [-a amd64] [-A]
#
#   -A  every patch of an advertised minor, instead of only the newest.

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
. "${here}/common.sh"

LANGS=python,node,go
VALUES="${here}/../../charts/serverless-api/values.yaml"
ARCH=amd64
ALL_PATCHES=0

while getopts ':l:v:a:Ah' opt; do
    case $opt in
        l) LANGS=$OPTARG ;;
        v) VALUES=$OPTARG ;;
        a) ARCH=$OPTARG ;;
        A) ALL_PATCHES=1 ;;
        h) sed -n '2,25p' "$0"; exit 0 ;;
        *) die "unknown option: -$OPTARG" ;;
    esac
done

need skopeo jq tar curl python3 sha256sum
[ -f "$VALUES" ] || die "chart values not found: ${VALUES}"

PAKETO_HOST=${PAKETO_HOST:-docker.io}
PAKETO_ORG=${PAKETO_ORG:-paketobuildpacks}

# The dependency id that carries the language runtime itself, so it can be
# filtered against the advertised versions. Every other id is a tool and takes
# its newest entry.
runtime_id() { case $1 in python) echo python ;; node) echo node ;; go) echo go ;; esac; }

# Version pinned in the chart for a store source, or empty to resolve newest.
pinned_version() {
    chart_images "$VALUES" | awk -F'\t' -v r="${PAKETO_ORG}/$1" '$1 == r { print $2; exit }'
}

# Extract every buildpack.toml from a buildpackage image into $2.
extract_tomls() {
    local ref=$1 dest=$2 layout blob n=0
    layout=$(mktemp -d)
    skopeo copy --quiet "docker://${ref}" "oci:${layout}:bp"
    mkdir -p "$dest"
    for blob in "${layout}"/blobs/sha256/*; do
        # Layers are gzipped tars; the manifest and config are plain JSON.
        tar tzf "$blob" >/dev/null 2>&1 || continue
        while IFS= read -r member; do
            tar xzf "$blob" -C "$dest" "$member" 2>/dev/null && n=$((n + 1))
        done < <(tar tzf "$blob" 2>/dev/null | grep -E 'buildpack\.toml$' || true)
    done
    rm -rf "$layout"
    [ "$n" -gt 0 ] || die "no buildpack.toml found in ${ref}"
    log "read ${n} buildpack.toml files"
}

# Select the dependencies to mirror: TSV of uri, checksum, destination path.
#
# $4 empty means the id is not version-selectable (a tool): take its newest.
select_deps() {
    python3 - "$1" "$2" "$3" "$4" "$5" <<'PY'
import pathlib, sys, tomllib
from urllib.parse import urlsplit

root, arch, dep_id, versions, all_patches = sys.argv[1:6]
wanted = [v for v in versions.split() if v]
found = {}

for toml in pathlib.Path(root).rglob("buildpack.toml"):
    try:
        data = tomllib.loads(toml.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        continue
    for dep in (data.get("metadata") or {}).get("dependencies") or []:
        uri = dep.get("uri")
        if not uri or (dep.get("arch") not in (None, arch)):
            continue
        if (dep.get("id") or "").split("/")[-1] != dep_id:
            continue
        version = str(dep.get("version") or "")
        if wanted:
            # "3.11" must match 3.11.9 but never 3.1
            if not any(version == w or version.startswith(w + ".") for w in wanted):
                continue
        checksum = dep.get("checksum") or (
            "sha256:" + dep["sha256"] if dep.get("sha256") else ""
        )
        found[version] = (uri, checksum)

def sortkey(v):
    return [int(p) if p.isdigit() else 0 for p in v.split(".")]

# Newest patch per advertised minor; newest overall when nothing was advertised.
keep = {}
for version, value in found.items():
    if not wanted:
        bucket = dep_id
    elif all_patches == "1":
        bucket = version
    else:
        bucket = next(
            (w for w in wanted if version == w or version.startswith(w + ".")), version
        )
    if bucket not in keep or sortkey(version) > sortkey(keep[bucket][0]):
        keep[bucket] = (version, value)

if not keep:
    sys.exit(f"no dependencies matched id={dep_id!r} versions={wanted} arch={arch!r}")

for _, (version, (uri, checksum)) in sorted(keep.items()):
    parts = urlsplit(uri)
    print("\t".join([uri, checksum, parts.netloc + parts.path, dep_id, version]))
PY
}

for lang in ${LANGS//,/ }; do
    step "${lang}"
    versions=$(advertised_versions "$lang" "$VALUES")
    log "advertised versions: ${versions}"

    work=$(mktemp -d)
    out="${work}/files"
    deps=

    # One buildpackage per download, taken from the ClusterStore's own sources -
    # the composites (paketobuildpacks/python and friends) are not what the store
    # references, so reading them would describe a mirror we do not build.
    while read -r repository dep_id; do
        [ -n "$repository" ] || continue
        image="${PAKETO_HOST}/${PAKETO_ORG}/${repository}"
        tag=$(pinned_version "$repository")
        [ -n "$tag" ] || tag=$(newest_tag "$image")
        log "${repository}:${tag}"

        rm -rf "${work}/toml"
        extract_tomls "${image}:${tag}" "${work}/toml"

        if [ "$dep_id" = "$(runtime_id "$lang")" ]; then
            found=$(select_deps "${work}/toml" "$ARCH" "$dep_id" "$versions" "$ALL_PATCHES")
        else
            # A tool is not version-selectable by the caller; take its newest.
            found=$(select_deps "${work}/toml" "$ARCH" "$dep_id" "" 0)
        fi
        deps=${deps:+${deps}$'\n'}${found}
    done < <(runtime_sources "$lang")

    while IFS=$'\t' read -r uri checksum path dep_id version; do
        [ -n "$uri" ] || continue
        log "${dep_id} ${version}"
        mkdir -p "${out}/$(dirname "$path")"
        curl -fsSL --retry 3 -o "${out}/${path}" "$uri"
        if [ -n "$checksum" ]; then
            actual="sha256:$(sha256sum "${out}/${path}" | cut -d' ' -f1)"
            [ "$actual" = "$checksum" ] \
                || die "checksum mismatch for ${uri}: got ${actual}, want ${checksum}"
        else
            log "  (no checksum published; not verified)"
        fi
    done <<< "$deps"

    printf '%s\n' "$deps" > "${out}/manifest.tsv"
    tar czf "runtime-${lang}.tar.gz" -C "$out" .
    log "runtime-${lang}.tar.gz  $(du -h "runtime-${lang}.tar.gz" | cut -f1)"
    rm -rf "$work"
done
