# Airgap mirror scripts

Three classes of artefact have to cross the airgap, and the third is the one
most setups miss because it is not registry content:

| Script | Produces | Contents |
|--------|----------|----------|
| `pull-images.sh` | `images.tar.gz` | kpack platform images + the stack and every ClusterStore buildpackage |
| `pull-runtimes.sh` | `runtime-python.tar.gz`, `runtime-node.tar.gz`, `runtime-go.tar.gz` | the interpreter/toolchain tarballs the buildpacks download at build time |
| `push-airgapped.sh` | - | loads `images.tar.gz` into the internal **registry** |

Run the first two on a connected host, carry the tars in, run the third inside.

## Connected side

```bash
./pull-images.sh                       # newest release tag of each image
./pull-runtimes.sh                     # versions come from the chart
```

Both read the chart, so neither can drift from what is deployed.
`pull-images.sh` takes the stack images and the ClusterStore sources from
`build.stack` and `build.store.sources`; `pull-runtimes.sh` takes the versions
from `runtimes[].versions`. Add a buildpackage or a version there and re-run.

The store names **individual component buildpackages** (`cpython`, `pip`,
`node-engine`, `go-dist`, ...), not the language composites
(`paketobuildpacks/python`). Mirroring a composite instead satisfies no id the
builder orders reference, and every `Builder` stays permanently not-Ready - so
the list has to come from the chart rather than be written out here.

By default it keeps the newest patch of each advertised minor (`3.12` ->
`3.12.4`); `-A` keeps every patch.

## Airgapped side

### Images -> the registry

```bash
./push-airgapped.sh -r registry.internal -o acme
```

`-n` prints what would happen and changes nothing.

### Runtime files -> the artifact server

`push-airgapped.sh` does **not** upload these. They are artifact server content,
not registry content: a different system, different credentials, and a partial
upload of one should not read as a failure of the other. Publish them with
whatever already owns that repository - most Artifactory/Nexus setups have an
ingest path, and a generic repository accepts a plain `PUT` per file:

```bash
tar xzf runtime-python.tar.gz -C stage/
rm -f stage/manifest.tsv          # a record of what was mirrored, not an artefact
cd stage && find . -type f -printf '%P\n' | while IFS= read -r f; do
    curl -fsS --retry 3 -T "$f" "https://artifactory.internal/artifactory/paketo/$f"
done
```

The `<host>/<path>` layout inside the tar has to be preserved on upload - that
is what `{originalHost}` resolves against (see below). `manifest.tsv` records
what was mirrored and is not itself an artefact.

Then point the chart at the internal copies:

```yaml
registry:
  url: registry.internal
  organization: acme
```

and set the dependency mirror on each runtime's `buildEnv`:

```yaml
- name: BP_DEPENDENCY_MIRROR
  value: https://artifactory.internal/artifactory/paketo/{originalHost}
```

`{originalHost}` is required, not cosmetic: the dependencies come from five
different upstream hosts (`www.python.org`, `nodejs.org`, `go.dev`,
`files.pythonhosted.org`, `artifacts.paketo.io`), so a flat prefix cannot address
them. The tars store files as `<host>/<path>` to match.

## Three things worth knowing

**Unpinned sources are reported.** The chart ships `version: ""` on the stack
and store sources, which floats them. Airgapped that is a trap: a floating tag
can later resolve to a buildpackage whose dependencies were never mirrored.
`pull-images.sh` prints the versions it resolved, ready to paste back into
`values.yaml` as pins.

**Tags are resolved, never floating.** Every image is mirrored at its newest
release tag; `latest` and floating pointers like `2` or `2.56` are skipped in
favour of the concrete `2.56.0`. A floating tag records no version, so the
airgapped copy could not be reproduced or compared later. `images.list` inside
the tar records exactly what was taken.

**The kpack images are versioned as a set.** `build-init`, `completion`,
`build-waiter` and `rebase` run alongside the controller and must all be the same
version, so the version is resolved once from the controller and applied to the
whole set. Mixing them yields a cluster that installs cleanly and fails at the
first build. Use `-k` to pin.

## Requirements

`skopeo`, `jq`, `curl`, `tar`, `python3` (3.11+, for `tomllib`) with `pyyaml`,
and `sha256sum`. Every downloaded runtime file is checked against the checksum in
the buildpack's own `buildpack.toml`; a mismatch aborts.
