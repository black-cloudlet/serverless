# Build Controller

The build controller is the platform's second service. It watches kpack `Image`s in its own
cluster and rolls each finished build's digest onto the Knative Service that build was for,
in that same cluster. It also prunes the per-build tags those builds leave in the region's
registry. How source becomes an image in the first place - buildpacks, credentials, the
build flow, the active/active rules - is BUILDING.md.

## Contents

- [Digest propagation](#digest-propagation)
- [Two images](#two-images)
- [One pass](#one-pass)
- [What it writes](#what-it-writes)
- [No leader election](#no-leader-election)
- [Who writes the ksvc image](#who-writes-the-ksvc-image)
- [Registry tag GC](#registry-tag-gc)
- [Configuration](#configuration)

## Digest propagation

The `Image` says what to build; `status.latestImage` says what was built. Nothing in a
request/response path can observe the second - a `STACK` or `BUILDPACK` rebuild fires with
nobody asking (BUILDING.md: What causes a new Build) - so a control loop closes the gap.

`build_controller/` is that loop, in its own Deployment (`{name}-build-controller`) and its
own image. Separate Deployments because a watch loop and an HTTP API scale and restart on
their own terms. It serves no HTTP at all, and takes only the domain and cluster layers of
`common`.

It reuses the API's client certificate rather than minting its own: its rights are a subset
of the API's verbs on the same two resources (DEPLOYING.md: RBAC).

### Two images

`Dockerfile.build-controller` installs the base dependencies only - `pydantic`,
`pydantic-settings`, `kubernetes`. `fastapi`, `uvicorn`, `httpx` and `pyjwt[crypto]` are the
API's, behind a `[project.optional-dependencies] api` extra its own image installs
with `pip install ".[api]"`.

That is what makes the split worth having: the controller holds a client certificate and
writes Knative Services, and it cannot load a web framework or `cryptography` at all -
roughly 23 MB it never imported, and the steadiest source of advisories against a pod that
has no HTTP surface to exploit them through. **What is not installed cannot be flagged, and
cannot be reached.**

The two are only ever built from the same commit, so they cannot disagree about `common/` -
the release job builds both from one tag. CI proves the split rather than trusting it: it
imports each service out of its own image, and asserts the controller's has no `fastapi`,
`starlette`, `uvicorn`, `jwt` or `cryptography`. An import in `common` that quietly pulled a
framework back in would pass every other check (`tests/test_layering.py` catches it in the
source; that step catches it in the artifact).

The tenant controller is a third image on the same principle, one notch along:
`Dockerfile.tenant-controller` installs a `[tenant-controller]` extra holding a web server
and nothing else. It does serve HTTP - one internal endpoint - so `fastapi` there is
expected; what it must never carry is the auth stack, and CI asserts that absence the same
way (TENANT-CONTROLLER.md: The provision call).

### One pass

```
list Images (local)  ──►  reconcile each  ──►  watch from that resourceVersion
      ▲                                              │
      └──────────────  stream ends (timeout)  ───────┘
```

Event-driven, without depending on having *seen* every event. A dropped connection or an
expired `resourceVersion` costs one extra relist, not a function stuck on an old digest. A
`410 Gone` - the server compacting history out from under the watch - is logged and resolved
by the next pass's relist; anything else takes the loop's error backoff.

`buildController.resyncSeconds` (default 300) is both the watch's lifetime and, therefore,
the relist interval - one knob, because they are the same number.

The watch is bound to no namespace, since an `Image` lives in its group's namespace. It
selects on `managed-by=serverless-api` and `offering=function`, because a kpack install is
shared and may carry `Image`s that are not this platform's.

**Both ends are local, for the same reason.** The `Image` is in this cluster because this
region built it, and the digest it produced names this region's registry - a peer cannot
pull it, so publishing there would be worse than doing nothing. Nothing in this loop reads
or writes a peer cluster, and the controller holds one client.

### What it writes

The controller does **not** compose a KSVC. The API owns that spec; the controller owns one
field of it. So it applies the *live* object with the image replaced - a full server-side
apply, like every other write path (BUILDING.md: Active/Active Behaviour), of an object that
has been stripped of the metadata the server owns (`managedFields`, `resourceVersion`,
`uid`, the `status`, the client-side last-applied annotation) and of any pinned
`spec.template.metadata.name`, which Knative would reject.

The digest written is `status.latestImage`, the last *successful* build, so the Image's
ready state is not consulted: a failed newest build leaves the previous digest serving. An
`Image` carrying no workload label, no namespace or no successful build yet is skipped.

**The KSVC it writes is the one the `Image` names.** `Reconciler._roll_out`
(`build_controller/reconciler.py`) takes the namespace off the `Image` object rather than
deriving it, so the KSVC that gets the digest is always the one that `Image` was built for.
Finding no KSVC there is expected rather than an error: an `Image` whose delete cascade has
not run yet has none.

Two things stop a write. The repository is deliberately **not** one of them: this is the
only writer of the image after the create (Who writes the ksvc image, below), so refusing a
moved one would strand the workload on a repository nothing pushes to.

| Condition | Why it is left alone |
|---|---|
| The KSVC already runs that digest | The loop's normal outcome, and why a resync costs nothing |
| It is not labelled `offering: function` | A container that reused a deleted function's name must not inherit its image |

A read or apply that fails is logged, not raised: the next resync retries it, and one bad
KSVC does not end the pass.

### No leader election

Two replicas - or two regions' controllers reaching the same conclusion - apply the same
desired state, and a server-side apply of identical content is a no-op that produces no
Knative revision. Same convergence rules as every other writer (BUILDING.md: Convergence
rules); `buildController.replicaCount` above 1 is safe, just redundant.

Two controllers never see the same input at all: each follows its own region's `Image`s and
writes its own region's KSVCs, so there is nothing to contend over between regions. The
redundancy that matters is within a region, and identical applies converge there.

## Who writes the ksvc image

The controller can own the digest outright because exactly one writer touches the KSVC image
per phase, with no overlap. The rest of the API/controller split - what `BuildBackend.plan`
returns, which manifests are owned resources of the KSVC, and which travel per region - is
BUILDING.md: Ownership: API vs Build Service.

| Path | ksvc image |
|------|-----------|
| POST | **written once, per region**: `{that region's registry}/{organization}/{builderRepository}/{group}/{name}:{revision}` |
| PUT | **kept, per region** - whatever each region is running, read back off its own KSVC. One value fanned out would point a peer at this region's registry |
| `POST .../build` | **not written** - no ksvc is applied at all |
| build controller | **the only writer after the create**, and only ever the digest |

A create has nothing to keep, so it deploys at the revision tag and reads `Building` until a
build pushes something there. After that the tag is never written again: it resolves to the
digest already running, so writing it cuts a revision of *the same code*, and the real
rollout arrives minutes later from the controller anyway. Two revisions where one belongs.

This is also what lets a **moved repository** work (RUNTIMES.md: Registry layout). The
controller does not compare repositories - it cannot, being the only writer - so the first
build that pushes to the new layout moves the workload there on its own. The update that
re-tags the `Image` and the roll-out are separate events, in that order, which is why the
migration reads "build first".

`POST .../functions/{name}/build` is the manual half of that: it re-applies the same
composed `Image` and then asks kpack for one more build of it, so a function can be rebuilt
without inventing a spec change (FUNCTIONS.md: Building again without changing anything).

## Registry tag GC

kpack pushes every successful build **twice**: the revision tag moves to the new digest, and a
unique `b{n}.{date}.{time}` tag is added beside it. The revision tag overwrites; the build
tags accumulate, one per build, for the life of the function - and `STACK`/`BUILDPACK` CVE
rebuilds and `POST .../build` create builds without a user touching anything, so they grow
even for functions nobody edits. They count against registry quota, and nothing else
reclaims them short of deleting the function. A revision change leaks the same way: the old
revision's projected tag stays behind permanently.

The build controller prunes them (`build_controller/gc.py`), because the problem is shaped
like the controller:

- **Per-region, local only.** A region builds what it runs into its own registry, so each
  region's controller prunes exactly the registry its region filled, with its own token. The
  one cross-region call stays the API's delete cleanup (BUILDING.md: Lifecycle & Cleanup);
  the GC adds none.
- **It already holds the ground truth.** The sweep rides the resync's `Image` listing - no
  second LIST - and judges tags against `spec.tag` and `status.latestImage` as just fetched.
- **Reconciled**, unlike the fire-once cleanup on delete: garbage is re-derived from live
  state on every sweep, so a crash or an unreachable registry leaks nothing permanently -
  the next sweep collects it. One function failing is logged and skipped, never the end of
  the sweep: the listing order is stable, so an aborting error would starve every function
  after it, deterministically, on every sweep.

**One region per registry is the safety premise, and it is enforced twice.** A controller
pruning a repository protects only its *own* region's serving digest; two regions on one
registry would each delete tags the other still serves. So the chart requires
`regions[].registry.url` on every region and refuses to render two regions on one registry
(RUNTIMES.md: Registry layout), and the controller independently refuses to sweep - loudly,
naming the regions and the shared host - when its resolved registry matches another
region's, as the backstop for a hand-rolled config. Refusing is the safe answer because the
alternative is not a smaller sweep: it is deleting tags a peer's KSVC is pinned to.

Per function repository, a sweep **keeps**:

| Kept | Why |
|---|---|
| The current **revision tag** (the tag half of `Image.spec.tag`) | A create deploys at it; a switchover region rebuilds into it |
| Every tag on the **digest of `status.latestImage`** | Deleting the last tag on a manifest lets Quay collect it, and the digest-pinned KSVC could no longer pull on a node change |
| The newest **`buildController.gc.keepBuilds`** build tags **beyond all of those** | Default **3**, mirroring `build.history.success`. Protected tags never consume a slot, so the retained history is exactly what the knob says |
| Any tag the listing reports **without a digest** | It cannot be proven safe |
| Everything, for an `Image` recording **no successful build** | A fresh Image - created, re-created, or post-switchover - can sit over a repository still holding a previous incarnation's tags; with nothing digest-protected, pruning would be a guess |

Everything else - older build tags, stale revision tags - is deleted. A tag whose host is not
this region's registry is skipped with a warning. The cache repository is never addressed:
it reuses one `latest` tag and does not accumulate (BUILDING.md: Open Questions).

**Wiring.** The controller mounts the same per-region tokens Secret the API holds
(`registry.apiTokens`, optional for the same ESO reason) and resolves only its own region's
token. `registry.deleteOnFunctionDelete: false` - the platform-wide "may we delete registry
content" switch - stops the GC exactly as it stops cleanup on delete.

The sweep runs on its **own daemon thread**: it is registry-bound I/O that must never sit
between the reconcile loop's relist and its watch, where every minute spent is a minute no
digest rolls out. **The first sweep is not waited for** - `TagGC` starts with a zero
deadline, so a restarted controller shows its GC working, or says why it is not, within one
pass rather than an interval later. The next deadline is set when a sweep starts, so a
failing registry retries at the next *due* resync; a sweep still running when the next is
due is logged and not doubled. The tag listing itself is page-capped (`common/registry.py`),
so paging that never terminates - a proxy dropping the `page` param - becomes a warning and
an empty listing (no listing, no deletes), never a wedged thread.

**The logs are the feature's UI.** Startup states whether the GC is on - and if off, *why*:
disabled by configuration (said once), or a loud reason re-said once per interval
(`deleteOnFunctionDelete` off, a missing token, a shared registry), so a state an operator
likely wants fixed is never deduced from silence. A token that syncs *after* the pod started
needs a pod restart to be seen - env is injected at container start - which the log line
says outright. Each sweep logs a per-function verdict
(`pruned 4 of 8 tag(s) in 'payments/hello'`), names every deleted tag individually, and
closes with a summary (`swept 12 function repositories in 'central', pruned 31 tag(s), 0 failed`).
Skips are named too: a
tag on a foreign host is a warning, an `Image` with no successful build yet is an info line,
a repository already deleted mid-sweep is silent by design.

### Accepted consequences

- **An old revision can outlive its tags.** Only the serving digest is protected; a revision
  pinned to an older one that re-pulls after its tags are pruned *and* after Quay's
  time-machine window has passed will fail. `keepBuilds` plus the time machine is the
  buffer. Deliberate: the RBAC for reading Revisions already exists, so the cost would only
  be a Revision list per sweep - it is the retention window's coverage of the edge, not
  RBAC, that makes the simpler rule enough for now.
- **Quota returns late.** A deleted tag sits in Quay's time machine until
  `DEFAULT_TAG_EXPIRATION` passes; the sweep frees the listing at once and the bytes later.
- **A container pinned to a function's build tag breaks** - the same accepted consequence as
  the repository delete (BUILDING.md: Registry cleanup on delete), one tag at a time.
- **Quay-specific**, exactly as that cleanup is: `/api/v1`, the same token, the same caveat
  about other registries.

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `buildController.resyncSeconds` | 300 | The watch's lifetime, and so the relist interval |
| `buildController.replicaCount` | 1 | Above 1 is safe, just redundant (no leader election) |
| `buildController.gc.enabled` | true | Off means the GC says so once at startup and never sweeps |
| `buildController.gc.intervalSeconds` | 21600 (6h) | Sweep interval, hours-scale rather than the resync's minutes |
| `buildController.gc.keepBuilds` | 3 | Newest build tags kept beyond the protected ones |
| `registry.deleteOnFunctionDelete` | - | False blocks the GC entirely, loudly, once per interval |
| `registry.apiTokens` | - | Per-region registry API tokens; without this region's token the GC announces itself off and does nothing |
