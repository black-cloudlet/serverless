# Tenant Controller

The tenant controller owns tenant namespaces: one per SSO group, in both clusters, holding
everything a workload needs before it is deployed - network policy, RBAC, the CA bundle, the
build prerequisites. This document covers the namespace model, the template set it renders,
the provision call the API makes before every deploy, the reconcile loop, and the namespace
GC. The rules for who may act for a group are in API.md: Group-based authorization
(tenancy).

## Contents

- [Tenant Namespaces](#tenant-namespaces)
- [The template set](#the-template-set)
- [The loop is local-only; provisioning reaches both clusters](#the-loop-is-local-only-provisioning-reaches-both-clusters)
- [The provision call](#the-provision-call)
- [The reconcile loop](#the-reconcile-loop)
- [Converging a namespace](#converging-a-namespace)
- [Namespace GC](#namespace-gc)
- [When it goes wrong](#when-it-goes-wrong)

## Tenant Namespaces

Each SSO group's workloads - and their builds, config Secrets and credentials - live in the
group's own namespace, **`{group}{suffix}`** (default suffix `-serverless`), in both
clusters. The name is resolved in exactly one place, `TenantNamespaceConfig.namespace_for`,
read by both ends. The namespace is the hard tenancy boundary; the API's group checks and
ownership labels are defense in depth behind it (API.md: Group-based authorization
(tenancy)).

Namespaces are created at runtime, because the group set is SSO data Helm cannot render. The
tenant controller (`tenant_controller/`) is its own Deployment, its own image
(BUILD-CONTROLLER.md: Two images) and its own client certificate. The split is privilege
separation: creating namespaces and writing RBAC is cluster-scoped power the internet-facing
API must not hold. The API cannot create a namespace, and the tenant controller cannot touch
a workload (DEPLOYING.md: RBAC).

The controller has three jobs:

| Job | Trigger | Reach |
|---|---|---|
| **Provision** | `PUT /groups/{group}/namespace`, called by the API before every accepted deploy | **every** region |
| **Reconcile** | level-triggered loop, every `resyncSeconds` (default 300) | local cluster |
| **Namespace GC** | periodic sweep, `gc.intervalSeconds` (default 3600), deletion off by default | local cluster |

All three run in one process, sharing the cluster clients and the mounted template set -
which is why they are not two deployments. The loop holds the main thread so SIGTERM unwinds
it; the provision API runs on a background daemon thread whose startup is awaited for 10s,
so a server that cannot bind crash-loops the pod instead of running beside a dead API. On
shutdown uvicorn gets a 5s graceful budget and the join waits 15s - deliberately longer, or
the cluster clients would close under an in-flight converge.

## The template set

**What lands in a tenant namespace comes from the tenant template set**: ConfigMaps the
chart renders as final YAML, mounted into the controller and applied per namespace. Today
the set carries the CA-bundle ConfigMap, the default-deny NetworkPolicies (ARCHITECTURE.md:
Networking & Exposure), the API's RoleBinding, and the build prerequisites (SCC RoleBinding,
registry credentials). It writes the `ExternalSecret`; ESO fills the Secret it names, which
is how a namespace gets the region's registry credential (ARCHITECTURE.md: Secrets
Management).

The operator's day-2 workflow stays *edit values → Argo sync*: the set's hash is stamped on
each namespace, and a hash mismatch triggers a re-apply.

| Property | Rule |
|---|---|
| Mount | `templatesDir`, default `/etc/serverless/tenant-templates`. Whole-ConfigMap, **never `subPath`** - a subPath mount is not refreshed, and the refresh is how a `helm upgrade` reaches the loop |
| Hash | SHA-256 over `(filename, text)` pairs sorted by filename, first 16 hex chars. Over the raw text, so it names the set itself, whatever the group |
| Placeholders | `{{namespace}}`, `{{group}}`, `{{region}}`, `{{registry}}` - the runtime facts Helm cannot know. Any other `{{token}}` of that shape fails at load; braces that are not that shape (a Go template in a ConfigMap payload) pass through |
| Kinds | `NetworkPolicy`, `ConfigMap`, `RoleBinding`, `Secret`, `ServiceAccount`, `ExternalSecret`, plus `Namespace`. The render gate and the prune iterate the same tuple, so a set can never create what the prune cannot collect |
| Validation | Read, validated and parsed **once, at load**. Each placeholder becomes a YAML-safe sentinel *before* parsing, so `name: {{namespace}}` may be unquoted; rendering is then a walk over the parsed docs. A bad set fails into the loop's backoff before any namespace is touched, naming the file |

**An empty set, or one that renders only a `Namespace`, is refused everywhere it could
act.** A set whose files render to nothing reads as a truncated ConfigMap, and obeying it
would prune every tenant namespace bare. `converge` raises before any write, so the stamp
stays intact and the next pass retries. `reconcile_all` refuses an empty set, because
mounted-but-empty is indistinguishable from a broken mount. Readiness re-checks the gate, or
a truncated ConfigMap would leave the pod in rotation and turn every provision into a `502`
blaming the regions for this pod's own bad mount.

`dev/tenant_templates.py` checks the chart-to-controller seam before a tenant meets it. It
follows the Deployment's projected volume rather than guessing ConfigMap names, and exits on
a missing volume or ConfigMap, because silently checking nothing is the one outcome it must
not have.

## The loop is local-only; provisioning reaches both clusters

Each tenant controller converges **its own** cluster from its own cluster's ConfigMap, so
the two sites never fight while Argo has one synced ahead of the other - the same rule the
build controller follows (BUILD-CONTROLLER.md: Digest propagation). The provision API exists
beside that loop because the one thing a per-cluster loop cannot fix in time is a create
landing in the region whose namespace does not exist yet.

**Writing a peer's namespace is sound only because the set is region-neutral.** Both regions
render it from the same chart and values, so the bytes, and therefore the hash, are
identical. `region` and `registry` are placeholders for that reason: a per-region value
baked in at chart render would mean the two sets never share a hash, and a provision writing
a peer's namespace would write the wrong region's value. A per-region value - a Vault path,
a registry host - belongs in a placeholder, never in the chart-rendered text. During an Argo
sync-skew window the peer's own loop pulls its namespace back to the set that cluster holds;
that is a legitimate convergence, and it ends when the sync lands.

## The provision call

`PUT /groups/{group}/namespace` converges a group's namespace in every region and returns
one row per region. It is internal only - no SSO, no browser, no Route. The caller is the
platform API one namespace away, reaching a ClusterIP Service that a NetworkPolicy scopes to
it.

**Provisioning is a `PUT`, and it returns the namespace.** The caller states a desired end
state rather than asking for a job: the API calls it before every create and retries on
timeout, so "safe to repeat" belongs in the method itself. Re-applying an already-converged
group is how the caller learns it is converged to *this* template hash rather than last
release's, and it costs one read per region. The response carries the namespace so the
suffix rule has one authority; the controller reads that suffix from settings and never from
a module default, or it would provision `{group}-serverless` while the API deployed into
`{group}{suffix}`.

| Endpoint | Answers |
|---|---|
| `PUT /groups/{group}/namespace` | `{group, namespace, templateHash, regions[]}`, each region row `Ready`, `Failed` or `Timeout` with a message |
| `GET /healthz` | Liveness: the process is up |
| `GET /readyz` | Readiness: this pod can converge right now, plus the loaded `templateHash`. Touches no cluster (API.md: Endpoints) - every condition is this pod's own configuration, so a bad ConfigMap stalls its own rollout instead of failing every create |

Failure handling, at both ends:

- A region that fails or times out gets its own row instead of aborting the others, the same
  shape as a partial deploy (API.md: Partial-failure semantics). The timeout is the
  **fan-out's** budget, not per region, so two dead regions cost one deadline between them.
- The controller raises `RegionTotalFailure` when **no** region converged. The API refuses
  the deploy unless **every** region is `Ready`, since a deploy writes to every one of them.
- **A namespace that cannot be confirmed is a `503`, not a create that proceeds.** The call
  is a pre-flight, and a check that could not be run has not passed
  (`api.services.tenant_namespace.provision_namespace`).
- A 4xx from the controller is a configuration mismatch between the two ends (suffix,
  token), not a retryable outage, and is reported as such.
- Where no controller is configured - a dev cluster, where the namespace is whatever the
  operator made by hand - the call is skipped and the skip is **logged**. Skipping is a
  decision, and a decision should not be silent.

**The shared token is depth, not the primary control.** An empty token setting disables the
check, because the NetworkPolicy is the primary control and a dev cluster has no Vault to
take a token from. The constant-time comparison is spelled out in `tenant_controller/api.py`
rather than taken from `cloudlet_apis.auth`: importing that package would pull `pyjwt` and
`cryptography` into an image that must not carry the auth stack (BUILD-CONTROLLER.md: Two
images).

**Converges and provisions run on pools of their own.** Converges are independent per
namespace, so a template rollout over many tenants is bounded by `convergeWorkers` (default
4) rather than serialized. The provision API has its own `provisionWorkers` pool (default
8), separate from the loop's and from the server's threads: it bounds how many converges a
burst of creates can have in flight, and it is why a slow region cannot starve the probes.
Two budgets bound one call: `cluster_op_timeout` (60s) is the controller's budget for the
whole fan-out, and `tenantNamespaces.timeout` (75s) is the API's budget for the HTTP call,
which exceeds it because a new group's first provision applies the full set in every region.

## The reconcile loop

The loop converges every managed namespace in the local cluster whose stamp is stale. That
is how a `helm upgrade` reaches namespaces Argo cannot see. Each pass re-reads the mounted
set, because the kubelet refreshes the mount in place and that refresh is the delivery
mechanism.

- Managed namespaces are found by label,
  `serverless.platform/managed-by=serverless-tenant-controller`. The loop and the GC sweep
  share the selector, so they can never enumerate different worlds.
- Every `fullResyncPasses` pass (default 12, roughly hourly at the default resync) converges
  even stamp-matching namespaces, repairing drift in the objects themselves - a deleted
  NetworkPolicy does not change the stamp.
- **One namespace's failure does not end the pass.** It is logged, counted and skipped, the
  same rule as tag GC (BUILD-CONTROLLER.md: Registry tag GC): an aborting error would
  deterministically starve every namespace after it. A managed namespace with no group label
  counts as a failure rather than being converged blind, because it is state a human made.
- An **all-failed** pass raises. That is one cause rather than many, and should take the
  loop's error backoff instead of a full resync sleep.
- The pool is shut down with `cancel_futures`, so a SIGTERM mid-pass drops the queue instead
  of working through every namespace already submitted.

## Converging a namespace

The converge is one operation, shared by the loop and the provision call. It is idempotent,
and both write under the field manager `serverless-tenant-controller`, so the two racing on
one namespace converge it twice, not halfway. The stamp protocol makes it crash-safe:

1. Apply the `Namespace` **without** the hash annotation. Under server-side apply this
   removes the old stamp, marking the converge in progress. It is also the write that
   creates the namespace.
2. Apply the contents, then prune managed objects the set no longer renders.
3. Re-apply the `Namespace` **with** the hash - last, so a crash anywhere above leaves no
   stamp and the next pass redoes the namespace.

Because the stamp is written last, a matching stamp proves a *completed* converge to that
exact set. That is what makes the provision call's read-first fast path safe.

**Every converge step is unconditional**, so a caller cannot skip the opening apply on a
stale read of someone else's stamp. The namespace name is passed in rather than derived
inside `converge`, so a changed suffix cannot strand namespaces that already exist. The
target's name overrides the template's, as its namespace does for each object: a template
naming something else would create a second namespace mid-converge. Manifests are rendered
for the cluster **being written to**, not for this pod, since a provision converges peers.
Ownership labels (`managed-by`, `group`) are injected in code, not trusted to the templates,
because the prune and the GC select on them.

The **prune** deletes controller-labeled objects of the prunable kinds that the current
render did not produce. The API's workload Secrets and a tenant's own objects are invisible
to it. The listing is per namespace and read at prune time, so it covers objects another
writer created earlier in the same pass.

The provision path adds two duties the loop's converge must not have. A namespace being
**deleted** fails rather than passing: its stamp is still readable, but nothing can be
created in it, and reporting `Ready` would break the fail-closed contract. And the GC's
`empty-since` stamp is **cleared**, because the caller is about to deploy and a clock left
running would let the sweep delete the namespace under an accepted deploy. The loop must not
clear it, or the GC could never collect.

## Namespace GC

A group's namespace is created on demand, but nothing else ever removes one. A group that
deletes its last workload - or one whose namespace a region holds without workloads, as a
region added to the platform after that group's last deploy does - would otherwise hold its
namespace, policies and credentials forever. The GC is slow and loud, modeled on the build controller's tag GC.

| Rule | Behaviour |
|---|---|
| **A grace period, not a watch** | The first sweep that finds a namespace empty of Knative Services stamps `serverless.platform/empty-since`. Only a namespace continuously empty past `gc.graceSeconds` (default 86400) is deleted |
| **A workload clears the clock** | A Knative Service appearing removes the stamp |
| **`keep` always wins** | `serverless.platform/keep` on the namespace blocks deletion, and the skip is logged with the age |
| **Local only** | Each controller collects in its own cluster; provisioning re-creates a collected namespace on the group's next deploy, so a mistaken collection costs one provision, never data |
| **Off by default** | Deletion is the operator's call (`tenantNamespaces.gc.enabled`). A disabled GC says so at startup and logs each namespace it *would* have collected, rather than leaving silence to be read as health |
| **Stamping always runs** | Even with deletion off, so enabling GC later does not restart every clock |

Emptiness is judged from **one cluster-wide list** of Knative Services; each namespace is
then a set lookup rather than its own round trip. A failed list ends the sweep - unreadable
is not empty. If Services are listed but none carry `metadata.namespace`, the sweep refuses
to judge anything: a missing namespace would read as "nothing runs there" for every
namespace at once, which past the grace is a cluster-wide delete.

One namespace failing is logged and skipped, never the end of the sweep. A namespace already
carrying a `deletionTimestamp` is skipped, and an unreadable `empty-since` value is
re-stamped with a warning rather than counted toward the grace. Stamps are written by
**merge patch**, not by the converge's server-side apply: the converge declares only the
template hash under its field manager, so its re-applies can never erase a stamp the sweep
wrote. The sweep runs on its own thread, after the reconcile pass and never inside it -
apiserver I/O in the pass is time no template change rolls out.

## When it goes wrong

The per-converge log line is the audit trail. `kubectl get ns -L serverless.platform/group`,
read with the template-hash annotation, answers "has the new policy reached every tenant".

| Symptom | Cause | What to do |
|---|---|---|
| Every create returns `503`, no region named | The controller is unreachable, or answered with no per-region rows | Check the controller Deployment and the NetworkPolicy between the two namespaces |
| Creates `503` naming one region | That region's converge failed or timed out | Read that pod's converge log; the API fails closed until every region is `Ready` |
| Create rejected with a 4xx from the controller | The API and the controller disagree on `tenantNamespaces` (suffix or token) | Diff the two configurations; a retry cannot fix it |
| The pod never becomes ready | The template set is missing, unparseable, empty, or renders only a `Namespace` | The readiness message names the reason; the backoff log names the file |
| A namespace's hash stays stale | Its converge is failing; the pass logs it and continues | Read the named exception; a managed namespace with no group label is skipped as failed until it is labelled |
| A namespace is `Terminating` and provisions fail | A delete is still in flight | Retry once it is gone |
| A namespace vanished | Namespace GC collected it after the grace period | The next deploy re-provisions it; annotate `serverless.platform/keep` to hold one |
