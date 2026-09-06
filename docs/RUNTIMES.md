# Runtimes - versions, dependencies and the registry layout

Reference material for builds: the three version axes, the runtimes ConfigMap that carries
them, how the registries are laid out, and everything an airgapped install must mirror. How
a build actually runs is in BUILDING.md.

## Contents

- [Runtime Versions & Dependencies](#runtime-versions--dependencies)
- [Registry layout](#registry-layout)
- [Airgapped Mirror Inventory](#airgapped-mirror-inventory)
- [Why pip needs three variables of its own](#why-pip-needs-three-variables-of-its-own)

## Runtime Versions & Dependencies

Three independent axes. Conflating them is the most common source of confusion.

| Axis | What it pins | Where it is set |
|------|--------------|-----------------|
| 1. Buildpack content | The mirrored Paketo image tags | kpack chart: `clusterBuild.stacks[].{build,run}Image.tag`, `clusterBuild.stores[].sources[].tag` |
| 2. Language runtime | CPython / Node / Go version | `BP_*_VERSION` build env |
| 3. App dependencies | pip / npm / go modules | package-manager env pointing at the on-prem artifact server |

**Axis 2 is bounded by axis 1.** A pinned buildpackage contains only certain interpreter
versions, and airgapped there is no fallback download. Whenever a
`clusterBuild.stores[].sources[].tag` is bumped, re-check that every advertised
`runtimes[].versions` entry is still available. Otherwise builds fail at `detect` or
`build`.

### Axis 2 - runtime version

| Runtime | Env var |
|---------|---------|
| python | `BP_CPYTHON_VERSION` |
| go | `BP_GO_VERSION` |
| node | `BP_NODE_VERSION` |

> Selecting a version only *asks* for it. The buildpack still has to fetch that runtime, so
> offline this axis works only once the download is redirected to the mirror
> (RUNTIMES.md: Airgapped Mirror Inventory).

### Axis 3 - application packages (airgapped)

The package managers run **inside the build pod** and cannot reach the internet. They are
pointed at the on-prem artifact server:

| Runtime | Env |
|---------|-----|
| python | `PIP_INDEX_URL` (+ `PIP_EXTRA_INDEX_URL`) |
| node | `npm_config_registry` |
| go | `GOPROXY`, `GOSUMDB=off` (or vendored deps with `GOFLAGS=-mod=vendor`) |

> Never disable TLS verification. No `PIP_TRUSTED_HOST`, no `npm strict-ssl=false`, no
> `GOINSECURE`, no `NODE_TLS_REJECT_UNAUTHORIZED=0`. Trust comes from the injected CA
> (BUILDING.md: Trust: CA Injection).

### Where it lives

All of this is **data**, carried by the `runtimes` list in `values.yaml`. It serialises into
the runtimes ConfigMap through `toYaml` and is read by `api/services/builder/runtimes.py`.
`RuntimeSpec` is `extra="allow"`, so these fields flow end to end with no template or model
change:

```yaml
runtimes:
  - name: python
    builder: python
    versionEnv: BP_CPYTHON_VERSION
    defaultVersion: "3.12"
    versions: ["3.11", "3.12", "3.13"]
    buildEnv:
      - { name: PIP_INDEX_URL, value: "https://artifactory.internal/artifactory/api/pypi/pypi/simple" }
  - name: node
    builder: node
    versionEnv: BP_NODE_VERSION
    defaultVersion: "20"
    versions: ["18", "20", "22"]
    buildEnv:
      - { name: npm_config_registry, value: "https://artifactory.internal/artifactory/api/npm/npm/" }
```

The rules that govern it:

- **The runtimes file is the contract.** `RuntimeSpec` declares every key the builder reads
  - `name`, `builder`, `versionEnv`, `defaultVersion`, `versions`, `buildEnv` - and keeps
  unknown keys, so a newer chart can be rolled out ahead of the API.
- **Numbers are coerced to strings.** An unquoted `defaultVersion: 3.12` is a YAML float,
  and no runtime should be lost over a missing pair of quotes.
- **The file is required and has no fallback.** `load_runtimes` raises, the lifespan loads
  it before serving, and a misconfigured pod never reaches readiness. A built-in default
  list would name no ClusterBuilder while looking like a working platform.
- **A runtime that maps to no ClusterBuilder is rejected up front**, as
  `400 runtime 'python' is not buildable` before the 202, not minutes later as a failed
  background deploy.
- **The loader carries no HTTP layer.** `api/services/builder/runtimes.py` only loads and
  validates; the cached, request-injectable registry is assembled in `api/dependencies.py`.
  Any service with no HTTP surface - the build controller, for one - reuses the same
  loading and validation.

## Registry layout

**There is a registry per region, and one shared registry beside them.** The split is
decided by *who writes*:

| Content | Registry | Written by |
|---|---|---|
| kpack's own images; the Paketo stack and buildpackages | **the kpack registry**, shared | the mirror scripts, once |
| Composed `ClusterBuilder` images | **the region's own** | that region's kpack |
| Function images, and their build caches | **the region's own** | that region's kpack |

Everything read-only is shared, so the mirror inventory stays a single copy
(RUNTIMES.md: Airgapped Mirror Inventory). Everything written is local, so two clusters can
never race to push one tag - which is what lets every region build the function it runs
(BUILDING.md: Active/Active Behaviour). The composed `ClusterBuilder` sits on the local side
even though it is *made* of mirrored content: composing it is a push.

**One region per registry is a hard requirement.** The chart requires `regions[].registry.url`
on every region and refuses to render two regions onto one registry; the build controller
re-checks it before pruning tags (BUILD-CONTROLLER.md: Registry tag GC).

Registries that namespace their repositories - Harbor projects, Quay and GitLab
organizations, Artifactory repository keys - need a path segment between the host and the
repository. `registry.organization` supplies it, `build.builderRepository` adds the one
everything the platform builds sits under, and the rest derives from those three:

```
{region registry url}/{registry.organization}/{build.builderRepository}/...   <- the "registry base"

  base/{name}                                       ClusterBuilder tags
  base/{group}/{name}:{revision}                      function images (the API)
  base/{group}/{name}_cache:latest                  build layer cache (RUNTIMES.md: Build cache)
```

- **Only the host varies per region.** `regions[].registry` normally sets `url` alone.
  Naming the same repositories differently per region would give `RegistryConfig.path` two
  answers and buy nothing.
- **One value covers the ClusterBuilders and the functions.** They are pushed by the same
  credential, mirrored together and cleaned up against the same root. A function cannot
  collide with a ClusterBuilder: a ClusterBuilder is one path component below the base, a
  function is two.
- **Either segment may be empty** and is then skipped, so the flat `{host}/{group}/{name}`
  layout produces no doubled slash.
- **`RegistryConfig.path` is the single derivation.** The image reference hangs off it, and
  the repository *delete* addresses Quay by the same string with the host removed, so what
  cleanup deletes is what the build pushed to.
- **`CommonSettings.registry_for(region)` is the single resolution**, merging a region's
  override over the platform default; every cluster client carries the answer as
  `Cluster.registry`. Nothing on a per-region path reads the platform default directly. A
  region that names no registry of its own inherits the default, which is exactly the
  single-registry install.

Two placement rules follow from per-region registries:

- **The KSVC image is composed inside the per-cluster fan-out.** A function's image is
  `{region registry base}/{group}/{name}:{revision}` - a different string per region. That is
  the create path only; afterwards the field belongs to each region's build controller
  (BUILD-CONTROLLER.md: Who writes the ksvc image). A container is unaffected: its image is
  the caller's, one value everywhere.
- **A region's registry lives in the shared `regions[]` list, never in per-release values.**
  The API instance handling a write composes manifests for *every* region, so it must know
  every region's registry. `regions[]` is the same ConfigMap in every cluster, which is what
  keeps two instances composing the same `Image` for region X (BUILDING.md: Convergence
  rules). It is ConfigMap data, so a region override carries `url` / `organization` /
  `repository` only - no secrets.

**No NetworkPolicy follows from any of this.** A registry is never a Service or a Route, so
the `allow-egress-external` rule already covers each region's registry and the shared kpack
registry.

Mirrored stack and store images are not something this platform builds, so they sit under
the organization but **not** under `build.builderRepository`. The kpack chart prefixes them
with its own `clusterBuild.registry` - set that to `{registry.url}/{registry.organization}`:

```
{clusterBuild.registry}/...                         <- the kpack chart's prefix

  base/paketobuildpacks/build-jammy-base:<tag>      ClusterStack
  base/paketobuildpacks/<component>:<tag>           ClusterStore sources
```

One deliberate exception: the pull/push Secret's `auths` key stays `registry.url` with **no**
organization. Docker credentials are keyed by registry *host*; adding the path there
produces a secret that silently never matches, and it surfaces much later as an
unauthenticated pull.

The chart and the API must agree on the derivation, so it is implemented twice - the
`serverless-api.registryBase` template helper and `RegistryConfig.base` in
`common/config.py`. The Deployment passes both halves as `SERVERLESS_REGISTRY__URL` and
`SERVERLESS_REGISTRY__ORGANIZATION`. Changing one implementation without the other pushes
builder images and function images to different places.

### Build cache

kpack can cache build layers - the restore and export ends of the lifecycle - in one of two
places, per `Image`:

| Form | `spec.cache` | Cost |
|------|--------------|------|
| Volume | `volume.size: 2Gi` | a **PVC per function**, provisioned in full whether or not a build fills it |
| Registry | `registry.tag: <ref>` | blobs in the registry the build already pushes to |

**The API writes the registry form.** The volume form's cost scales with the number of
functions, so a few hundred functions carry a few hundred idle PVCs, and on a
`ReadWriteOnce` StorageClass each one also pins its build to the node holding it. The
registry is storage the platform already runs, and the build `ServiceAccount` already carries
a push credential for it (BUILDING.md: Registry & Git Credentials).

The cache is a sibling repository of the function image:

```
base/{group}/{name}:{revision}                      function images
base/{group}/{name}_cache:latest                  that function's layer cache
```

The `_` is load-bearing. A name is a DNS-1123 label, which admits only `[a-z0-9-]`, so no
function can ever be named `{name}_cache` and the two repositories can never be the same
one. The suffix also needs no `FEATURE_EXTENDED_REPOSITORY_NAMES`, and keeps the cache in
whatever namespace the function image already lives in.

The cache is per `Image` - per function, not per revision. There is one `Image` per
function and its `spec.tag` follows the revision, so keying the cache by revision would
strand the old cache and start cold on every revision change.

`build.cache: inherit` writes **no** `spec.cache` at all. That is the escape hatch for an
install that wants kpack's own behaviour. It is not a way to disable caching: a stock kpack
defaults an `Image` with no cache spec to a volume cache.

### Moving a function's repository

`spec.tag` is **immutable on a kpack `Image`**. `validateTag` compares against the baseline
on every update and rejects a change at admission:

```go
if apis.IsInUpdate(ctx) {
    original := apis.GetBaseline(ctx).(*Image)
    return validate.ImmutableField(original.Spec.Tag, is.Tag, "tag")
}
```

A moved tag therefore cannot be applied over. Left as an ordinary apply it wedges the
function: every later write emits the `Image` manifest, so a `PUT` that has nothing to do
with the registry is rejected too, until someone deletes the object by hand.

The API instead **deletes the `Image` and lets the apply recreate it** whenever the computed
tag differs from the deployed one (`WorkloadService.retag_build`, one GET on the build
region per write). Three things follow:

- **The new `Image` has no prior `Build`, so it builds immediately.** Changing the layout and
  sending any `PUT` is the whole migration.
- **The old repository and its cache are reclaimed - but only when the repository is what
  moved.** Quay deletes a repository whole, so the reclaim runs on the *repository* half of
  the previous tag and only when the new tag names a different one, through the same Quay API
  the delete path uses (BUILDING.md: Registry cleanup on delete). Without it a layout change
  would leak a repository pair permanently. It is skipped when the old reference is on a
  different **host**: this token addresses one registry, and a same-named path elsewhere is
  somebody else's repository.
- **Build history resets.** `Build`s are owned by the `Image`, so deleting it collects them.

The workload keeps serving its existing digest throughout: the reclaim runs against the
*previous* repository, which the current digest has already been pushed out of, and the
running pods hold their image regardless.

### A tag that moved inside one repository

Changing a function's **`revision`** moves the tag too - the tag is the revision projected
onto one repository - so an ordinary `PUT` that switches branches takes the delete-and-
recreate path above. It reclaims **nothing**, and must not: that one repository still holds
the digest the `KSVC` is serving (which a scale-from-zero has to pull again) and is where the
next build pushes. What it leaves behind is one abandoned revision tag, which the build
controller's tag GC collects on its own schedule - it protects the current revision tag and
the serving digest, and nothing else (BUILD-CONTROLLER.md: Registry tag GC).

## Airgapped Mirror Inventory

Three **distinct** classes of artefact must be mirrored. Mirroring only the first two is the
most common airgapped failure, and it fails late - at the `build` phase of the first real
build, not at install time.

The mirror scripts live in the **kpack chart repository** (`scripts/mirror/`), because
everything below is named by that chart's values. Point them at the values the kpack release
is deployed with:

```bash
./pull-images.sh   -v /path/to/your-kpack-values.yaml
./pull-runtimes.sh -v /path/to/your-kpack-values.yaml
```

The second reads every `buildpack.toml` in the store's buildpackages and mirrors what they
download, so it follows the store rather than the runtimes this chart advertises. It can
carry versions no runtime offers. Narrowing `runtimes[].versions` shrinks what callers may
select, not what is mirrored.

### Container images - kpack platform

Pulled by the platform chart. Registry `ghcr.io`, repository prefix
`buildpacks-community/kpack/`, tag = the chart's `appVersion`:

| Image | Pulled by |
|-------|-----------|
| `controller` | kpack Deployment |
| `webhook` | kpack Deployment |
| `build-init` | every build pod (`prepare`) |
| `build-waiter` | every build pod |
| `rebase` | rebase builds (CVE patches) |
| `completion` | every build pod |
| `lifecycle` | referenced by the `ClusterLifecycle` |

### Container images - Paketo content

| Image | Used by |
|-------|---------|
| `paketobuildpacks/build-jammy-base` | `ClusterStack.spec.buildImage` |
| `paketobuildpacks/run-jammy-base` | `ClusterStack.spec.runImage` (and the running function) |
| `paketobuildpacks/go` | `ClusterStore` |
| `paketobuildpacks/nodejs` | `ClusterStore` |
| `paketobuildpacks/python` | `ClusterStore` |

These are pulled from the **kpack registry**, shared by every region and written by nobody,
so the inventory is mirrored once rather than once per region.

The **composed builder images** this platform *produces* are the exception. They are pushed
to `{region registry base}/<lang>` by that region's `ClusterBuilder` objects, so that
repository must exist and be writable in **each** region's registry.

### Runtime distributions - **not images**

A Paketo buildpackage ships the buildpack *logic and metadata*, **not** the language runtime.
Its `buildpack.toml` points at the public internet - the `cpython` buildpack carries 60
dependency entries of the form:

```toml
[[metadata.dependencies]]
  id       = "python"
  version  = "3.10.19"
  uri      = "https://www.python.org/ftp/python/3.10.19/Python-3.10.19.tgz"
  checksum = "sha256:a078fb2d7a216071ebbe2e34b5f5355dd6b6e9b0cd1bacc4a41c63990c5a0eec"
```

Airgapped, that fetch fails, so `BP_CPYTHON_VERSION` cannot be satisfied by the image alone.
The tarballs for every advertised `runtimes[].versions` entry must be mirrored **to the
artifact server** - they are files, not registry content. Nothing is bundled in the
buildpackage: each buildpack's `include-files` lists what goes into its image, and it is
`buildpack.toml` plus a few `bin/` scripts.

**Only the buildpacks that *provide* a tool download anything.** The ones that *use* it
(`pip-install`, `poetry-install`, `npm-install`, `go-build`, `*-start`) are pure logic.
Across the orders in BUILDING.md: Buildpack Topology this is the complete download set:

| Component | Entries (amd64) | Upstream hosts |
|-----------|-----------------|----------------|
| `cpython` | 30 | www.python.org, artifacts.paketo.io |
| `node-engine` | 11 | nodejs.org |
| `go-dist` | 5 | go.dev |
| `poetry` | 12 | files.pythonhosted.org |
| `pip` | 2 | artifacts.paketo.io |
| `watchexec` | 1 | github.com |

Two things keep this small:

- **Filter by what is advertised.** `cpython`'s 30 amd64 entries cover ten minor versions;
  only the `runtimes[].versions` on offer are ever requested - six files for 3.11/3.12/3.13,
  three if only the newest patch of each is kept.
- **A dependency is fetched only if its buildpack can run.** Narrowing the orders
  (BUILDING.md: Buildpack Topology) is what shrinks this list: with no pipenv or conda group,
  `pipenv` and `miniconda` never execute and their files are never needed.

Note the **five distinct upstream hosts**. That is why the mirror below uses
`{originalHost}` rather than a single flat prefix.

The authoritative list is always the `uri` and `checksum` fields in each buildpack's
`buildpack.toml`, readable with `pack buildpack inspect <image>`.

### Redirecting the download - `dependency-mirror`

Mirroring the tarballs is not enough: the buildpack still resolves the **public** URI from
`buildpack.toml`. Paketo's dependency resolver (`libpak`) offers two ways to redirect it.
They are **mutually exclusive** - libpak warns and ignores the mappings if both are set.

#### Preferred: a dependency mirror

One setting redirects **every** dependency, with no per-version list to maintain.

```yaml
env:
  - name: BP_DEPENDENCY_MIRROR
    value: https://artifactory.internal/artifactory/deps/{originalHost}
```

The resolver replaces the scheme, host and user from the mirror and **appends the original
path**:

```
buildpack.toml:  https://www.python.org/ftp/python/3.10.19/Python-3.10.19.tgz
resolved to:     https://artifactory.internal/artifactory/deps/www.python.org
                                                   /ftp/python/3.10.19/Python-3.10.19.tgz
```

Because the upstream path is preserved, a remote or generic repository that mirrors upstream
layout needs no per-file curation. Related knobs:

| Knob | Effect |
|------|--------|
| `BP_DEPENDENCY_MIRROR` | Default mirror for all upstream hosts |
| `BP_DEPENDENCY_MIRROR_<HOSTNAME>` | Per-host mirror (encode `.`/`-` as `__`, upper case) |
| `{originalHost}` | Placeholder substituted with the upstream hostname |
| `skip-path` | Strips a prefix from the original path when layouts differ |

Only the `https://` and `file://` schemes are accepted.

> **Credentials:** the resolver honours userinfo in the mirror URL, but a mirror needing auth
> must be supplied as a **binding** of type `dependency-mirror`, never as
> `BP_DEPENDENCY_MIRROR` env - env lands in the world-readable runtimes ConfigMap and in
> every `Image` spec.

#### Fallback: per-dependency mappings

A binding of type `dependency-mapping` maps each dependency's SHA256 to its mirrored
location. The checksum is both the lookup key and the integrity check, so a redirect cannot
silently serve the wrong file:

```
type:                                                              dependency-mapping
a078fb2d7a216071ebbe2e34b5f5355dd6b6e9b0cd1bacc4a41c63990c5a0eec:  https://artifactory.internal/.../Python-3.10.19.tgz
```

Use this only when the artifact server cannot reproduce upstream path structure. It must be
regenerated whenever a buildpackage bump (axis 1 above) changes the dependency set, or builds
break for the versions that moved.

Either form is attached per build through `spec.build.services`, alongside the CA binding.

## Why pip needs three variables of its own

Mounting the CA bundle (BUILDING.md: Trust: CA Injection) is enough for Go, git and Node:
Go's `crypto/x509` and OpenSSL read `SSL_CERT_FILE`, git reads `GIT_SSL_CAINFO`, and Node
appends `NODE_EXTRA_CA_CERTS` to its built-in roots, which npm inherits.

**pip reads none of them.** It verifies against the `certifi` bundle vendored inside the pip
package - public roots only - and consults neither the OS trust store nor `SSL_CERT_FILE`. An
internal PyPI index then fails with `CERTIFICATE_VERIFY_FAILED` no matter where the CA is
mounted, and the failure is easy to misread: pip cannot fetch the simple index, so it reports
the requirement as `(from versions: none)` and then `No matching distribution found`, which
looks like a missing package.

`PIP_CERT` (pip itself), plus `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` (its vendored
`requests`), are what actually redirect it.

> The same `pip install` succeeds on a RHEL host because Red Hat patches its packaged pip to
> de-vendor certifi and use the system trust store, so a CA in
> `/etc/pki/ca-trust/source/anchors/` is picked up with no configuration. The jammy build image
> runs upstream pip, which is unpatched.

Those three **replace** the trust set rather than adding to it, unlike `NODE_EXTRA_CA_CERTS`.
That is safe only because the OpenShift bundle is the complete store, system roots included;
a partial bundle would silently cut off every public host.

**Do not mount the bundle over `/etc/ssl/certs`.** A ConfigMap volume replaces the whole
directory, so on the jammy build image `/etc/ssl/certs/ca-certificates.crt` - the target of
the `/usr/lib/ssl/cert.pem` symlink OpenSSL reads - becomes a dangling link, and the hashed
`c_rehash` symlinks its CApath needs are gone too. Python ends up with an empty trust store
while Go still works, because Go falls back to scanning every file in that directory. The
build gets *further* than an unmounted one and fails somewhere that looks unrelated.
`build.caInjection.mountPath` defaults to `/etc/serverless/ca` for this reason.
