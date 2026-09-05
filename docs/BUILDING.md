# Building - kpack + Cloud Native Buildpacks

How a function's source becomes an image: the buildpack topology, the build flow, the
credentials a build needs, what the API owns versus the build controller, and what happens
under active/active. Runtime versions, the registry layout and the airgap mirror are in
RUNTIMES.md; the controller that publishes built digests is in BUILD-CONTROLLER.md.

## Contents

- [Design Decisions (locked in)](#design-decisions-locked-in)
- [Overview & Goals](#overview--goals)
- [Buildpack Topology](#buildpack-topology)
- [Build Flow](#build-flow)
- [Trust: CA Injection](#trust-ca-injection)
- [Registry & Git Credentials](#registry--git-credentials)
- [Ownership: API vs Build Service](#ownership-api-vs-build-service)
- [Active/Active Behaviour](#activeactive-behaviour)
- [Lifecycle & Cleanup](#lifecycle--cleanup)
- [Sample Manifests](#sample-manifests)
- [Open Questions](#open-questions)

## Design Decisions (locked in)

| Topic | Decision |
|-------|----------|
| Build engine | **kpack** (Kubernetes-native Cloud Native Buildpacks) |
| kpack install | The `kpack` Helm chart is a **subchart** of the platform chart |
| Buildpack content | `ClusterStack` and `ClusterStore` ship in the **kpack chart**; the per-runtime `ClusterBuilder`s ship in the **serverless-api chart**. All three kinds are cluster-scoped, so the engine release owns the cluster singletons and the serverless-api chart references them by name |
| Languages | `go`, `python`, `node`, on **one shared** jammy base stack |
| Build locality | **Build where you run** - every region builds its own copy, into its own registry |
| Build namespace | The workload's **own** namespace, `{group}{suffix}` (DEPLOYING.md: Chart Topology) |
| Image CR writer | The **API**, on POST / PUT / `POST .../build`, and on a git push through the same build endpoint (FUNCTIONS.md: Git webhook) |
| Write model | **Full server-side apply** of the desired spec, never a partial patch |
| Rebuild trigger | `POST .../build` annotates the **latest `Build`**, never the `Image` |
| CA trust | **Kyverno mutation** injecting the OpenShift-injected CA bundle into build pods |
| Git credential | **Per function** - caller-supplied, on a per-function ServiceAccount the API creates; never platform-wide |
| Registry credential | **One** ESO-managed secret per region for kpack push and function pull, plus a pull-only secret for the kpack registry |
| Registry cleanup | Function delete **deletes both repositories in every region's registry** through Quay's management API |

Decided elsewhere: the registry layout, the registry build cache and the runtime download
mirror (RUNTIMES.md); digest propagation and registry tag GC (BUILD-CONTROLLER.md).

## Overview & Goals

A build turns a git revision into a runnable OCI image. The API writes one kpack `Image` per
region; kpack resolves the source, runs the buildpack lifecycle in a pod, and pushes the result
to that region's registry. The build controller then rolls the new digest out to the Knative
Service.

### Goals

- Build from git, in-cluster, fully **airgapped** - no egress to public registries, PyPI, npmjs
  or `proxy.golang.org`.
- Offer **three languages** (Go, Python, Node) with a selectable runtime version, from mirrored
  buildpack content.
- **Continuously rebuild** on base-image and buildpack CVE patches without user action. This is
  why kpack, and not a one-shot builder.
- Stay correct under **active/active** with a floating DNS address: concurrent or duplicated
  writes must never produce duplicate builds.
- Survive **switchover**: a cluster that has never built a function must be able to reconstruct
  everything it needs from state already replicated to it.

### Non-goals (this phase)

- Reproducible, bit-identical builds across clusters (BUILDING.md: Active/Active Behaviour).
- Per-tenant builder isolation. Builders are shared platform infrastructure.
- Build caching tuned per language. *Where* the cache lives is settled (RUNTIMES.md: Build
  cache); its size and hit rate are not.

## Buildpack Topology

```
ClusterStack  (build + run base images)  ┐
ClusterStore  (buildpackages)            ├──► ClusterBuilder ──► composes and PUSHES a
order         (explicit components)      ┘                       builder image to the registry
```

A `ClusterBuilder` must report `Ready` with a `status.latestImage` before any `Image`
referencing it will build. **This is the first thing to check when a build never starts.**
In an airgapped cluster it usually means the Stack or Store could not pull from the mirror.

The builders are **cluster-scoped**, one per runtime, because an `Image` resolves a namespaced
`Builder` only in its own namespace and every function's `Image` lives in its group's namespace
(DEPLOYING.md: Chart Topology). Their names are cluster-scoped with them, so one release per
cluster: two would fight over the same three names.

### Language mapping

| Runtime | ClusterBuilder | Detection groups (supported paths) |
|---------|----------------|-----------------------------------|
| `go` | `go` | vendored (`go-mod-vendor`), non-vendored (`go mod download`) |
| `python` | `python` | `requirements.txt` (pip), `pyproject.toml` (poetry x2) |
| `node` | `node` | npm (`npm-install`) |

Orders name **component** buildpacks explicitly rather than the language composites, so the
platform supports exactly the paths it mirrors. yarn, pipenv and conda groups are omitted: an
app on one of those fails at `detect` with "no group passed" instead of failing deep in a build
on a dependency that was never mirrored. Narrowing shrinks the dependency mirror, not the image
mirror (RUNTIMES.md: Airgapped Mirror Inventory), because only buildpacks that can run ever
download.

**TypeScript is not offered.** Paketo has no TypeScript buildpack, so TS builds through the
Node.js buildpack, which runs the project's build script and needs the `typescript` compiler
from npm as a devDependency; without that mirrored the build fails at `npm install`.
`node-run-script` stays in the node order, so a TS app becomes buildable as soon as
`npm_config_registry` points at a mirror carrying its devDependencies - no chart change beyond
re-adding the runtime entry.

One shared `ClusterStack` (jammy base) serves all three builders. Per-language stacks - Go on
`tiny` or `static` for smaller images - are a later optimisation.

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

**The `Image` takes the workload's object name verbatim.** kpack stamps that name onto every
`Build` as the `image.kpack.io/image` label value, which caps at 63 characters - the limit
already enforced on a workload name. `common.kpack.build_image_name` stays a function even
though it is the identity, so applying an `Image` and deleting one cannot disagree. The build
`ServiceAccount` is the one exception, suffixed `{workload}-build`; it is never written where a
label value has to fit.

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

Because each phase is a named container, `Cluster.pod_logs(pod, container=...)` yields
**per-phase build logs** - the difference between "build failed" and an actionable error.

### What causes a new Build

| Reason | Trigger | In this platform |
|--------|---------|------------------|
| `CONFIG` | `spec` changed | PUT that changes runtime, version, revision, path or env |
| `COMMIT` | resolved source SHA changed | kpack's `SourceResolver` re-resolving the revision, when it names a branch. Also the per-function webhook, which pins the pushed SHA so a push builds at once rather than at the next poll (FUNCTIONS.md: Git webhook) |
| `TRIGGER` | the latest `Build` carries `image.kpack.io/additionalBuildNeeded` | `POST .../functions/{name}/build` |
| `BUILDPACK` | a Store buildpackage was updated | ops bumps buildpack content |
| `STACK` | the Stack run image was updated | **CVE patch**, often a fast *rebase* |

`BUILDPACK` and `STACK` fire with **no user action**, which is why digest propagation is
event-driven rather than triggered by API writes (BUILD-CONTROLLER.md: Digest propagation).

`TRIGGER` is the one reason that is imperative rather than a state change, which is why the
explicit rebuild exists: nothing about the function changed, so there is no desired state to
write. kpack looks for the annotation on the **latest `Build`**, not on the `Image`, and the
next `Build` inherits only the *Image's* annotations - so one request produces exactly one
build and no loop.

> **Never put the trigger on the `Image`.** It is a nonce, and the `Image` spec must stay a
> pure function of the function definition (BUILDING.md: Convergence rules).

### Build pod resources

A build is far heavier than the function it produces - a dependency resolve plus a compile -
and it draws on its namespace's quota (DEPLOYING.md: Chart Topology). `build.resources` sets
`Image.spec.build.resources`. Unset, the build pod is BestEffort and is the first thing evicted
under node pressure. **One bound covers every build:** the variance that matters is between a
small function and a large one, not between Go and Node.

## Trust: CA Injection

Internal TLS - git, the registry, the artifact server - is signed by the internal CA. The
build pod must trust it, and **verification is never disabled**.

**Mechanism: a Kyverno `ClusterPolicy`** that mutates kpack build pods, mounting the existing
OpenShift-injected `ca-bundle` ConfigMap (created in each group's namespace by the tenant
template set) and setting the per-tool CA env vars. It is preferred over a CNB
`ca-certificates` service binding, which affects only the **build** phase and not `prepare`,
where kpack clones from git; the OpenShift bundle also rotates on its own, so there is no
ExternalSecret and no `ca-certificates` entry in every builder `order`.

> **The policy must mutate `initContainers`, not just `containers`.** The kpack lifecycle runs
> as init containers; `completion` is the only main container. A policy that patches
> `spec.containers` alone silently does nothing to the phases that clone source and run
> package managers.

The OpenShift bundle is the **complete** trust store - system CAs plus the internal CA - so
mount it at a path and export `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `PIP_CERT` and
`GIT_SSL_CAINFO` rather than overwriting `/etc/ssl/certs/ca-certificates.crt`. pip needs three
of its own (RUNTIMES.md: Why pip needs three variables of its own).

**Kyverno is a hard dependency of the build path.** The policy ships with
`failurePolicy: Fail` (`build.caInjection.failurePolicy`): a build pod is *rejected* if Kyverno
cannot mutate it, rather than starting without the CA and dying later with an opaque TLS error
from pip, npm or git. Kyverno being down then blocks builds, which is acceptable because builds
are asynchronous and retried. Set it to `Ignore` only if you would rather builds proceed
unmutated.

Cover the path with a smoke test that builds a function pulling one internal dependency. That
is the only thing that proves the mount reached the phase that needed it.

If Kyverno is not available, set `build.caInjection.enabled: false` and bake the CA into the
mirrored stack images instead (`update-ca-certificates` at mirror time). That also covers the
run image, so the running function trusts internal TLS too.

## Registry & Git Credentials

### Two registries, three credentials

A build reads two registries: it pushes to the region's own, and pulls the stack, the store
and (at `export`) the run image from the shared kpack registry. Docker auth is keyed by
**host**, so that is two dockerconfigjson secrets, plus the function's git token.

| Secret | Content | Same in every region? |
|---|---|---|
| `serverless-registry-creds` | **this region's** registry: push + pull | Name yes, contents no |
| `kpack-registry-creds` | the shared kpack registry, pull only | Yes, both |
| `{workload}-git` | that function's token | Per function, on every region it runs in |

The region credential serves both ends of the image's life: ESO syncs one dockerconfigjson
Secret, the build ServiceAccounts push the built image with it, and the ksvc pulls the image to
run with it as an `imagePullSecrets` entry.

**The name must stay identical in every region.** It is written into every region's KSVC
`imagePullSecrets` and onto every per-function build ServiceAccount, and the API emits those
for all regions from one place. Only the *contents* differ, so only the Vault path is per
region (`.../serverless/{{ .Values.global.region }}`).

`build.kpackRegistry.url` empty means the kpack registry **is** the region registry - the
single-registry install. No second Secret is created and nothing is added to the build
accounts.

**kpack reads the two ServiceAccount fields differently.** Put both credentials in both:

| SA field | Used for |
|----------|----------|
| `secrets:` | Registry auth for **push** (`spec.tag`) and pulling stack/store images |
| `imagePullSecrets:` | The build **pod** pulling the composed builder image |

### Git credential - per function, never shared

**The git token belongs to the function, not the platform.** The caller supplies it on create
and the API persists it as `{workload}-git`, because rebuilds happen without the caller -
CVE patches, webhooks.

kpack resolves git credentials from the ServiceAccount named by the `Image`, matching secrets
by host annotation. A single shared account would hand **one tenant's token to another
tenant's build**, so there is no platform-wide git credential anywhere in this design.

There are **two kinds of ServiceAccount**:

| Account | Created by | Holds | Used by |
|---------|-----------|-------|---------|
| `kpack-builder` | the chart, in the API namespace | both registry credentials, no git one | `ClusterBuilder` objects (compose and push a builder image; never clone source) |
| `{workload}-build` | the **API**, per function, in the workload's namespace | that function's git Secret **+** both registry credentials | the function's `Image` |

The chart creates `kpack-builder` **in the API's namespace**, along with both registry
ExternalSecrets: a `ClusterBuilder` is cluster-scoped and names that account through
`spec.serviceAccountRef`. Function builds run in the group's namespace, where the tenant
template set has already placed this region's registry credential. The per-function account is
created alongside the function and named on its `Image`:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: hello-build
  namespace: payments-serverless        # with the Image and the KSVC (DEPLOYING.md: Chart Topology)
secrets:
  - name: serverless-registry-creds     # this region's, from the tenant template set
  - name: kpack-registry-creds          # the run image `export` pulls
  - name: hello-git                     # this function's token, from the API
imagePullSecrets:
  - name: serverless-registry-creds
```

The chart passes both Secret names as `SERVERLESS_BUILD__REGISTRY_SECRET` and
`SERVERLESS_BUILD__KPACK_REGISTRY_SECRET` (the second only when the kpack registry is a
separate host) and grants the API `serviceaccounts` write (DEPLOYING.md: RBAC).

The account and the git Secret it names must sit in the **same namespace as the `Image`**:
kpack resolves a build's credentials from the ServiceAccount named on the Image, in the Image's
own namespace, and looks nowhere else. All three are in the workload's own namespace, which is
what makes one git Secret enough.

> **One Secret, two readers.** `{workload}-git` is `kubernetes.io/basic-auth`
> (`username` + `password`) annotated `kpack.io/git: <scheme>://<host>`. kpack clones with it
> and reads no other shape; the API reads the password back so a later edit rebuilds without
> the client re-sending the token. `FunctionOffering.read_extra_state` pulls the `password`
> key through `region_read.secret_text`, the same call that reads a workload's env values.

## Ownership: API vs Build Service

Two components, split by execution model:

| Component | Path | Responsibility |
|-----------|------|----------------|
| **API** | request/response | On POST / PUT / `POST .../build`: compose the desired `Image` and server-side apply it to the **local** cluster. Returns `202`. |
| **Build controller** | control loop | Watches `Image.status.latestImage` in the local cluster. On change, applies the ksvc with the new **digest** (BUILD-CONTROLLER.md: Digest propagation). |

A watch loop does not fit a request/response API, and the shared library (`common/cluster.py`,
`common/labels.py`) is written for both services.

The ksvc image field follows the same split. The API writes it **once, on create, per region**;
a PUT keeps whatever each region is already running; `POST .../build` writes no ksvc at all.
After the create the build controller is the only writer, and only ever of the digest
(BUILD-CONTROLLER.md: Who writes the ksvc image).

**The API's contract is declarative.** `BuildBackend.plan` returns no finished image and
touches no cluster. It returns the manifests recording desired state - git Secret, build
ServiceAccount, `Image`, in dependency order - plus the deterministic `tag` each region's build
will push to. The caller applies them with the KSVC's other derived resources, the ksvc is
applied against that tag immediately, and `GET` reports `Building` until kpack finishes
(FUNCTIONS.md: Function Status Resolution). "Created" does not imply "serving".

The manifests are **owned resources of the KSVC**, carrying the same `ownerReference` as the
function's env Secret and DomainMapping. That is what deletes them with the function
(BUILDING.md: Lifecycle & Cleanup): there is no cleanup code because there is nothing to clean
up.

`plan` splits them by how far each piece travels:

| | Scope | Why |
|---|---|---|
| git `Secret` | **shared by every target region** | One token for the function, wherever it builds. Nothing can recover a token whose only copy was on the region that went away. |
| `Image` + build `ServiceAccount` | **per target region**, one set each | Every region builds what it runs. The sets are identical but for the `tag` and cache reference, which name that region's registry. |

The build objects go to the workload's target regions and nowhere else, so nothing has to be
reclaimed by name: a region that runs no copy has nothing to build, and one that does has a
KSVC beside every build object to own it.

`plan` takes the registries the build pushes to - `{region: RegistryConfig}`, whose keys *are*
the building regions - rather than resolving them itself (`KpackBackend.plan`,
`api/services/builder/kpack_backend.py`).

`manifests` is emitted on **every** create and update, not only when a build input changed.
Re-applying an unchanged spec is a no-op kpack does not rebuild from, but it recreates the
`Image` on a region that has never had one, which is what makes a PUT after a switchover
self-healing.

`POST .../functions/{name}/build` re-applies the same composed `Image` and then asks kpack for
one more build of it, so a function can be rebuilt without inventing a spec change
(FUNCTIONS.md: Building again without changing anything).

**`BuildRequest` validates itself on construction** (`common/build.py`). Its fields become
Kubernetes object names and an image reference, and the build path is reachable away from the
HTTP edge, so the inputs are checked where they are assembled.

**A failed trigger is raised, not swallowed** (`BuildBackend.trigger`). A swallowed error there
is a rebuild that silently never happens.

## Active/Active Behaviour

### A region builds what it runs

Each region builds in its **own** cluster, into its **own** registry. The full build stack -
kpack, Stack, Store, ClusterBuilders - is installed in every cluster, and a function deployed to
two regions has an `Image` in both. Nothing crosses a region boundary at runtime: the image a
region serves was built there, from a registry it owns, and published by its own controller.
That is also the whole switchover story - the surviving region was already building and running
its own copy.

### Every write path is a full server-side apply

`Cluster.apply()` uses `apply=True, force_conflicts=True`, and **server-side apply is
create-or-update by construction**. Every path composes the complete desired `Image` and
applies it:

| Path | Behaviour |
|------|-----------|
| POST | compose -> apply -> creates |
| PUT | compose -> apply -> **creates if missing**, else updates. Keeps each region's own ksvc image (BUILD-CONTROLLER.md: Who writes the ksvc image) |
| build | reconstruct -> apply -> **creates if missing** -> annotate the latest `Build` |
| webhook | reconstruct + `commit` = pushed SHA -> stamp it on the ksvc -> apply -> **creates if missing**. No trigger: the changed revision is the spec change kpack builds from |

The build applies *before* it triggers, which is what makes it self-healing rather than merely
idempotent. On a region that has never built the function the apply creates the `Image`, which
builds on its own, and there is no `Build` to annotate - so the trigger finds nothing and says
so instead of failing. It is also the one write path that leaves the KSVC alone, and it skips
regions the function does not run in: an absent KSVC there means there is no build to
re-declare.

> **Never use a targeted patch** (for example patching only `spec.source.git.revision`). It
> returns 404 when the object is absent - precisely the post-switchover case this design must
> survive.

### Reconstruction after a gap

A region that never built a function - one added to `regions` later, or one whose `Image` was
deleted - can still compose it, because the inputs are on the workload itself:

| Input | Source |
|-------|--------|
| runtime | ksvc annotation `ANNOTATION_RUNTIME` |
| git url | ksvc annotation `ANNOTATION_GIT_URL` |
| revision | ksvc annotation `ANNOTATION_GIT_REVISION` |
| commit | ksvc annotation `ANNOTATION_GIT_COMMIT`; absent = build the revision's head |
| builder, version env, build env | runtimes ConfigMap (RUNTIMES.md: Where it lives) |
| git token | the persisted git secret |
| registry credential | the ESO-managed secret (BUILDING.md: Registry & Git Credentials) |

No database and no cross-cluster state replication is required. The Knative Service is the
replicated source of truth.

### Convergence rules

Concurrent writers are safe **only** if the composed spec is a pure function of the function
definition. Duplicate builds come from nonces, not from concurrency:

1. **Deterministic name** - the workload's own `{name}`.
2. **No timestamps, UUIDs or counters** anywhere in the spec.
3. **Never set `spec.build.creationTime`.** The field exists in kpack's `ImageBuild` type and
   setting it forces a rebuild on every apply.
4. **The webhook sets a SHA, not a trigger annotation.** Bumping
   `image.kpack.io/additionalBuildNeeded` is a nonce: two instances handling one push would
   produce two builds. `spec.source.git.revision = <pushed SHA>` is idempotent by data, so
   a redelivery and a concurrent replica converge on one build. The same rule is why the
   explicit rebuild *does* send the trigger when there is no pin to clear, and does not
   when there is: a cleared pin is itself a spec change.

With these, two instances applying the same desired state produce one object and kpack creates
**one** build. No lease and no leader election is required.

The explicit rebuild is not an exception to rule 4, because it is not a write of desired state.
The nonce rule is about state that gets *re-applied*: a timestamp in the `Image` spec is
asserted on every write and rebuilds forever, while an annotation on one `Build` is asserted
once. Two instances handling one rebuild request patch the same `Build` and kpack still creates
one build.

### Accepted consequences

- **The regions run different bytes.** Builds are not bit-reproducible, so the same commit
  produces a different digest in each region. Both run *that commit*, but not the same layers,
  so anything comparing images across regions has to compare source instead.
- **A rollout is not atomic across regions.** A build can succeed in one region and fail in the
  other, which reads as `Failed` with `reason: "BuildFailed"` while the other is `Building`.
- **Build load and registry storage multiply by the number of regions.** A build pod is the
  heaviest thing in a tenant namespace, so quota has to cover concurrent builds in every region.
- **Builds depend on the kpack registry.** If it is down no region can build; every region can
  still run and serve.

## Lifecycle & Cleanup

| Event | Action |
|-------|--------|
| Function delete | Nothing to do *in any cluster*: each region's `Image` and build `ServiceAccount` are owned by its KSVC, so deleting it garbage-collects them. Co-location buys this - ownerReferences cannot cross namespaces (DEPLOYING.md: Chart Topology). |
| Function delete (registry) | Nothing in a cluster owns registry content, so the API deletes both repositories in **every** region's registry, by name (BUILDING.md: Registry cleanup on delete). |
| Old build tags | Each region's **build controller** prunes them from its own registry on an hours-scale sweep (BUILD-CONTROLLER.md: Registry tag GC). |
| Switchover | Nothing to clean up. Each region already held its own build objects for the workloads it runs. |

### Build history

Every `Image` carries an explicit `spec.successBuildHistoryLimit` and
`spec.failedBuildHistoryLimit`, from `build.history.success` and `build.history.failed` (the
chart ships **1** and **1**; the field's own default is 3). kpack garbage-collects older
`Build` objects, and a `Build` owns its pod, so collecting one takes its completed pod with it.

**Neither may be 0** - kpack's `Image` webhook validates `*SuccessBuildHistoryLimit < 1` and
answers *"build history limit must be greater than 0"*, and its defaulting fills only an
**absent** limit, so an explicit 0 reaches that check and every create and update is refused at
admission. The API mirrors the floor (`ge=1`), so the refusal happens once at startup rather
than per function.

They are set rather than left out, because "left out" is kpack's own default of **10 and 10** -
20 `Build`s and 20 completed pods per function. At three hundred functions that is the whole
namespace: `oc get pods` stops being usable and every controller listing pods pays for them. `STACK`/`BUILDPACK` CVE rebuilds and `POST .../build` fill that history faster than
edits do. Failed builds keep their own quota because their pods are the only place the
per-phase build log exists (BUILDING.md: Inside the build pod).

The limits are a constant from configuration, identical on every apply, so they converge like
the rest of the spec. Lowering them takes effect on each function's next build: kpack prunes
when it creates a `Build`.

**Whatever reads that history orders it on the build-number label**, never on the creation
timestamp, which has only one-second resolution. That is what `common.kpack.latest_build` sorts
on, and it is the `Build` the explicit rebuild annotates.

### Registry cleanup on delete

Deleting a function deletes its image repository and its cache repository outright, **in every
region's registry** - each built its own copy, and nothing else would ever address the peer's:

```
DELETE /api/v1/repository/{registry.organization}/{build.builderRepository}/{group}/{name}
DELETE /api/v1/repository/{registry.organization}/{build.builderRepository}/{group}/{name}_cache
```

The path is `RegistryConfig.path`, the image reference with the host removed
(RUNTIMES.md: Registry layout), so the repository deleted is exactly the one that was pushed
to and a layout change cannot make cleanup miss.

This is **Quay's management API**, not the distribution API, which can only delete manifests
(`DELETE /v2/{repo}/manifests/{digest}`) and leaves the repository in the registry's listing.

**It needs a Quay OAuth token, not the push robot.** Robot accounts authenticate
`docker push`/`pull` and the `/v2` endpoints, and cannot call `/api/v1` at all. Generate the
token from an Application under a Quay organization (Organization → Applications), with
`repo:admin`. Two consequences of how Quay scopes it:

- It acts as **the user who authorized it**, so that user needs admin on every namespace the
  platform pushes to, and a deactivated user stops the token working.
- With `registry.organization` empty - the chart default - **each group is its own Quay
  namespace** (`payments/hello`), so one token only reaches the groups its user administers.
  Setting `registry.organization` collapses everything into one namespace and one grant, at the
  cost of needing `FEATURE_EXTENDED_REPOSITORY_NAMES` for the nested path.

**Each registry has its own token, and every pod holds all of them**, because a delete lands
on whichever instance the DNS record points at and that instance is responsible for every
region. The chart reads one Vault entry per region and an ESO template assembles them into
`SERVERLESS_REGION_REGISTRY_TOKENS`, a region-keyed JSON object - one object rather than a
variable per region, because a region name may contain `-`. One underscore: it is not
`SERVERLESS_REGISTRY__API_TOKEN`, which stays the fallback for a region the map does not name
and is what a single-registry install keeps using.

**Wiring that secret is what enables cleanup.** Without it the step is skipped, so an install
that never adds it is unaffected by the upgrade. `registry.deleteOnFunctionDelete: false`
switches cleanup off with the tokens still mounted, and it is the platform-wide switch: the
build controller's tag GC honours the same flag, so `false` stops **every** registry delete the
platform makes. This is the one thing left that crosses a region boundary, and it is
control-plane only.

Both repository paths come from `common.names.image_repository` and `cache_repository`, under
`RegistryConfig.path` - the same functions and prefix the build pushes through. They take only
the validated `{group}` and `{name}` labels, never request input, and run only for the function
offering: a container's image was built elsewhere. The `/api/v1` mechanics - how a repository or
a tag is addressed, and how each HTTP outcome is judged (2xx deleted, 404 already gone, 401/403
names the token's missing namespace admin) - live in `common.registry.RegistryClient`, a domain
module either service may import and a context manager over **one** `httpx` connection.
`api.services.builder.registry` keeps only the policy of *what* a function event reclaims; the
build controller's tag pruning uses the same client.

#### Accepted consequences

- **A crash leaks a repository.** Cleanup is best-effort, fired once after every region
  confirms the delete, and never reconciled. The alternative is a reconcile pass that derives
  "unowned" from a cluster read, which deletes everything the moment that read wrongly returns
  empty.
- **An unreachable peer registry leaks**, the same way and unnoticed.
- **A container pinned to a function's image breaks.** `image` on the container offering is
  grammar-validated only, not scoped to the caller's group, so a container may reference a
  function's image. Deleting the function removes it regardless.
- **Reclamation is not immediate.** The repository leaves the listing at once; the blobs come
  back when Quay garbage-collects, after its time-machine window.
- **Quay-specific.** Moving to another registry means reimplementing this against that
  registry's `/api/v1` equivalent.

## Sample Manifests

The build-side objects only. The platform manifests (KSVC, RBAC, ESO, DomainMapping) are under
DEPLOYING.md: Sample Manifests.

### Image (created by the API, local cluster)

```yaml
apiVersion: kpack.io/v1alpha2
kind: Image
metadata:
  name: hello                          # deterministic: the workload's own name
  namespace: payments-serverless       # owned by the KSVC (DEPLOYING.md: Chart Topology)
  labels:                              # common/labels.py
    serverless.platform/managed-by: serverless-api
    serverless.platform/workload: hello
spec:
  # {base}/{group}/{name}:{revision projected to a legal OCI tag} (RUNTIMES.md: Registry layout)
  tag: registry.internal/<org>/<repo>/payments/hello:main
  builder:                             # cluster-scoped, so no namespace to name
    kind: ClusterBuilder
    name: python
  serviceAccountName: hello-build      # per-function: its git token + both registry creds
  source:
    git:
      url: https://git.internal/payments/hello.git
      revision: main                   # the revision, or the commit a push pinned
  cache:                               # registry, not a PVC (RUNTIMES.md: Build cache)
    registry:
      tag: registry.internal/<org>/<repo>/payments/hello_cache:latest
  build:
    env:
      - { name: BP_CPYTHON_VERSION, value: "3.12" }
      - { name: PIP_INDEX_URL, value: "https://artifactory.internal/artifactory/api/pypi/pypi/simple" }
    # NOTE: never set creationTime here - see BUILDING.md: Convergence rules
```

### ClusterBuilder (serverless-api chart, per region)

Cluster-scoped, one per runtime. The object is cluster-wide but its content is this region's:
each region's release composes the builder from the mirrored stack and store and pushes it to
its own registry.

```yaml
apiVersion: kpack.io/v1alpha2
kind: ClusterBuilder
metadata:
  name: python                       # cluster-scoped: no namespace of its own
spec:
  serviceAccountRef:                 # namespaced: the account the chart puts in the API namespace
    name: kpack-builder
    namespace: serverless-api
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
credential when the internal registry requires auth. The kpack chart creates that account and
its ExternalSecret too (`clusterBuild.serviceAccount` / `clusterBuild.registrySecret`).

### Build ServiceAccount (registry push/pull + git)

The account the **ClusterBuilders** name in `spec.serviceAccountRef`, in the API's namespace.
No git credential: composing a builder image never clones source. It does read two registries -
stack and store from the kpack registry, the composed builder pushed to this region's. The
per-function build account is in BUILDING.md: Registry & Git Credentials.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: kpack-builder
  namespace: serverless-api
secrets:                       # push to this region's registry, pull stack/store
  - name: serverless-registry-creds
  - name: kpack-registry-creds
imagePullSecrets:              # build pod pulling the composed builder image
  - name: serverless-registry-creds
  - name: kpack-registry-creds
```

### Kyverno policy - CA into build pods

Shipped as `templates/kpack/ca-policy.yaml`, gated on `build.caInjection.enabled`. Abridged:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: serverless-api-build-ca-bundle   # {.Values.name}-build-ca-bundle
spec:
  rules:
    - name: mount-ca-bundle
      match:
        any:
          # Every group namespace, matched on the label the tenant controller stamps:
          # they are created at runtime, so there is no list of names to hold here.
          - resources:
              kinds: [Pod]
              namespaceSelector:
                matchLabels:
                  serverless.platform/managed-by: serverless-tenant-controller
              selector:
                matchExpressions:
                  - { key: kpack.io/build, operator: Exists }
      mutate:
        patchStrategicMerge:
          spec:
            volumes:
              - name: internal-ca
                configMap: { name: ca-bundle }
            # BOTH lists - the lifecycle runs as init containers (BUILDING.md: Trust: CA Injection)
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

Why pip needs `PIP_CERT`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` of its own is in
RUNTIMES.md: Why pip needs three variables of its own.

## Open Questions

1. **Artifact server layout** - are pip/npm/go served by one Artifactory/Nexus host on the
   standard `api/pypi`, `api/npm`, `api/go` paths, and are those repos anonymous-read? If they
   require auth, the credential must reach the build pod without landing in the world-readable
   runtimes ConfigMap: a CNB service binding, not env.
2. **Mirror layout** (RUNTIMES.md: Airgapped Mirror Inventory) - can the artifact server expose
   the runtime tarballs under their upstream paths, enabling a single `BP_DEPENDENCY_MIRROR`,
   or must per-dependency `dependency-mapping` bindings be generated from each
   `buildpack.toml`? The former removes a regeneration step on every buildpackage bump.
3. **Build resource sizing** - `build.resources` ships a default (500m/1Gi requests, 2 CPU/4Gi
   limits), so a build is no longer BestEffort. Whether one bound suits every function is the
   open part: a large `node_modules` or Go module graph may need more, and per-function tuning
   would belong beside the workload's `size`.
4. **Cache retention** (RUNTIMES.md: Build cache) - kpack overwrites the one `latest` tag each
   build, so a cache repository does not accumulate tags; superseded blobs are the registry's
   to reclaim. Whether the registry's own GC settles this depends on the registry, and it is
   now per region.
5. **Monorepo pushes** - a push rebuilds every function whose `revision` names the branch,
   whatever `path` each builds from, so one commit in a monorepo can start several builds
   that compile unchanged directories. GitLab's payload lists the files each commit touched
   (the first 20 commits only), so filtering on `path` is possible; it is not done, because
   a false negative - a change outside `path` that matters, a shared lockfile at the root -
   is worse than an extra build. Revisit if build load makes it worth the risk.
6. **Peer-registry reachability** - a function delete reclaims repositories in *every* region's
   registry from whichever API instance took the request (BUILDING.md: Registry cleanup on
   delete), so the internal network must route each region's registry host from every cluster.
   If it does not, deletes leak repositories in the peer region and a different reclamation
   story is needed.
