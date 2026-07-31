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

PAKETO_REGISTRY=${PAKETO_REGISTRY:-docker.io/paketobuildpacks}

# language -> buildpackage image, the dependency id carrying the runtime, and the
# tool ids that ship alongside it (always newest, they are not version-selectable).
image_for()   { case $1 in python) echo python ;; node) echo nodejs ;; go) echo go ;; esac; }
runtime_id()  { case $1 in python) echo python ;; node) echo node   ;; go) echo go ;; esac; }
tool_ids()    { case $1 in python) echo "pip poetry watchexec" ;; *) echo "" ;; esac; }

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
select_deps() {
    python3 - "$1" "$2" "$3" "$4" "$5" <<'PY'
import pathlib, sys, tomllib
from urllib.parse import urlsplit

root, arch, runtime_id, versions, all_patches = sys.argv[1:6]
wanted = [v for v in versions.split() if v]
tools = {t for t in (sys.argv[6].split() if len(sys.argv) > 6 else [])}
found = {}

for toml in pathlib.Path(root).rglob("buildpack.toml"):
    try:
        data = tomllib.loads(toml.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        continue
    for dep in (data.get("metadata") or {}).get("dependencies") or []:
        uri = dep.get("uri")
        if not uri:
            continue
        if dep.get("arch") not in (None, arch):
            continue
        dep_id = (dep.get("id") or "").split("/")[-1]
        if dep_id != runtime_id and dep_id not in tools:
            continue
        version = str(dep.get("version") or "")
        if dep_id == runtime_id and wanted:
            # "3.11" must match 3.11.9 but never 3.1
            if not any(version == w or version.startswith(w + ".") for w in wanted):
                continue
        checksum = dep.get("checksum") or (
            "sha256:" + dep["sha256"] if dep.get("sha256") else ""
        )
        found.setdefault((dep_id, version), (uri, checksum))

def sortkey(v):
    return [int(p) if p.isdigit() else 0 for p in v.split(".")]

# Newest patch per advertised minor, unless every patch was asked for.
keep = {}
for (dep_id, version), value in found.items():
    if dep_id == runtime_id and wanted and all_patches != "1":
        bucket = next(
            (w for w in wanted if version == w or version.startswith(w + ".")), version
        )
    elif dep_id in tools:
        bucket = dep_id
    else:
        bucket = version
    key = (dep_id, bucket)
    if key not in keep or sortkey(version) > sortkey(keep[key][0]):
        keep[key] = (version, value)

if not keep:
    sys.exit(f"no dependencies matched id={runtime_id!r} versions={wanted} arch={arch!r}")

for (dep_id, _), (version, (uri, checksum)) in sorted(keep.items()):
    parts = urlsplit(uri)
    print("\t".join([uri, checksum, parts.netloc + parts.path, dep_id, version]))
PY
}

for lang in ${LANGS//,/ }; do
    step "${lang}"
    image="${PAKETO_REGISTRY}/$(image_for "$lang")"
    tag=$(newest_tag "$image")
    log "buildpackage ${image}:${tag}"

    work=$(mktemp -d)
    extract_tomls "${image}:${tag}" "${work}/toml"

    versions=$(advertised_versions "$lang" "$VALUES")
    log "advertised versions: ${versions}"

    deps=$(select_deps "${work}/toml" "$ARCH" "$(runtime_id "$lang")" \
        "$versions" "$ALL_PATCHES" "$(tool_ids "$lang")")

    out="${work}/files"
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
