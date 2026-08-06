# Building - kpack + Cloud Native Buildpacks

How source becomes an image: buildpack topology, runtime versions, credentials,
the build flow, and what the API owns versus the build controller.

## Contents

- [Design Decisions (locked in)](#design-decisions-locked-in)
- [Overview & Goals](#overview--goals)
- [Buildpack Topology](#buildpack-topology)
- [Runtime Versions & Dependencies](#runtime-versions--dependencies)
- [Trust: CA Injection](#trust-ca-injection)
- [Registry & Git Credentials](#registry--git-credentials)
- [Build Flow](#build-flow)
- [Ownership: API vs Build Service](#ownership-api-vs-build-service)
- [Digest propagation](#digest-propagation)
- [Who writes the ksvc image](#who-writes-the-ksvc-image)
- [Moving a function's repository](#moving-a-functions-repository)
- [Pruning stranded Images](#pruning-stranded-images)
- [Active/Active Behaviour](#activeactive-behaviour)
- [Lifecycle & Cleanup](#lifecycle--cleanup)
- [Sample Manifests](#sample-manifests)
- [Airgapped Mirror Inventory](#airgapped-mirror-inventory)
- [Open Questions](#open-questions)

## Design Decisions (locked in)

| Topic | Decision |
|-------|----------|
| Build engine | **kpack** (Kubernetes-native Cloud Native Buildpacks), not `func`/Tekton |
| kpack install | The `kpack` Helm chart is a **subchart** of the platform chart |
| Buildpack content | `ClusterStack` and `ClusterStore` ship in the **kpack chart** (cluster-scoped); the `Builder`s ship in the **serverless-api chart** |
| Cluster singletons | Stack and store are cluster-wide, so the engine release owns them; the serverless-api chart references them by name |
| Languages | `go`, `python`, `node` |
| Stack | **One shared** jammy base stack for all languages |
| Build locality | **Local cluster** - each site builds its own image |
| Build namespace | The **workloads** namespace, so a function's Image is owned by its KSVC and one git Secret serves both the API and kpack (DEPLOYING.md: Chart Topology) |
| Image CR writer | The **API** (POST / PUT / build / webhook) |
| Digest propagation | The **build controller**, its own Deployment, watches `status.latestImage` in the local cluster and applies the ksvc with the new digest to *all* sites (BUILDING.md: Digest propagation) |
| Write model | **Full server-side apply** of the desired spec - never a partial patch |
| Rebuild trigger | Webhook sets `spec.source.git.revision` to the **pushed commit SHA** (idempotent). An explicit `POST .../build` annotates the **latest `Build`**, never the `Image`, so the desired state stays a pure function of the function definition |
| CA trust | **Kyverno mutation** injecting the OpenShift-injected CA bundle into build pods |
| Runtime downloads | **`BP_DEPENDENCY_MIRROR`** redirecting all buildpack dependencies at once, not per-SHA mappings |
| Registry credential | **One** ESO-managed secret: kpack **push** + function **pull** |
| Build cache | **Registry**, at `{base}/{group}/{name}_cache` - not kpack's default PVC per `Image`, which scales with the function count |
| Registry cleanup | Function delete **deletes both repositories** through Quay's management API, with a Quay OAuth token - robots cannot call it (BUILDING.md: Registry cleanup on delete) |
| Registry layout | `registry.url` + optional `registry.organization` + `build.builderRepository`, prefixing every image the chart and the API reference. **One** root for the Builders this platform composes and the functions it builds (BUILDING.md: Registry layout) |
| Git credential | **Per function** - caller-supplied, on a per-function ServiceAccount the API creates; never platform-wide |

---

## Overview & Goals

### Goals

- Build a function from git, in-cluster, fully **airgapped** - no egress to public
  registries, PyPI, npmjs or `proxy.golang.org`.
- Offer **three languages** (Go, Python, Node) with a selectable runtime version, from
  mirrored buildpack content.
- **Continuously rebuild** on base-image/buildpack CVE patches without user action -
  this is the reason kpack was chosen over a one-shot builder.
- Stay correct under **active/active** with a floating DNS address: concurrent or
  duplicated writes must never produce duplicate builds.
- Survive **switchover**: a cluster that has never built a given function must be able
  to reconstruct everything it needs from state that is already replicated to it.

### Non-goals (this phase)

- Reproducible/bit-identical builds across clusters (see BUILDING.md: Active/Active Behaviour).
- Per-tenant builder isolation - builders are shared platform infrastructure.
- Build caching tuned per language. *Where* the cache lives is settled - the registry, not
  a PVC per function (BUILDING.md: Build cache) - but its size and hit rate are not tuned.

---

## Buildpack Topology

```
ClusterStack  (build + run base images)  ┐
ClusterStore  (buildpackages)            ├──► Builder ──► composes and PUSHES a
order         (explicit components)      ┘                builder image to the registry
```

A `Builder` must report `Ready` with a `status.latestImage` before any `Image` referencing
it will build. **This is the first thing to check when a build never starts** - in an
airgapped cluster it usually means the Stack or Store could not pull from the mirror.

### Language mapping

| Runtime | Builder | Detection groups (supported paths) |
|---------|---------|-----------------------------------|
| `go` | `go` | vendored (`go-mod-vendor`), non-vendored (`go mod download`) |
| `python` | `python` | `requirements.txt` (pip), `pyproject.toml` (poetry x2) |
| `node` | `node` | npm (`npm-install`) |

Orders name **component** buildpacks explicitly rather than the language composites, so the
platform supports exactly the paths it mirrors. yarn, pipenv and conda groups are omitted:
an app on one of those fails at `detect` with "no group passed" instead of failing deep in
a build on a dependency that was never mirrored. Narrowing does not shrink the image
mirror - it shrinks the dependency mirror (BUILDING.md: Airgapped Mirror Inventory), because only buildpacks that can run
ever download.

**TypeScript is not offered.** Paketo has no TypeScript buildpack - TS builds through the
Node.js buildpack, which runs the project's build script and therefore needs the
`typescript` compiler pulled from npm as a devDependency. Without that mirrored, the build
fails at `npm install`, so the runtime is not advertised. `node-run-script` stays in the
node order (plain JS projects use build scripts too), so a TS app becomes buildable the
moment `npm_config_registry` points at a mirror carrying its devDependencies - no chart
change beyond re-adding the runtime entry.

One shared `ClusterStack` (jammy base) serves all three builders. Per-language stacks
(e.g. Go on `tiny`/`static` for smaller images) are a later optimisation.

---

## Runtime Versions & Dependencies

Three independent axes. Conflating them is the most common source of confusion:

| Axis | What it pins | Where it is set |
|------|--------------|-----------------|
| 1. Buildpack content | The mirrored Paketo image tags | kpack chart: `clusterBuild.stacks[].{build,run}Image.tag`, `clusterBuild.stores[].sources[].tag` |
| 2. Language runtime | CPython / Node / Go version | `BP_*_VERSION` build env |
| 3. App dependencies | pip / npm / go modules | package-manager env pointing at the on-prem artifact server |

### Axis 2 - runtime version

| Runtime | Env var |
|---------|---------|
| python | `BP_CPYTHON_VERSION` |
| go | `BP_GO_VERSION` |
| node | `BP_NODE_VERSION` |

> Selecting a version only *asks* for it - the buildpack still has to fetch that runtime
> from the internet. Offline, this axis works only once the download is redirected to the
> mirror (BUILDING.md: Airgapped Mirror Inventory, BUILDING.md: Airgapped Mirror Inventory).

### Axis 3 - application packages (airgapped)

The package managers run **inside the build pod** and cannot reach the internet. They are
pointed at the on-prem artifact server:

| Runtime | Env |
|---------|-----|
| python | `PIP_INDEX_URL` (+ `PIP_EXTRA_INDEX_URL`) |
| node | `npm_config_registry` |
| go | `GOPROXY`, `GOSUMDB=off` (or vendored deps with `GOFLAGS=-mod=vendor`) |

> Do **not** use `PIP_TRUSTED_HOST`, `npm strict-ssl=false`, `GOINSECURE` or
> `NODE_TLS_REJECT_UNAUTHORIZED=0`. TLS verification stays on; trust comes from the CA
> injected in BUILDING.md: Trust: CA Injection.

### Where it lives

All of this is **data**, carried by the existing `runtimes` list in `values.yaml`. It
serialises into the runtimes ConfigMap through `toYaml` and is read by
`api/services/builder/runtimes.py`, whose `RuntimeSpec` is `extra="allow"` - so these fields flow
end-to-end with no template or model change:

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

**The runtimes file is the contract.** `RuntimeSpec` declares every key the builder
reads - `builder`, `versionEnv`, `defaultVersion`, `versions`, `buildEnv`,
`buildEnv` - and keeps unknown keys, so a newer chart can be rolled out ahead of the
API. Numbers are coerced to strings, because an unquoted `defaultVersion: 3.12` is a float
in YAML and rejecting it would take down every runtime over a missing pair of quotes.

The file is **required and has no fallback**. A built-in default list would be
indistinguishable from a real one at the API surface while naming no Builder, so a broken
mount would look like a working platform right up until the first function failed to
build. Instead `load_runtimes` raises, the lifespan loads it before serving, and a
misconfigured pod never reaches readiness.

A runtime that is present but maps to no Builder - a partially filled ConfigMap - is
caught separately, as a `400 runtime 'python' is not buildable` before the 202, rather
than minutes later as a failed background deploy that reads like a broken build.

**Coupling warning.** Axis 2 is bounded by axis 1: a pinned buildpackage only *contains*
certain interpreter versions, and in an airgapped cluster there is no fallback download.
Whenever a `clusterBuild.stores[].sources[].tag` is bumped, re-check that every advertised
`runtimes[].versions` entry is still available, or builds will fail at `detect`/`build`.

### Registry layout

Registries that namespace their repositories - Harbor projects, Quay and GitLab
organizations, Artifactory repository keys - need a path segment between the host and
the repository. `registry.organization` supplies it, `build.builderRepository` adds the
one everything the platform builds sits under, and the rest derives from those three:

```
{registry.url}/{registry.organization}/{build.builderRepository}/...   <- the "registry base"

  base/{name}                                       Builder tags
  base/{group}/{name}:{branch}                      function images (the API)
  base/{group}/{name}_cache:latest                  build layer cache (BUILDING.md: Build cache)
```

One value covers the Builders and the functions deliberately: they are pushed by the same
credential, mirrored together, and cleaned up against the same root, so a second value
would only be a second thing to keep in step. A function cannot collide with a Builder -
a Builder is one path component below the base and a function is two.

Either segment may be empty and is then skipped, so the flat `{host}/{group}/{name}`
layout still produces no doubled slash. `RegistryConfig.path` is the single derivation:
the image reference hangs off it and the repository *delete* addresses Quay by the same
string with the host removed, so the repository that is deleted is the one that was
pushed to.

### Moving a function's repository

`spec.tag` is **immutable on a kpack `Image`** - `validateTag` compares against the
baseline on every update and rejects a change at admission:

```go
if apis.IsInUpdate(ctx) {
    original := apis.GetBaseline(ctx).(*Image)
    return validate.ImmutableField(original.Spec.Tag, is.Tag, "tag")
}
```

So a moved tag cannot be *applied over*. Left as an ordinary apply it does not merely fail
to migrate - it wedges the function: every later write emits the `Image` manifest, so a
`PUT` that has nothing to do with the registry is rejected too, until someone deletes the
object by hand.

The API therefore **deletes the `Image` and lets the apply recreate it** whenever the
computed tag differs from the deployed one (`WorkloadService.retag_build`, one GET on the
build site per write). Three things follow:

- **A new `Image` has no prior `Build`, so it builds immediately.** Nothing has to ask for
  one; changing the layout and sending any `PUT` is the whole migration.
- **The old repository and its cache are reclaimed** through the same Quay API the delete
  path uses (BUILDING.md: Registry cleanup on delete). Cleanup on delete derives the
  *current* layout, so without this each function would leak a repository pair permanently
  - and the mutable tag would be left pointing at content nothing tracks. The reclaim is
  skipped when the old reference is on a different **host**: this token addresses one
  registry, and a same-named path elsewhere is somebody else's repository.
- **Build history resets.** `Build`s are owned by the `Image`, so deleting it collects
  them. Acceptable for a function that is about to rebuild anyway.

The workload keeps serving its existing digest throughout - the create is the only path
that writes the ksvc image (BUILDING.md: Who writes the ksvc image) - so the old repository
must not be reclaimed before the new build lands. It is not: the reclaim runs against the
*previous* tag only after the `Image` has been replaced, and the running pods already hold
their image.

The stack and store images are *mirrored* content rather than something this platform
builds, so they sit under the organization but **not** under `build.builderRepository`.
The kpack chart prefixes them with its own `clusterBuild.registry` - set that to
`{registry.url}/{registry.organization}`:

```
{clusterBuild.registry}/...                         <- the kpack chart's prefix

  base/paketobuildpacks/build-jammy-base:<tag>      ClusterStack
  base/paketobuildpacks/<component>:<tag>           ClusterStore sources
```

One deliberate exception: the pull/push Secret's `auths` key stays `registry.url` with
**no** organization. Docker credentials are keyed by registry *host*; adding the path
there produces a secret that silently never matches, and the failure surfaces as an
unauthenticated pull much later.

The chart and the API must agree on this, so the same rule is implemented twice - the
`serverless-api.registryBase` template helper and `RegistryConfig.base` in
`common/config.py`. The Deployment passes both halves as `SERVERLESS_REGISTRY__URL` and
`SERVERLESS_REGISTRY__ORGANIZATION`; changing one implementation without the other will
push builder images and function images to different places.

---

### Build pod resources

A build is far heavier than the function it produces - a dependency resolve plus a compile
- and it now draws on the workloads namespace quota (DEPLOYING.md: Chart Topology). `build.resources` sets
`Image.spec.build.resources`. Unset, the build pod is BestEffort and is the first thing
evicted under node pressure.

**One bound for every build, deliberately.** A per-language override was tried and
removed: the variance that matters is between a small function and a large one, not
between Go and Node, so language is the wrong axis to tune on. If per-build tuning is ever
needed it belongs on the function, alongside its `size` - a different feature, not a
generalisation of this one.

### Build cache

kpack can cache build layers - the restore/export ends of the lifecycle - in one of two
places, and the choice is per `Image`:

| Form | `spec.cache` | Cost |
|------|--------------|------|
| Volume | `volume.size: 2Gi` | a **PVC per function**, provisioned in full whether or not a build ever fills it |
| Registry | `registry.tag: <ref>` | blobs in the registry the build already pushes to |

**The API writes the registry form.** The volume form's cost scales with the number of
functions rather than with how much is actually cached, so a platform with a few hundred
functions is carrying a few hundred idle PVCs - and on a `ReadWriteOnce` StorageClass each
one also pins its build to the node holding it. The registry is storage the platform
already runs, the build `ServiceAccount` already carries a push credential for it
(BUILDING.md: Registry & Git Credentials), and nothing new has to be provisioned per function.

The cache is a sibling repository of the function image:

```
base/{group}/{name}:{branch}                      function images
base/{group}/{name}_cache:latest                  that function's layer cache
```

The `_` is load-bearing, not cosmetic. A name is a DNS-1123 label, which admits only
`[a-z0-9-]`, so no function can ever be named `{name}_cache` and the two repositories
can never be the same one. Two alternatives were rejected for colliding:

- **A reserved tag** in the function's own repository (`{name}:cache`) - a branch named
  `cache` projects to exactly that tag, and image and cache would overwrite each other.
- **A nested path** (`{name}/cache`) - collision-free, but it adds a repository level.
  Quay's native model is two-level (`namespace/repository`), so a nested path needs
  `FEATURE_EXTENDED_REPOSITORY_NAMES`. The suffix form needs no such flag, and keeps the
  cache inside whatever namespace the function image already lives in.

The cache is per `Image` - that is, per function, not per branch. There is one `Image` per
function whose `spec.tag` follows the branch, so keying the cache by branch would strand
the old cache on every branch change and start cold each time.

`build.cache: inherit` writes **no** `spec.cache` at all. That is the escape hatch for an
install that wants kpack's own behaviour, not a way to disable caching: a stock kpack
defaults an `Image` with no cache spec to a volume cache, which is the case this setting
exists to avoid.

## Trust: CA Injection

Internal TLS (git, the registry, the artifact server) is signed by the internal CA. The
build pod must trust it - **verification is never disabled**.

**Mechanism: a Kyverno `ClusterPolicy`** that mutates kpack build pods, mounting the
existing OpenShift-injected `ca-bundle` ConfigMap (already created in the workloads
namespace by `templates/ca-bundle.yaml`) and setting the per-tool CA env vars.

Two properties make this the right choice over a CNB `ca-certificates` service binding:

- The binding only affects the **build** phase. It does **not** cover `prepare`
  (build-init), which is where kpack does the **git clone** - so an internal-CA git
  server would still fail.
- The bundle rotates with OpenShift; no ExternalSecret, no `ca-certificates` entry in
  every builder `order`.

> **The policy must mutate `initContainers`, not just `containers`.** The kpack lifecycle
> runs as init containers (BUILDING.md: Build Flow); `completion` is the only main container. A policy that
> patches `spec.containers` alone silently does nothing to the phases that clone source and
> run package managers.

Because the OpenShift bundle is the **complete** trust store (system CAs + internal CA),
mounting it at a path and exporting `SSL_CERT_FILE` / `NODE_EXTRA_CA_CERTS` / `PIP_CERT` /
`GIT_SSL_CAINFO` is preferred over overwriting `/etc/ssl/certs/ca-certificates.crt`.

**Operational risk:** Kyverno becomes a hard dependency of the build path. The policy
therefore ships with `failurePolicy: Fail` (`build.caInjection.failurePolicy`): a build pod
is *rejected* if Kyverno cannot mutate it, rather than starting without the CA and dying
later with an opaque TLS error from pip, npm or git. The cost is that Kyverno being down
blocks builds - acceptable, since builds are asynchronous and retried, and a rejected pod
names the cause. Set it to `Ignore` only if you would rather builds proceed unmutated.

Cover the path with a smoke test that builds a function pulling one internal dependency;
that is the only thing that proves the mount reached the phase that needed it.

If Kyverno is not available, set `build.caInjection.enabled: false` and bake the CA into
the mirrored stack images instead (`update-ca-certificates` at mirror time) - that also
covers the run image, so the running function trusts internal TLS too.

---

## Registry & Git Credentials

### One registry secret, two roles

A **single** ESO-managed `kubernetes.io/dockerconfigjson` secret serves both ends of the
image's life:

```
ExternalSecret ──► Secret (dockerconfigjson)
                     ├──► build ServiceAccounts     → kpack PUSHES the built image
                     └──► ksvc imagePullSecrets      → Knative PULLS it to run
```

This is deliberate: the image is pushed to and pulled from the same internal registry, so
splitting the credential would mean maintaining two secrets with identical contents.

**kpack reads the two SA fields differently** - put the secret in both:

| SA field | Used for |
|----------|----------|
| `secrets:` | Registry auth for **push** (`spec.tag`) and pulling stack/store images |
| `imagePullSecrets:` | The build **pod** pulling the composed builder image |

### Git credential - per function, never shared

Unlike the registry, **the git token belongs to the function, not the platform**: the caller
supplies it on create, and the API persists it as `{workload}-git` (per BUILDING.md: Build Flow, because
rebuilds happen without the caller - CVE patches, webhooks).

kpack resolves git credentials from the ServiceAccount named by the `Image`, matching
secrets by host annotation. A single shared account would therefore hand **one tenant's
token to another tenant's build** - so there is no platform-wide git credential anywhere in
this design.

Instead there are **two kinds of ServiceAccount**:

| Account | Created by | Holds | Used by |
|---------|-----------|-------|---------|
| `kpack-builder` | the chart | registry credential only | `Builder` objects (compose + push a builder image; never clone source) |
| `fn-{name}-{group}` | the **API**, per function | that function's git Secret **+** the shared registry credential | the function's `Image` |

The per-function account is created alongside the function and named on its `Image`:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: fn-hello-payments
  namespace: serverless-workloads       # with the Image and the KSVC (DEPLOYING.md: Chart Topology)
secrets:
  - name: serverless-registry-creds     # shared, from the chart (BUILDING.md: Registry & Git Credentials)
  - name: hello-payments-git            # this function's token, from the API
imagePullSecrets:
  - name: serverless-registry-creds
```

The chart passes the shared registry Secret's name as
`SERVERLESS_BUILD__REGISTRY_SECRET` and grants the API `serviceaccounts` write (DEPLOYING.md: RBAC).

The account and the git Secret it names must sit in the **same namespace as the
`Image`** - kpack resolves a build's credentials from the ServiceAccount named on the
Image, in the Image's own namespace, and looks nowhere else. All three are in the
workloads namespace, which is what makes one git Secret enough.

> **One Secret, two readers - implemented.** `{workload}-git` is
> `kubernetes.io/basic-auth` (`username` + `password`) annotated
> `kpack.io/git: <scheme>://<host>`. kpack clones with it - it reads no other shape - and
> the API reads the password back so a later edit rebuilds without the client re-sending
> the token. One shape and one decode path - `load_existing` reads the `password` key
> through `_secret_data`, the same way it reads a workload's env and files.

---

## Build Flow

### Resource chain

```
API creates:      Image                     ← the only object the API manages
                    │
kpack creates:      ├──► SourceResolver     ← resolves the git ref to a concrete SHA
                    │
                    └──► Build              ← one per actual build
                          │
                          └──► Pod          ← the CNB lifecycle
```

### Inside the build pod

The lifecycle runs as **init containers**, in order; `completion` is the main container:

| # | Container | Does | Config that matters |
|---|-----------|------|---------------------|
| 1 | `prepare` | git clone, credential setup | git secret, **CA** |
| 2 | `analyze` | reads previous image metadata | - |
| 3 | `detect` | runs the `order`, first passing group wins | builder `order` |
| 4 | `restore` | restores cached layers | - |
| 5 | `build` | **runs the buildpacks** - pip / npm / go | `BP_*_VERSION`, artifact-server env, **CA** |
| 6 | `export` | assembles OCI layers, **pushes** | `spec.tag`, push credential |
| - | `completion` *(main)* | finalise, optional attestation | - |

Because each phase is a named container, `Cluster.pod_logs(pod, container=...)` (already
implemented) yields **per-phase build logs** - the difference between "build failed" and an
actionable error.

### What causes a new Build

| Reason | Trigger | In this platform |
|--------|---------|------------------|
| `CONFIG` | `spec` changed | PUT that changes runtime, version or env |
| `COMMIT` | resolved source SHA changed | the per-function **webhook** |
| `TRIGGER` | the latest `Build` carries `image.kpack.io/additionalBuildNeeded` | `POST .../functions/{name}/build` |
| `BUILDPACK` | a Store buildpackage was updated | ops bumps buildpack content |
| `STACK` | the Stack run image was updated | **CVE patch** - often a fast *rebase* |

`BUILDPACK` and `STACK` fire with **no user action**. Digest propagation must therefore be
event-driven (BUILDING.md: Ownership: API vs Build Service), not only triggered by API writes.

`TRIGGER` is the one reason that is imperative rather than a state change, and it is why
the explicit rebuild exists: nothing about the function changed, so there is no desired
state to write that would produce a build. kpack looks for the annotation on the **latest
`Build`**, not on the `Image` - the next `Build` inherits the *Image's* annotations, which
never carry it, so one request produces exactly one build and no loop.

> **Never put the trigger on the `Image`.** It is a nonce, and the `Image` spec is a pure
> function of the function definition (BUILDING.md: Convergence rules). On the `Image` it
> would look like a change to every apply, and the next ordinary `PUT` - which composes the
> spec from the request, without it - would drop it and build once more.

---

## Ownership: API vs Build Service

Two components, split by execution model:

| Component | Path | Responsibility |
|-----------|------|----------------|
| **API** | request/response | On POST / PUT / build / webhook: compose the desired `Image` and server-side apply it to the **local** cluster. Returns `202`. |
| **Build controller** | control loop | Watches `Image.status.latestImage` in the local cluster. On change, applies the ksvc with the new **digest** to **all** sites (BUILDING.md: Digest propagation). |

The watch loop does not fit a request/response API, and the shared library already
anticipates this split (`common/cluster.py`: *"the API and a future builder service both
reach a cluster the same way"*; `common/labels.py`: *"a future builder service stamps them
on its build resources"*).

**Contract change - implemented.** `BuildBackend.build` no longer returns a finished image. It
records desired state (git Secret -> ServiceAccount -> `Image`, in dependency order) and
returns the deterministic tag the build will push to; the ksvc is applied against that tag
immediately, and `GET` reports `Building` until kpack finishes (FUNCTIONS.md: Function Status Resolution). "Created" no longer
implies "serving", which is why the status code for `Building` is `202`.

The manifests are **owned resources of the KSVC**, applied in the same pass as the
function's env Secret and DomainMapping and carrying the same `ownerReference`. That is
what deletes them with the function (BUILDING.md: Lifecycle & Cleanup) - there is no cleanup code, because there is
nothing to clean up.

`BuildBackend.plan` splits them by how far each piece travels, and the split is load-bearing:

| | Scope | Why |
|---|---|---|
| git `Secret` | **every site** | Only one site builds, but every site must be *able* to. After a switchover the new local site rebuilds from the token it already holds, and nothing can recover a token whose only copy was on the site that went away (BUILDING.md: Active/Active Behaviour). |
| `Image` + build `ServiceAccount` | **the local site** | Replicating them would have every site build the same source and race to push the same tag (BUILDING.md: Active/Active Behaviour). |

The building site is **always the local one**, whether or not the function runs there.
The registry is shared, so a site that only runs the workload pulls what the local site
pushed, and the site that reads build status is always the site that has the `Image`.

When the request's target sites exclude the local one, it receives the build objects and
nothing else - no `KSVC`, no `DomainMapping`. Those objects are applied **unowned**: an
`ownerReference` must name an owner in the same cluster, and the `KSVC` that would be it
was never applied here. Nothing cascades, so `delete` removes them by name (`Image`,
build `ServiceAccount`, git `Secret`); a leftover `Image` would keep rebuilding a
function that no longer exists.

`manifests` is emitted on **every** create and update, not only when a build input changed.
Re-applying an unchanged spec is a no-op kpack does not rebuild from, but it recreates the
`Image` on a site that has never had one - which is what makes a PUT after a switchover
self-healing (BUILDING.md: Active/Active Behaviour).

### Who writes the ksvc image

Exactly one writer per phase, with no overlap:

| Path | ksvc image |
|------|-----------|
| POST | **written once**: `{registry.url}/{organization}/{builderRepository}/{group}/{name}:{branch}` |
| PUT | **kept** - whatever the workload is running, read back off it |
| `POST .../build` | **not written** - no ksvc is applied at all |
| build controller | **the only writer after the create**, and only ever the digest |

A create has nothing to keep, so it deploys at the branch tag and reads `Building` until a
build pushes something there. After that the tag is never written again: it resolves to the
digest already running, so writing it cuts a revision of *the same code*, and the real
rollout arrives minutes later from the controller anyway. Two revisions where one belongs.

This is also what lets a **moved repository** work (BUILDING.md: Registry layout). The
controller does not compare repositories - it cannot, being the only writer - so the first
build that pushes to the new layout moves the workload there on its own. The update that
re-tags the `Image` and the roll-out are separate events, in that order, which is why the
migration reads "build first".

`POST .../functions/{name}/build` is the manual half of that: it re-applies the same
composed `Image` and then asks kpack for one more build of it, so a function can be rebuilt
without inventing a spec change (FUNCTIONS.md: Building again without changing anything).

Still to come, and deliberately out of scope for the current implementation: the
per-function webhook endpoint that pins a pushed SHA to `spec.source.git.revision`
(`BuildRequest.revision` already carries it).

---

## Digest propagation

The `Image` says what to build; `status.latestImage` says what was built. Nothing in a
request/response path can observe the second - a `STACK` or `BUILDPACK` rebuild fires with
nobody asking (BUILDING.md: What causes a new Build) - so a control loop closes the gap.

`controller/` is that loop, in its own Deployment (`{name}-build-controller`), running the
API's image with a different entrypoint. One image means the two services cannot drift in
the library they share; separate Deployments mean a watch loop and an HTTP API scale and
restart on their own terms.

### One pass

```
list Images (local)  ──►  reconcile each  ──►  watch from that resourceVersion
      ▲                                              │
      └──────────────  stream ends (timeout)  ───────┘
```

Event-driven, without depending on having *seen* every event. A dropped connection or an
expired `resourceVersion` costs one extra relist, not a function stuck on an old digest.
`buildController.resyncSeconds` (default 300) is both the watch's lifetime and, therefore,
the relist interval - one knob, because they are the same number.

Reads are **local only**: a function's `Image` exists in exactly one cluster, the one that
built it (BUILDING.md: Active/Active Behaviour). Writes go to **every** site, because the
registry is shared and a site that only runs the workload pulls what this site pushed.

### What it writes

The controller does **not** compose a KSVC. The API owns that spec; the controller owns one
field of it. So it applies the *live* object with the image replaced - a full server-side
apply, like every other write path (BUILDING.md: Active/Active Behaviour), of an object
that has been stripped of the metadata the server owns (`managedFields`, `resourceVersion`,
`uid`, …) and of any pinned `spec.template.metadata.name`, which Knative would reject.

Two things stop a write. The repository is deliberately **not** one of them: this is the
only writer of the image after the create (BUILDING.md: Who writes the ksvc image), so
refusing a moved one would strand the workload on a repository nothing pushes to.

| Condition | Why it is left alone |
|---|---|
| The KSVC already runs that digest | The loop's normal outcome, and why a resync costs nothing |
| It is not labelled `offering: function` | A container that reused a deleted function's name must not inherit its image |

### No leader election

Two replicas - or two sites' controllers reaching the same conclusion - apply the same
desired state, and a server-side apply of identical content is a no-op that produces no
Knative revision. Same convergence rules as every other writer (BUILDING.md: Convergence
rules); `buildController.replicaCount` above 1 is safe, just redundant.

Two controllers never see the same input, so there is normally nothing to contend over: a
function's `Image` exists in exactly one cluster, and only that site's controller has an
opinion about it. The one way both sites hold a live `Image` for one function is a
switchover, and that is what the prune below removes.

### Pruning stranded Images

A switchover leaves the previous site's `Image` objects in place. They keep firing
`STACK`/`BUILDPACK` rebuilds, and because builds are not bit-reproducible they push a
*different* digest from the same source - so both sites' controllers would publish, and
each swap rolls a Knative revision of identical code.

Each resync therefore also compares this site's `Image`s with the other sites' and deletes
the ones this site has superseded, by `metadata.creationTimestamp`:

| | |
|---|---|
| Newer here | delete theirs |
| Newer there | do nothing - their controller will delete ours |
| Same age, or either timestamp unreadable | do nothing |

**Only the newer site acts**, so the two can never delete each other's. It deletes
*outward* rather than deleting its own on losing, deliberately: the stranded site is the
one that may be down, and its controller with it, so a prune that ran only locally would
never fire in the case it exists for.

**A site that cannot be listed stops the pass** - not just that site. Deciding what is
stranded from a partial view is how a transient read failure deletes every live build.

Timestamps come from two API servers, so a few seconds of clock skew is possible; a tie
prunes nothing, and the gap this is aimed at is the minutes or hours between a switchover
and the next build.

**It assumes writes land at one site at a time**, which is what the API's DNS already does
(ARCHITECTURE.md: Networking & Exposure - one active site, the other on failover). Were
both sites to take writes for the *same* function concurrently, each would keep recreating
its `Image` and pruning the other's, cancelling builds in flight. That is the same
assumption the rest of the build path rests on - it is why an `Image` normally exists in
one cluster at all - but this is the component that would misbehave loudest if it stopped
holding. `buildController.pruneOrphans: false` turns it off, leaving the
stranded Images rebuilding.

The deleted `Image` is not missed. Nothing needs it to *run* the function - the registry is
shared - and the next write at that site recreates it (BUILDING.md: Active/Active Behaviour).

---

## Active/Active Behaviour

### Builds are local

Each site builds in its **own** cluster, so the full build stack (kpack, Stack, Store,
Builders) is installed in **every** cluster. The `Image` CR exists only in the cluster that
built it.

### Every write path is a full server-side apply

`Cluster.apply()` already uses `apply=True, force_conflicts=True`; **server-side apply is
create-or-update by construction**. Every path therefore composes the *complete* desired
`Image` and applies it:

| Path | Behaviour |
|------|-----------|
| POST | compose -> apply -> creates |
| PUT | compose -> apply -> **creates if missing**, else updates. Keeps the ksvc image (BUILDING.md: Who writes the ksvc image) |
| build | reconstruct (BUILDING.md: Active/Active Behaviour) -> apply -> **creates if missing** -> annotate the latest `Build` |
| webhook | reconstruct (BUILDING.md: Active/Active Behaviour) + `revision` = pushed SHA -> apply -> **creates if missing** |

The build applies *before* it triggers, and that ordering is what makes it self-healing
rather than merely idempotent: on a site that has never built the function the apply
creates the `Image`, which builds on its own, and there is no `Build` to annotate - so the
trigger finds nothing and says so instead of failing. It is also the one write path that
leaves the `KSVC` alone: the function's desired state does not change, so there is nothing
to fan out to the other sites, and only the local site (which builds) is touched.

> **Never use a targeted patch** (e.g. patching only `spec.source.git.revision`). It
> returns 404 when the object is absent - precisely the post-switchover case this design
> must survive.

### Reconstruction after switchover

A cluster that never built a function can still compose its `Image`, because the inputs are
already replicated to every site:

| Input | Source |
|-------|--------|
| runtime | ksvc annotation `ANNOTATION_RUNTIME` |
| git url | ksvc annotation `ANNOTATION_GIT_URL` |
| branch | ksvc annotation `ANNOTATION_GIT_BRANCH` |
| builder, version env, build env | runtimes ConfigMap |
| git token | the persisted git secret |
| registry credential | the ESO-managed secret (BUILDING.md: Registry & Git Credentials) |

No database and no cross-cluster state replication is required - the Knative Service is the
replicated source of truth.

### Convergence rules

Concurrent writers are safe **only** if the composed spec is a pure function of the function
definition. Duplicate builds come from nonces, not from concurrency:

1. **Deterministic name** - `fn-{name}-{group}`.
2. **No timestamps, UUIDs or counters** anywhere in the spec.
3. **Never set `spec.build.creationTime`.** The field exists in kpack's `ImageBuild` type
   and setting it forces a rebuild on every apply.
4. **The webhook sets a SHA, not a trigger annotation.** Bumping
   `image.kpack.io/additionalBuildNeeded` is a nonce: two instances handling one push would
   produce two builds. `spec.source.git.revision = <pushed SHA>` is idempotent by data.

With these, two instances applying the same desired state produce one object and kpack
creates **one** build - no lease or leader election is required.

The explicit rebuild is not an exception to rule 4, because it is not a write of desired
state: the `Image` it applies is composed the same way every other path composes it, and
the trigger annotation goes on the latest `Build` afterwards. The nonce rule is about state
that gets *re-applied* - a value in the `Image` spec is asserted again on every write, so a
timestamp there rebuilds forever. An annotation on one `Build` is asserted once. Two
instances handling one rebuild request would patch the same `Build` and kpack would still
create one build; two clients asking twice **should** get two builds, which is what asking
twice means.

### Accepted consequences

- **A post-switchover write rebuilds.** The new cluster has no `Image`, so the first
  PUT/webhook builds from scratch. Builds are not bit-reproducible, so the digest differs
  from the previous cluster's and a new Knative revision rolls out even when the source is
  unchanged. It is bounded to functions actually touched after switchover.
- **Orphaned Images keep building, until the next prune.** The previously-active cluster
  still holds `Image` objects and keeps firing `STACK`/`BUILDPACK` rebuilds. The build
  controller deletes them once the new site has built the function itself, so the window
  is one resync past the first build there (BUILDING.md: Pruning stranded Images) - not
  indefinite, but not instant either.

---

## Lifecycle & Cleanup

| Event | Action |
|-------|--------|
| Function delete | Nothing to do *in the cluster*: the `Image` and build `ServiceAccount` are owned by the KSVC, so deleting it garbage-collects them. Co-location is what buys this - ownerReferences cannot cross namespaces (DEPLOYING.md: Chart Topology). |
| Function delete (registry) | Nothing in the cluster owns registry content, so the API deletes both repositories by name - `{base path}/{group}/{name}` and `{base path}/{group}/{name}_cache` (BUILDING.md: Registry cleanup on delete). |
| Switchover | Orphaned `Image` objects remain in the previously-active cluster (BUILDING.md: Active/Active Behaviour). |
| Periodic prune | Each build-controller resync deletes `Image` objects the local site has superseded in the others (BUILDING.md: Pruning stranded Images). |

### Build history

Every `Image` carries an explicit `spec.successBuildHistoryLimit` /
`spec.failedBuildHistoryLimit`, from `build.history.success` / `build.history.failed`
(default **3** and **3**). kpack garbage-collects older `Build` objects, and a `Build` owns
its pod, so collecting one takes its completed pod with it.

They are set rather than left out, because "left out" is not "unbounded" - it is kpack's
own default of **10 and 10**, so **20 `Build`s and 20 completed pods per function**. That is
invisible at ten functions and is the whole namespace at three hundred: `oc get pods`
stops being usable, and every controller that lists pods pays for them. Anything that
re-triggers builds without a user - `STACK`/`BUILDPACK` CVE rebuilds, and now
`POST .../build` - fills that history faster than edits do.

Failed builds keep their own quota because their pods are the only place the per-phase
build log exists (BUILDING.md: Inside the build pod); dropping the limit to 1 would mean a
second failure erases the evidence of the first.

The limits are a constant from configuration, identical on every apply, so they converge
like the rest of the spec (BUILDING.md: Convergence rules). Lowering them takes effect on
each function's next build, not at once - kpack prunes when it creates a `Build`, so an
untouched function keeps its existing history until something rebuilds it.

### Registry cleanup on delete

Deleting a function deletes its image repository and its cache repository outright:

```
DELETE /api/v1/repository/{registry.organization}/{build.builderRepository}/{group}/{name}
DELETE /api/v1/repository/{registry.organization}/{build.builderRepository}/{group}/{name}_cache
```

The path is `RegistryConfig.path` - the image reference with the host removed - so the
repository deleted is exactly the one that was pushed to, and a layout change cannot make
cleanup miss.


This is **Quay's management API**, not the distribution API. The distribution API can only
delete manifests (`DELETE /v2/{repo}/manifests/{digest}`), which reclaims the same bytes but
leaves the repository itself in the registry's listing. Deleting the repository is what
matches the request: a deleted function leaves nothing behind.

**It needs a Quay OAuth token, not the push robot.** Robot accounts are registry
credentials - they authenticate `docker push`/`pull` and the `/v2` endpoints, and cannot
call `/api/v1` at all. The token is generated from an Application under a Quay organization
(Organization → Applications) and must carry `repo:admin`.

Two consequences of how Quay scopes that token, both worth checking before enabling:

- The token acts as **the user who authorized it**, so that user needs admin on every
  namespace the platform pushes to. If that user is deactivated the token stops working.
- With `registry.organization` empty - the chart default - **each group is its own Quay
  namespace** (`payments/hello`), so one token only reaches the groups its user administers.
  Setting `registry.organization` collapses everything into one namespace and one grant, at
  the cost of needing `FEATURE_EXTENDED_REPOSITORY_NAMES` for the nested path.

The token reaches the API pod through its own `ExternalSecret`
(`SERVERLESS_REGISTRY__API_TOKEN`). **Wiring that secret is what enables cleanup** - without
it the step is skipped, so an install that never adds it is unaffected by the upgrade.
`registry.deleteOnFunctionDelete: false` switches it off with the token still mounted.

Both repository paths come from `common.names.image_repository` / `cache_repository`,
under `RegistryConfig.path` - the same two functions and the same prefix the build pushes
through, so what cleanup deletes cannot drift from what was pushed. They sit beside `image_tag` because they are the same kind of rule: the
repository half of an image reference, where `image_tag` is the tag half. They take only
the validated `{group}`/`{name}` labels, never request input, and the call runs only for
the function offering - a container's image was built elsewhere and is not the platform's
to delete.

#### Accepted consequences

- **A crash leaks a repository.** Cleanup is best-effort and fired once, after every site
  confirms the delete; it is not reconciled. If the pod dies between the two, the
  repository survives and nothing will notice. Deliberate: the alternative is a
  reconcile pass that derives "unowned" from a cluster read, which deletes everything the
  moment that read wrongly returns empty.
- **A container pinned to a function's image breaks.** `image` on the container offering is
  grammar-validated only, not scoped to the caller's group, so a container may reference a
  function's image. Deleting the function removes it regardless.
- **A switchover orphan re-pushes.** An `Image` left in a previously-active cluster keeps
  rebuilding (BUILDING.md: Active/Active Behaviour) and will re-create the repository after cleanup ran,
  until it is pruned (BUILDING.md: Pruning stranded Images).
- **Reclamation is not immediate.** Deleting the repository removes it from the listing at
  once, but the underlying blobs come back when Quay garbage-collects, after its
  time-machine window has passed.
- **Quay-specific.** `/api/v1` is Quay's own API; moving to another registry means
  reimplementing this against that registry's equivalent.

---

## Sample Manifests

### Image (created by the API, local cluster)

```yaml
apiVersion: kpack.io/v1alpha2
kind: Image
metadata:
  name: fn-hello-payments              # deterministic: fn-{name}-{group}
  namespace: serverless-workloads      # owned by the KSVC (DEPLOYING.md: Chart Topology)
  labels:                              # common/labels.py
    serverless.platform/managed-by: serverless-api
    serverless.platform/workload: hello-payments
spec:
  tag: registry.internal/<org>/<repo>/payments/hello  # {base}/{group}/{name} (BUILDING.md: Registry layout)
  builder:
    kind: Builder
    name: python
  serviceAccountName: fn-hello-payments   # per-function: its git token + the shared registry cred
  source:
    git:
      url: https://git.internal/payments/hello.git
      revision: 9f2c1ab…               # pushed SHA (webhook) or branch
  cache:                               # registry, not a PVC (BUILDING.md: Build cache)
    registry:
      tag: registry.internal/<org>/<repo>/payments/hello_cache:latest
  build:
    env:
      - { name: BP_CPYTHON_VERSION, value: "3.12" }
      - { name: PIP_INDEX_URL, value: "https://artifactory.internal/artifactory/api/pypi/pypi/simple" }
    # NOTE: never set creationTime here - see BUILDING.md: Active/Active Behaviour
```

### Builder (serverless-api chart, per site)

```yaml
apiVersion: kpack.io/v1alpha2
kind: Builder
metadata:
  name: python
  namespace: serverless-workloads
spec:
  serviceAccountName: kpack-builder
  tag: registry.internal/<org>/serverless/builders/python
  stack: { name: serverless-base, kind: ClusterStack }
  store: { name: serverless-store, kind: ClusterStore }
  order:
    - group:
        - id: paketo-buildpacks/python
```

### ClusterStack + ClusterStore (kpack chart, cluster-scoped)

Rendered by the kpack release from its `clusterBuild` values (the kpack repo's
`examples/clusterbuild-values.yaml` is a working starting point):

```yaml
apiVersion: kpack.io/v1alpha2
kind: ClusterStack
metadata:
  name: serverless-base
spec:
  id: io.buildpacks.stacks.jammy
  buildImage: { image: registry.internal/paketobuildpacks/build-jammy-base:0.2.44 }
  runImage:   { image: registry.internal/paketobuildpacks/run-jammy-base:0.2.44 }
---
apiVersion: kpack.io/v1alpha2
kind: ClusterStore
metadata:
  name: serverless-store
spec:
  sources:
    - image: registry.internal/paketobuildpacks/go:4.6.0
    - image: registry.internal/paketobuildpacks/nodejs:5.1.0
    - image: registry.internal/paketobuildpacks/python:3.1.0
```

Both need `spec.serviceAccountRef` pointing at a ServiceAccount holding the mirror pull
credential when the internal registry requires auth. That account and its ExternalSecret
are created by the kpack chart too (`clusterBuild.serviceAccount` /
`clusterBuild.registrySecret`), so the objects and the credential they pull with stay in
one release.

### Build ServiceAccount (registry push/pull + git)

The account the **Builders** run as. No git credential: a Builder composes and pushes a
builder image, it never clones source. The per-function build account is in BUILDING.md: Registry & Git Credentials.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: kpack-builder
  namespace: serverless-workloads
secrets:                       # registry auth for push + stack/store pulls
  - name: serverless-registry-creds
imagePullSecrets:              # build pod pulling the composed builder image
  - name: serverless-registry-creds
```

### Kyverno policy - CA into build pods

Shipped as `templates/kpack/ca-policy.yaml`, gated on `build.caInjection.enabled`. Abridged:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: kpack-build-ca-bundle
spec:
  rules:
    - name: mount-ca-bundle
      match:
        any:
          - resources:
              kinds: [Pod]
              namespaces: [serverless-workloads]
              selector:
                matchExpressions:
                  - { key: kpack.io/build, operator: Exists }
      mutate:
        patchStrategicMerge:
          spec:
            volumes:
              - name: internal-ca
                configMap: { name: ca-bundle }
            # BOTH lists - the lifecycle runs as init containers (BUILDING.md: Trust: CA Injection, BUILDING.md: Build Flow)
            initContainers:
              - (name): "*"
                volumeMounts:
                  - { name: internal-ca, mountPath: /etc/serverless/ca, readOnly: true }
                env:
                  - { name: SSL_CERT_FILE,        value: /etc/serverless/ca/ca-bundle.crt }
                  - { name: GIT_SSL_CAINFO,       value: /etc/serverless/ca/ca-bundle.crt }
                  - { name: NODE_EXTRA_CA_CERTS,  value: /etc/serverless/ca/ca-bundle.crt }
                  - { name: PIP_CERT,             value: /etc/serverless/ca/ca-bundle.crt }
                  - { name: REQUESTS_CA_BUNDLE,   value: /etc/serverless/ca/ca-bundle.crt }
                  - { name: CURL_CA_BUNDLE,       value: /etc/serverless/ca/ca-bundle.crt }
            containers:
              - (name): "*"
                volumeMounts:
                  - { name: internal-ca, mountPath: /etc/serverless/ca, readOnly: true }
                env:   # the same six - `serverless-api.buildCaEnv` renders both lists
```

### Why pip needs three variables of its own

Mounting the bundle is enough for Go, git and Node: Go's `crypto/x509` and OpenSSL read
`SSL_CERT_FILE`, git reads `GIT_SSL_CAINFO`, and Node appends `NODE_EXTRA_CA_CERTS` to its
built-in roots (npm inherits that). **pip reads none of them.** It verifies against the
`certifi` bundle vendored inside the pip package - public roots only - and consults neither
the OS trust store nor `SSL_CERT_FILE`. So an internal PyPI index fails with
`CERTIFICATE_VERIFY_FAILED` no matter where the CA is mounted, and the failure is easy to
misread: pip cannot fetch the simple index, so it reports the requirement as
`(from versions: none)` and then `No matching distribution found` - which looks like a
missing package rather than a TLS problem.

`PIP_CERT` (pip itself), plus `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` (its vendored
`requests`), are what actually redirect it.

> This is also why the same `pip install` succeeds on a RHEL host: Red Hat patches its
> packaged pip to de-vendor certifi and use the system trust store, so an internal CA in
> `/etc/pki/ca-trust/source/anchors/` is picked up with no configuration. The jammy build
> image runs upstream pip, which is unpatched.

Note that those three **replace** the trust set rather than adding to it, unlike
`NODE_EXTRA_CA_CERTS`. That is safe only because the OpenShift bundle is the complete
store, system roots included; a partial bundle would silently cut off every public host.

**Do not mount the bundle over `/etc/ssl/certs`.** A ConfigMap volume replaces the whole
directory, so on the jammy build image `/etc/ssl/certs/ca-certificates.crt` - the target of
the `/usr/lib/ssl/cert.pem` symlink OpenSSL actually reads - becomes a dangling link, and
the hashed `c_rehash` symlinks its CApath needs are gone too. Python ends up with an empty
trust store while Go still works, because Go falls back to scanning every file in that
directory. The result is a build that gets *further* than an unmounted one and fails in a
place that looks unrelated. `build.caInjection.mountPath` defaults to `/etc/serverless/ca`
for this reason.

---

## Airgapped Mirror Inventory

Three **distinct** classes of artefact must be mirrored. Mirroring only the first two is
the most common airgapped failure, and it fails late - at the `build` phase of the first
real build, not at install time.

The scripts that mirror them live in the **kpack chart repository**
(`scripts/mirror/`), because everything below is named by that chart's values.
Point them at the values the kpack release is deployed with:

```bash
./pull-images.sh   -v /path/to/your-kpack-values.yaml
./pull-runtimes.sh -v /path/to/your-kpack-values.yaml
```

The second reads every buildpack.toml in the store's buildpackages and mirrors
what they download, so it follows the store rather than the runtimes this chart
advertises. That means it can carry versions no runtime offers - the store's
buildpackages support them, so a build could ask for them. Narrowing
`runtimes[].versions` shrinks what callers may select, not what is mirrored.

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

Plus the **composed builder images** this platform *produces* - they are pushed to
`{registry base}/<lang>` by the `Builder` objects (the base already carries
`build.builderRepository`), so that repository
must exist and be writable by the build ServiceAccount.

### Runtime distributions - **not images**

A Paketo buildpackage ships the buildpack *logic and metadata*, **not** the language
runtime. Its `buildpack.toml` points at the public internet - e.g. the `cpython` buildpack
carries 60 dependency entries of the form:

```toml
[[metadata.dependencies]]
  id       = "python"
  version  = "3.10.19"
  uri      = "https://www.python.org/ftp/python/3.10.19/Python-3.10.19.tgz"
  checksum = "sha256:a078fb2d7a216071ebbe2e34b5f5355dd6b6e9b0cd1bacc4a41c63990c5a0eec"
```

In an airgapped cluster that fetch fails, so `BP_CPYTHON_VERSION` (BUILDING.md: Runtime Versions & Dependencies axis 2) cannot be
satisfied by the image alone. The tarballs for every advertised
`runtimes[].versions` entry must be mirrored **to the artifact server** (they are files,
not registry content):

Proof that nothing is bundled: each buildpack's own `include-files` lists everything that
goes into its image - `buildpack.toml` plus a few `bin/` scripts, and no archives.

**Only the buildpacks that *provide* a tool download anything.** The ones that *use* it
(`pip-install`, `poetry-install`, `npm-install`, `go-build`, `*-start`, ...) are pure
logic. Across the orders in BUILDING.md: Buildpack Topology this is the complete download set:

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
- **A dependency is fetched only if its buildpack can run.** Narrowing the orders (BUILDING.md: Buildpack Topology) is
  what shrinks this list: with no pipenv or conda group, `pipenv` and `miniconda` never
  execute and their files are never needed.

Note the **five distinct upstream hosts** - that is why the mirror in BUILDING.md: Airgapped Mirror Inventory uses
`{originalHost}` rather than a single flat prefix.

The authoritative list is always the `uri` + `checksum` fields in each buildpack's
`buildpack.toml`, readable with `pack buildpack inspect <image>`.

### Redirecting the download - `dependency-mirror`

Mirroring the tarballs is not enough: the buildpack still resolves the **public** URI from
`buildpack.toml`. Paketo's dependency resolver (`libpak`) offers two ways to redirect it.
They are **mutually exclusive** - libpak warns and ignores the mappings if both are set.

#### Preferred: a dependency mirror

One setting redirects **every** dependency, with no per-version list to maintain. libpak's
own documentation gives this as the reason it exists: *"avoiding too many
dependency-mapping bindings"*.

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

Because the upstream path is preserved, a **remote/generic repository that mirrors upstream
layout** needs no per-file curation. Related knobs:

| Knob | Effect |
|------|--------|
| `BP_DEPENDENCY_MIRROR` | Default mirror for all upstream hosts |
| `BP_DEPENDENCY_MIRROR_<HOSTNAME>` | Per-host mirror (encode `.`/`-` as `__`, upper case) |
| `{originalHost}` | Placeholder substituted with the upstream hostname |
| `skip-path` | Strips a prefix from the original path when layouts differ |

Only the `https://` and `file://` schemes are accepted.

> **Credentials:** the resolver honours userinfo in the mirror URL, but a mirror needing
> auth must be supplied as a **binding** of type `dependency-mirror`, never as
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

Use this only when the artifact server cannot reproduce upstream path structure - it must
be regenerated whenever a buildpackage bump (BUILDING.md: Runtime Versions & Dependencies axis 1) changes the dependency set, or
builds break for the versions that moved.

Either form is attached per build through `spec.build.services`, alongside the CA binding.

---

## Open Questions

1. **Artifact server layout** - are pip/npm/go served by one Artifactory/Nexus host on the
   standard `api/pypi`, `api/npm`, `api/go` paths, and are those repos anonymous-read? If
   they require auth, the credential must reach the build pod without landing in the
   world-readable runtimes ConfigMap (a CNB service binding, not env).
2. **Mirror layout** (BUILDING.md: Airgapped Mirror Inventory) - can the artifact server expose the runtime tarballs under
   their upstream paths (enabling a single `BP_DEPENDENCY_MIRROR`), or must per-dependency
   `dependency-mapping` bindings be generated from each `buildpack.toml`? The former
   removes a regeneration step on every buildpackage bump.
3. **Build resource limits** - `spec.build.resources` defaults are unset; large dependency
   trees (node_modules, Go module graphs) may need explicit limits.
4. **Cache retention** (BUILDING.md: Build cache) - kpack overwrites the one `latest` tag each build, so a
   cache repository does not accumulate tags; superseded blobs are the registry's to
   reclaim. Whether the registry's own GC settles this, or the periodic prune has to,
   depends on the registry.

### Resolved

- **`javascript` -> `node` rename** - done. The runtimes list is `python`, `go`, `node`
  across the chart values, `runtimes.py::_DEFAULT_RUNTIMES`, the contract docstring and
  the tests. TypeScript was offered briefly as an alias to the node builder and has been
  withdrawn: it needs the npm registry mirror to fetch the compiler as a devDependency,
  which is not mirrored. A TS app can still be deployed by committing compiled JS, or by
  building under the `node` runtime once `npm_config_registry` is set. Safe without a compatibility alias because function
  creation had never succeeded at the time (`builder.build` raised `NotImplementedError`),
  so no deployed function carries `ANNOTATION_RUNTIME: javascript` for BUILDING.md: Active/Active Behaviour to reconstruct.
  The same fact retires the git-Secret compatibility path: no `{workload}-git` Secret was
  ever written in the earlier Opaque shape, so nothing reads that key any more.
