# Building - kpack + Cloud Native Buildpacks

How source becomes an image: buildpack topology, runtime versions, credentials,
the build flow, and what the API owns versus the build service.

## Contents

- [Design Decisions (locked in)](#design-decisions-locked-in)
- [Overview & Goals](#overview--goals)
- [Buildpack Topology](#buildpack-topology)
- [Runtime Versions & Dependencies](#runtime-versions--dependencies)
- [Trust: CA Injection](#trust-ca-injection)
- [Registry & Git Credentials](#registry--git-credentials)
- [Build Flow](#build-flow)
- [Ownership: API vs Build Service](#ownership-api-vs-build-service)
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
| Image CR writer | The **API** (POST / PUT / webhook) |
| Digest propagation | The **build service** watches `status.latestImage` and updates the ksvc in *all* sites |
| Write model | **Full server-side apply** of the desired spec - never a partial patch |
| Rebuild trigger | Webhook sets `spec.source.git.revision` to the **pushed commit SHA** (idempotent) |
| CA trust | **Kyverno mutation** injecting the OpenShift-injected CA bundle into build pods |
| Runtime downloads | **`BP_DEPENDENCY_MIRROR`** redirecting all buildpack dependencies at once, not per-SHA mappings |
| Registry credential | **One** ESO-managed secret: kpack **push** + function **pull** |
| Build cache | **Registry**, at `{base}/{group}/{name}_cache` - not kpack's default PVC per `Image`, which scales with the function count |
| Registry layout | `registry.url` + optional `registry.organization`, prefixing every image the chart and the API reference (BUILDING.md: Runtime Versions & Dependencies) |
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
`api/services/runtimes.py`, whose `RuntimeSpec` is `extra="allow"` - so these fields flow
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
the repository. `registry.organization` supplies it, and everything derives from the
pair:

```
{registry.url}/{registry.organization}/...          <- the "registry base"

  base/{build.builderRepository}/{name}             Builder tags
  base/{group}/{name}:{branch}                      function images (the API)
  base/{group}/{name}_cache:latest                  build layer cache (BUILDING.md: Build cache)
```

Empty organization collapses this to the flat `{host}/{repository}` layout, so existing
installs are unaffected.

The stack and store images sit under the same base, but the kpack chart prefixes them
with its own `clusterBuild.registry` - set that to the identical
`{registry.url}/{registry.organization}` string:

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
| `BUILDPACK` | a Store buildpackage was updated | ops bumps buildpack content |
| `STACK` | the Stack run image was updated | **CVE patch** - often a fast *rebase* |

The last two fire with **no user action**. Digest propagation must therefore be
event-driven (BUILDING.md: Ownership: API vs Build Service), not only triggered by API writes.

---

## Ownership: API vs Build Service

Two components, split by execution model:

| Component | Path | Responsibility |
|-----------|------|----------------|
| **API** | request/response | On POST / PUT / webhook: compose the desired `Image` and server-side apply it to the **local** cluster. Returns `202`. |
| **Build service** | control loop | Watches `Image.status.latestImage` in the local cluster. On change, applies the ksvc with the new **digest** to **all** sites. |

The watch loop does not fit a request/response API, and the shared library already
anticipates this split (`common/cluster.py`: *"the API and a future builder service both
reach a cluster the same way"*; `common/labels.py`: *"a future builder service stamps them
on its build resources"*).

**Contract change - implemented.** `Builder.build` no longer returns a finished image. It
records desired state (git Secret -> ServiceAccount -> `Image`, in dependency order) and
returns the deterministic tag the build will push to; the ksvc is applied against that tag
immediately, and `GET` reports `Building` until kpack finishes (FUNCTIONS.md: Function Status Resolution). "Created" no longer
implies "serving", which is why the status code for `Building` is `202`.

The manifests are **owned resources of the KSVC**, applied in the same pass as the
function's env Secret and DomainMapping and carrying the same `ownerReference`. That is
what deletes them with the function (BUILDING.md: Lifecycle & Cleanup) - there is no cleanup code, because there is
nothing to clean up.

`Builder.plan` splits them by how far each piece travels, and the split is load-bearing:

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
self-healing (BUILDING.md: Active/Active Behaviour). An update that changes nothing therefore keeps the deployed image
exactly as it is: that image may be a digest a finished build resolved, and rewriting it
back to the tag would spawn a pointless revision.

Still to come, and deliberately out of scope for the current implementation: the build
service's watch loop, and the per-function webhook endpoint that pins a pushed SHA to
`spec.source.git.revision` (`BuildRequest.revision` already carries it).

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
| PUT | compose -> apply -> **creates if missing**, else updates |
| webhook | reconstruct (BUILDING.md: Active/Active Behaviour) + `revision` = pushed SHA -> apply -> **creates if missing** |

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

### Accepted consequences

- **A post-switchover write rebuilds.** The new cluster has no `Image`, so the first
  PUT/webhook builds from scratch. Builds are not bit-reproducible, so the digest differs
  from the previous cluster's and a new Knative revision rolls out even when the source is
  unchanged. It is bounded to functions actually touched after switchover.
- **Orphaned Images keep building.** The previously-active cluster still holds `Image`
  objects and will keep firing `STACK`/`BUILDPACK` rebuilds, pushing digests nobody
  deploys. ksvcs are digest-pinned so nothing breaks, but build capacity is wasted and the
  mutable tag drifts to an undeployed digest. See BUILDING.md: Lifecycle & Cleanup.

---

## Lifecycle & Cleanup

| Event | Action |
|-------|--------|
| Function delete | Nothing to do *in the cluster*: the `Image` and build `ServiceAccount` are owned by the KSVC, so deleting it garbage-collects them. Co-location is what buys this - ownerReferences cannot cross namespaces (DEPLOYING.md: Chart Topology). |
| Function delete (registry) | The function's image repository and its `{name}_cache` repository (BUILDING.md: Build cache) both outlive the KSVC - nothing in the cluster owns registry content. Both are named deterministically from `{group}/{name}`, so registry retention can select them together. |
| Switchover | Orphaned `Image` objects remain in the previously-active cluster (BUILDING.md: Active/Active Behaviour). |
| Periodic prune | A reconcile pass deletes `Image` objects in non-local clusters, selected by the existing `LABEL_MANAGED_BY` / `LABEL_WORKLOAD` labels. |

Build history is bounded per `Image` by `spec.successBuildHistoryLimit` /
`spec.failedBuildHistoryLimit`; kpack garbage-collects older `Build` objects and their pods.

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
  tag: registry.internal/<org>/payments/hello       # {base}/{group}/{name} (BUILDING.md: Runtime Versions & Dependencies)
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
      tag: registry.internal/<org>/payments/hello_cache:latest
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
                  - { name: PIP_CERT,             value: /etc/serverless/ca/ca-bundle.crt }
                  - { name: NODE_EXTRA_CA_CERTS,  value: /etc/serverless/ca/ca-bundle.crt }
            containers:
              - (name): "*"
                volumeMounts:
                  - { name: internal-ca, mountPath: /etc/serverless/ca, readOnly: true }
```

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
`{registry base}/serverless/builders/<lang>` by the `Builder` objects, so that repository
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
3. **Build service packaging** - separate Deployment in this chart, or a second container
   in the API pod? A watch loop and an HTTP API have different scaling and restart
   characteristics. *Default if undecided: separate Deployment, single replica.*
4. **Prune cadence** (BUILDING.md: Lifecycle & Cleanup) - periodic reconcile, or triggered explicitly on switchover?
   *Default if undecided: periodic.*
5. **Build resource limits** - `spec.build.resources` defaults are unset; large dependency
   trees (node_modules, Go module graphs) may need explicit limits.
6. **Cache retention** (BUILDING.md: Build cache) - kpack overwrites the one `latest` tag each build, so a
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
