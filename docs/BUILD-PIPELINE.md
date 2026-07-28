# Build Pipeline - kpack + Cloud Native Buildpacks

How a function's source becomes a running image: the buildpack topology, the charts that
deploy it, and the control flow that keeps it correct under active/active.

> **Status:** Design. Companion to [ARCHITECTURE.md](./ARCHITECTURE.md) - this document
> refines §3.1 (FaaS), §4 (multi-site), §7.2 (customer credentials) and §9 (airgapped)
> for the build path specifically. Where the two disagree, this document wins for build
> concerns and ARCHITECTURE.md wins for everything else.
>
> Replaces the `func`/Tekton placeholder in `api/services/builder.py`.

---

## Table of Contents

1. [Overview & Goals](#1-overview--goals)
2. [Chart Topology](#2-chart-topology)
3. [Buildpack Topology](#3-buildpack-topology)
4. [Runtime Versions & Dependencies](#4-runtime-versions--dependencies)
5. [Trust: CA Injection](#5-trust-ca-injection)
6. [Registry & Git Credentials](#6-registry--git-credentials)
7. [Build Flow](#7-build-flow)
8. [Ownership: API vs Build Service](#8-ownership-api-vs-build-service)
9. [Active/Active Behaviour](#9-activeactive-behaviour)
10. [Function Status Resolution](#10-function-status-resolution)
11. [Lifecycle & Cleanup](#11-lifecycle--cleanup)
12. [RBAC](#12-rbac)
13. [Sample Manifests](#13-sample-manifests)
14. [Airgapped Mirror Inventory](#14-airgapped-mirror-inventory)
15. [Open Questions](#15-open-questions)

---

## Design Decisions (locked in)

| Topic | Decision |
|-------|----------|
| Build engine | **kpack** (Kubernetes-native Cloud Native Buildpacks), not `func`/Tekton |
| kpack install | The `kpack` Helm chart is a **subchart** of the platform chart |
| Cluster-scoped content | `ClusterStack` + `ClusterStore` ship in the **platform chart** (cluster singletons) |
| Namespaced content | `Builder` objects ship in the **serverless-api chart** (workloads namespace) |
| Languages | `go`, `python`, `node`, `typescript` |
| TypeScript | **Alias** to the Node builder - Paketo builds TS with the Node.js buildpack |
| Stack | **One shared** jammy base stack for all languages |
| Build locality | **Local cluster** - each site builds its own image |
| Image CR writer | The **API** (POST / PUT / webhook) |
| Digest propagation | The **build service** watches `status.latestImage` and updates the ksvc in *all* sites |
| Write model | **Full server-side apply** of the desired spec - never a partial patch |
| Rebuild trigger | Webhook sets `spec.source.git.revision` to the **pushed commit SHA** (idempotent) |
| CA trust | **Kyverno mutation** injecting the OpenShift-injected CA bundle into build pods |
| Runtime downloads | **`BP_DEPENDENCY_MIRROR`** redirecting all buildpack dependencies at once, not per-SHA mappings |
| Registry credential | **One** ESO-managed secret: kpack **push** + function **pull** |
| Git credential | Persistent secret on the build ServiceAccount (annotated `kpack.io/git`) |

---

## 1. Overview & Goals

### Goals

- Build a function from git, in-cluster, fully **airgapped** - no egress to public
  registries, PyPI, npmjs or `proxy.golang.org`.
- Offer **four languages** (Go, Python, Node, TypeScript) with a selectable runtime
  version, from mirrored buildpack content.
- **Continuously rebuild** on base-image/buildpack CVE patches without user action -
  this is the reason kpack was chosen over a one-shot builder.
- Stay correct under **active/active** with a floating DNS address: concurrent or
  duplicated writes must never produce duplicate builds.
- Survive **switchover**: a cluster that has never built a given function must be able
  to reconstruct everything it needs from state that is already replicated to it.

### Non-goals (this phase)

- Reproducible/bit-identical builds across clusters (see §9.4).
- Per-tenant builder isolation - builders are shared platform infrastructure.
- Build caching tuned per language (kpack's default registry/volume cache is used as-is).

---

## 2. Chart Topology

Three tiers, split by **cardinality** and **rate of change**:

```
Platform chart                                          once per cluster
├── kpack chart (subchart)  ...... CRDs, controller, webhook, ClusterLifecycle
├── ClusterStack            ...... jammy build + run base images
└── ClusterStore            ...... Paketo buildpackages (go, nodejs, python)

serverless-api chart                                    per release, every site
├── Builder x3              ...... go | python | node   (workloads namespace)
├── runtimes ConfigMap      ...... runtime -> builder + version + build env
├── kpack-builder SA        ...... registry push/pull + git credentials
├── ExternalSecret          ...... the registry dockerconfigjson (§6)
└── (existing: ksvc, Route, NetworkPolicy, CA bundle, ...)

Cluster policy                                          once per cluster
└── Kyverno ClusterPolicy   ...... CA bundle -> build pods (§5)
```

**Why the split.** The kpack chart is a generic, upstream-modelled *engine* installer;
baking Paketo content into it would make it un-reusable and would couple buildpack
version bumps to control-plane upgrades. `ClusterStack`/`ClusterStore` are **cluster
singletons** - two `serverless-api` releases in one cluster would collide on their names,
so they belong one tier up. Namespaced `Builder` objects are safe per release and stay
with the application chart.

**Ordering.** kpack's CRDs are templated (not in a `crds/` directory) so the conversion
webhook can target the release namespace. A `ClusterStack`/`ClusterStore` therefore cannot
be applied until the CRDs are Established *and* the kpack webhook is admitting - enforce
with ArgoCD sync waves: engine -> cluster content -> serverless-api.

---

## 3. Buildpack Topology

```
ClusterStack  (build + run base images)  ┐
ClusterStore  (buildpackages)            ├──► Builder ──► composes and PUSHES a
order         (paketo-buildpacks/<lang>) ┘                builder image to the registry
```

A `Builder` must report `Ready` with a `status.latestImage` before any `Image` referencing
it will build. **This is the first thing to check when a build never starts** - in an
airgapped cluster it usually means the Stack or Store could not pull from the mirror.

### Language mapping

| Runtime | Builder | Buildpack id in `order` |
|---------|---------|-------------------------|
| `go` | `go` | `paketo-buildpacks/go` |
| `python` | `python` | `paketo-buildpacks/python` |
| `node` | `node` | `paketo-buildpacks/nodejs` |
| `typescript` | `node` | `paketo-buildpacks/nodejs` |

**TypeScript is not a separate buildpack.** Paketo builds TS through the Node.js
buildpack via the project's build script. Exposing it as a distinct *runtime* that resolves
to the *same* builder gives users the four choices they expect without a duplicate builder
image to compose, push and patch.

One shared `ClusterStack` (jammy base) serves all three builders. Per-language stacks
(e.g. Go on `tiny`/`static` for smaller images) are a later optimisation.

---

## 4. Runtime Versions & Dependencies

Three independent axes. Conflating them is the most common source of confusion:

| Axis | What it pins | Where it is set |
|------|--------------|-----------------|
| 1. Buildpack content | The mirrored Paketo image tags | `build.stack.*.version`, `build.store.sources[].version` |
| 2. Language runtime | CPython / Node / Go version | `BP_*_VERSION` build env |
| 3. App dependencies | pip / npm / go modules | package-manager env pointing at the on-prem artifact server |

### Axis 2 - runtime version

| Runtime | Env var |
|---------|---------|
| python | `BP_CPYTHON_VERSION` |
| go | `BP_GO_VERSION` |
| node / typescript | `BP_NODE_VERSION` |

> Selecting a version only *asks* for it - the buildpack still has to fetch that runtime
> from the internet. Offline, this axis works only once the download is redirected to the
> mirror (§14.3, §14.4).

### Axis 3 - application packages (airgapped)

The package managers run **inside the build pod** and cannot reach the internet. They are
pointed at the on-prem artifact server:

| Runtime | Env |
|---------|-----|
| python | `PIP_INDEX_URL` (+ `PIP_EXTRA_INDEX_URL`) |
| node / typescript | `npm_config_registry` |
| go | `GOPROXY`, `GOSUMDB=off` (or vendored deps with `GOFLAGS=-mod=vendor`) |

> Do **not** use `PIP_TRUSTED_HOST`, `npm strict-ssl=false`, `GOINSECURE` or
> `NODE_TLS_REJECT_UNAUTHORIZED=0`. TLS verification stays on; trust comes from the CA
> injected in §5.

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
  - name: typescript
    builder: node                      # alias
    versionEnv: BP_NODE_VERSION
    defaultVersion: "20"
    versions: ["18", "20", "22"]
    buildEnv:
      - { name: npm_config_registry, value: "https://artifactory.internal/artifactory/api/npm/npm/" }
```

**Coupling warning.** Axis 2 is bounded by axis 1: a pinned buildpackage only *contains*
certain interpreter versions, and in an airgapped cluster there is no fallback download.
Whenever `build.store.sources[].version` is bumped, re-check that every advertised
`runtimes[].versions` entry is still available, or builds will fail at `detect`/`build`.

---

## 5. Trust: CA Injection

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
> runs as init containers (§7); `completion` is the only main container. A policy that
> patches `spec.containers` alone silently does nothing to the phases that clone source and
> run package managers.

Because the OpenShift bundle is the **complete** trust store (system CAs + internal CA),
mounting it at a path and exporting `SSL_CERT_FILE` / `NODE_EXTRA_CA_CERTS` / `PIP_CERT` /
`GIT_SSL_CAINFO` is preferred over overwriting `/etc/ssl/certs/ca-certificates.crt`.

**Operational risk:** Kyverno becomes a hard dependency of the build path, and a missing
policy fails *late and confusingly* (a TLS error deep inside pip). Cover it with a smoke
test that builds a function pulling one internal dependency.

---

## 6. Registry & Git Credentials

### One registry secret, two roles

A **single** ESO-managed `kubernetes.io/dockerconfigjson` secret serves both ends of the
image's life:

```
ExternalSecret ──► Secret (dockerconfigjson)
                     ├──► kpack-builder SA          → kpack PUSHES the built image
                     └──► ksvc imagePullSecrets      → Knative PULLS it to run
```

This is deliberate: the image is pushed to and pulled from the same internal registry, so
splitting the credential would mean maintaining two secrets with identical contents.

**kpack reads the two SA fields differently** - put the secret in both:

| SA field | Used for |
|----------|----------|
| `secrets:` | Registry auth for **push** (`spec.tag`) and pulling stack/store images |
| `imagePullSecrets:` | The build **pod** pulling the composed builder image |

### Git credential

Per §7.2, the customer's git token is persisted (it is needed for rebuilds the user did not
initiate - CVE patches, webhooks). kpack authenticates git through a secret on the same
ServiceAccount, annotated with the git host:

```yaml
metadata:
  annotations:
    kpack.io/git: https://git.internal
```

**Scope note:** the build SA is shared platform infrastructure, so any function build can
clone anything its git credential can reach. Scope the token read-only and to the narrowest
project set that is workable.

---

## 7. Build Flow

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
event-driven (§8), not only triggered by API writes.

---

## 8. Ownership: API vs Build Service

Two components, split by execution model:

| Component | Path | Responsibility |
|-----------|------|----------------|
| **API** | request/response | On POST / PUT / webhook: compose the desired `Image` and server-side apply it to the **local** cluster. Returns `202`. |
| **Build service** | control loop | Watches `Image.status.latestImage` in the local cluster. On change, applies the ksvc with the new **digest** to **all** sites. |

The watch loop does not fit a request/response API, and the shared library already
anticipates this split (`common/cluster.py`: *"the API and a future builder service both
reach a cluster the same way"*; `common/labels.py`: *"a future builder service stamps them
on its build resources"*).

**Contract change.** `FunctionService.create` currently calls `builder.build(...)`
synchronously and applies the ksvc with the returned digest. Under this model a build takes
minutes, so create/update become **asynchronous**: apply the `Image`, return `202`, and let
the build service deploy when `latestImage` appears. "Created" no longer implies "serving".

---

## 9. Active/Active Behaviour

### 9.1 Builds are local

Each site builds in its **own** cluster, so the full build stack (kpack, Stack, Store,
Builders) is installed in **every** cluster. The `Image` CR exists only in the cluster that
built it.

### 9.2 Every write path is a full server-side apply

`Cluster.apply()` already uses `apply=True, force_conflicts=True`; **server-side apply is
create-or-update by construction**. Every path therefore composes the *complete* desired
`Image` and applies it:

| Path | Behaviour |
|------|-----------|
| POST | compose -> apply -> creates |
| PUT | compose -> apply -> **creates if missing**, else updates |
| webhook | reconstruct (§9.3) + `revision` = pushed SHA -> apply -> **creates if missing** |

> **Never use a targeted patch** (e.g. patching only `spec.source.git.revision`). It
> returns 404 when the object is absent - precisely the post-switchover case this design
> must survive.

### 9.3 Reconstruction after switchover

A cluster that never built a function can still compose its `Image`, because the inputs are
already replicated to every site:

| Input | Source |
|-------|--------|
| runtime | ksvc annotation `ANNOTATION_RUNTIME` |
| git url | ksvc annotation `ANNOTATION_GIT_URL` |
| branch | ksvc annotation `ANNOTATION_GIT_BRANCH` |
| builder, version env, build env | runtimes ConfigMap |
| git token | the persisted git secret |
| registry credential | the ESO-managed secret (§6) |

No database and no cross-cluster state replication is required - the Knative Service is the
replicated source of truth.

### 9.4 Convergence rules

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

### 9.5 Accepted consequences

- **A post-switchover write rebuilds.** The new cluster has no `Image`, so the first
  PUT/webhook builds from scratch. Builds are not bit-reproducible, so the digest differs
  from the previous cluster's and a new Knative revision rolls out even when the source is
  unchanged. It is bounded to functions actually touched after switchover.
- **Orphaned Images keep building.** The previously-active cluster still holds `Image`
  objects and will keep firing `STACK`/`BUILDPACK` rebuilds, pushing digests nobody
  deploys. ksvcs are digest-pinned so nothing breaks, but build capacity is wasted and the
  mutable tag drifts to an undeployed digest. See §11.

---

## 10. Function Status Resolution

`GET` on a function resolves status **build-first, deployment-second**:

```
GET /functions/{name}
  1. look up the Image / latest Build in the LOCAL cluster
       building  -> return "building"  (+ build reason, phase, started-at)
       failed    -> return "build failed" (+ failure detail, log pointer)
  2. no Image found, or the build succeeded
       -> fall through to the Knative Service status (existing behaviour)
```

Two properties this gives us:

- A function whose first build is still running reports **building** rather than a
  confusing "not ready" ksvc state.
- After a switchover the local cluster has **no** `Image`, so step 1 finds nothing and the
  handler falls through to the ksvc - correctly reporting the function as **serving**, since
  it is still running the previously-built digest. The absence of a build is not an error.

Build detail is read from the local cluster only; there is no cross-site aggregation on this
path (the ksvc fan-out in ARCHITECTURE.md §4 is unchanged).

---

## 11. Lifecycle & Cleanup

| Event | Action |
|-------|--------|
| Function delete | Delete the `Image` in **all** sites, not just the local one - otherwise a leftover keeps rebuilding forever. |
| Switchover | Orphaned `Image` objects remain in the previously-active cluster (§9.5). |
| Periodic prune | A reconcile pass deletes `Image` objects in non-local clusters, selected by the existing `LABEL_MANAGED_BY` / `LABEL_WORKLOAD` labels. |

Build history is bounded per `Image` by `spec.successBuildHistoryLimit` /
`spec.failedBuildHistoryLimit`; kpack garbage-collects older `Build` objects and their pods.

---

## 12. RBAC

The API and build service identities (per ARCHITECTURE.md §6.3, the cert CN user) need, in
the workloads namespace of every cluster:

| Resource | Verbs | Used by |
|----------|-------|---------|
| `images.kpack.io` | get, list, watch, create, update, patch, delete | API (write), build service (watch) |
| `builds.kpack.io` | get, list, watch | status resolution (§10), log lookup |
| `pods`, `pods/log` | get, list | per-phase build logs (§7) |

`Builder`, `ClusterStack` and `ClusterStore` are managed by Helm/ArgoCD, not by the
services - no runtime write permission on them.

---

## 13. Sample Manifests

### 13.1 Image (created by the API, local cluster)

```yaml
apiVersion: kpack.io/v1alpha2
kind: Image
metadata:
  name: fn-hello-payments              # deterministic: fn-{name}-{group}
  namespace: serverless-workloads
  labels:                              # common/labels.py
    serverless.platform/managed-by: serverless-api
    serverless.platform/workload: hello-payments
spec:
  tag: registry.internal/serverless/functions/hello-payments
  builder:
    kind: Builder
    name: python
  serviceAccountName: kpack-builder
  source:
    git:
      url: https://git.internal/payments/hello.git
      revision: 9f2c1ab…               # pushed SHA (webhook) or branch
  build:
    env:
      - { name: BP_CPYTHON_VERSION, value: "3.12" }
      - { name: PIP_INDEX_URL, value: "https://artifactory.internal/artifactory/api/pypi/pypi/simple" }
    # NOTE: never set creationTime here - see §9.4
```

### 13.2 Builder (serverless-api chart, per site)

```yaml
apiVersion: kpack.io/v1alpha2
kind: Builder
metadata:
  name: python
  namespace: serverless-workloads
spec:
  serviceAccountName: kpack-builder
  tag: registry.internal/serverless/builders/python
  stack: { name: serverless-base, kind: ClusterStack }
  store: { name: serverless-store, kind: ClusterStore }
  order:
    - group:
        - id: paketo-buildpacks/python
```

### 13.3 ClusterStack + ClusterStore (platform chart, per cluster)

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
credential when the internal registry requires auth.

### 13.4 Build ServiceAccount (registry push/pull + git)

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: kpack-builder
  namespace: serverless-workloads
secrets:                       # registry auth for push + stack/store pulls
  - name: serverless-registry-creds
  - name: serverless-git-creds # annotated kpack.io/git
imagePullSecrets:              # build pod pulling the composed builder image
  - name: serverless-registry-creds
```

### 13.5 Kyverno policy - CA into build pods

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
            # BOTH lists - the lifecycle runs as init containers (§5, §7)
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

## 14. Airgapped Mirror Inventory

Three **distinct** classes of artefact must be mirrored. Mirroring only the first two is
the most common airgapped failure, and it fails late - at the `build` phase of the first
real build, not at install time.

### 14.1 Container images - kpack platform

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

### 14.2 Container images - Paketo content

| Image | Used by |
|-------|---------|
| `paketobuildpacks/build-jammy-base` | `ClusterStack.spec.buildImage` |
| `paketobuildpacks/run-jammy-base` | `ClusterStack.spec.runImage` (and the running function) |
| `paketobuildpacks/go` | `ClusterStore` |
| `paketobuildpacks/nodejs` | `ClusterStore` |
| `paketobuildpacks/python` | `ClusterStore` |

Plus the **composed builder images** this platform *produces* - they are pushed to
`registry.internal/serverless/builders/<lang>` by the `Builder` objects, so that repository
must exist and be writable by the build ServiceAccount.

### 14.3 Runtime distributions - **not images**

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

In an airgapped cluster that fetch fails, so `BP_CPYTHON_VERSION` (§4 axis 2) cannot be
satisfied by the image alone. The tarballs for every advertised
`runtimes[].versions` entry must be mirrored **to the artifact server** (they are files,
not registry content):

| Runtime | Upstream source to mirror |
|---------|---------------------------|
| python | `https://www.python.org/ftp/python/<ver>/Python-<ver>.tgz` |
| node | `https://nodejs.org/dist/v<ver>/node-v<ver>-linux-x64.tar.gz` |
| go | `https://go.dev/dl/go<ver>.linux-amd64.tar.gz` |

The authoritative list is the `uri` + `checksum` fields in each buildpackage's
`buildpack.toml` (readable with `pack buildpack inspect <image>`), including the
sub-buildpacks (`cpython`, `pip`, `poetry`, `node-engine`, `npm-install`, ...).

### 14.4 Redirecting the download - `dependency-mirror`

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
be regenerated whenever a buildpackage bump (§4 axis 1) changes the dependency set, or
builds break for the versions that moved.

Either form is attached per build through `spec.build.services`, alongside the CA binding.

---

## 15. Open Questions

1. **Artifact server layout** - are pip/npm/go served by one Artifactory/Nexus host on the
   standard `api/pypi`, `api/npm`, `api/go` paths, and are those repos anonymous-read? If
   they require auth, the credential must reach the build pod without landing in the
   world-readable runtimes ConfigMap (a CNB service binding, not env).
2. **Mirror layout** (§14.4) - can the artifact server expose the runtime tarballs under
   their upstream paths (enabling a single `BP_DEPENDENCY_MIRROR`), or must per-dependency
   `dependency-mapping` bindings be generated from each `buildpack.toml`? The former
   removes a regeneration step on every buildpackage bump.
3. **Build service packaging** - separate Deployment in this chart, or a second container
   in the API pod? A watch loop and an HTTP API have different scaling and restart
   characteristics. *Default if undecided: separate Deployment, single replica.*
4. **Prune cadence** (§11) - periodic reconcile, or triggered explicitly on switchover?
   *Default if undecided: periodic.*
5. **Build resource limits** - `spec.build.resources` defaults are unset; large dependency
   trees (node_modules, Go module graphs) may need explicit limits and a bigger cache.

### Resolved

- **`javascript` -> `node` rename** - done. The runtimes list is now `python`, `go`,
  `node`, `typescript` across the chart values, `runtimes.py::_DEFAULT_RUNTIMES`, the
  contract docstring and the tests. Safe without a compatibility alias because function
  creation has never succeeded (`builder.build` raises `NotImplementedError`), so no
  deployed function carries `ANNOTATION_RUNTIME: javascript` for §9.3 to reconstruct.
