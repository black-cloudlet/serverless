# Airgap mirror scripts

Three classes of artefact have to cross the airgap, and the third is the one
most setups miss because it is not registry content:

| Script | Produces | Contents |
|--------|----------|----------|
| `pull-images.sh` | `images.tar.gz` | kpack platform images + the Paketo stack and buildpackages |
| `pull-runtimes.sh` | `runtime-python.tar.gz`, `runtime-node.tar.gz`, `runtime-go.tar.gz` | the interpreter/toolchain tarballs the buildpacks download at build time |
| `push-airgapped.sh` | - | loads the above into the internal registry and artifact server |

Run the first two on a connected host, carry the tars in, run the third inside.

## Connected side

```bash
./pull-images.sh                       # newest release tag of each image
./pull-runtimes.sh                     # versions come from the chart
```

`pull-runtimes.sh` reads `runtimes[].versions` from
`charts/serverless-api/values.yaml`, so the mirror cannot offer a version the API
does not advertise, or miss one it does. Add a version there and re-run.

By default it keeps the newest patch of each advertised minor (`3.12` ->
`3.12.4`); `-A` keeps every patch.

## Airgapped side

```bash
./push-airgapped.sh -r registry.internal -o acme \
    -m https://artifactory.internal/artifactory/paketo \
    -t runtime-python.tar.gz,runtime-node.tar.gz,runtime-go.tar.gz
```

`-n` prints what would happen and changes nothing.

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

## Two things worth knowing

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
