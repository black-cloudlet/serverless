# Namespace-per-Group: Step-by-Step Implementation

The execution companion to [namespace-per-group.md](./namespace-per-group.md).
That document says *what* and *why*; this one says *in which order, in which
PR, touching which files, verified how*. Each PR is small enough to review in
one sitting, lands green on `main`, and changes no behavior until PR 5
switches resolution outright - pre-GA there are no customers, so there is no
migration, no fallback path, and no runtime flag (see Cutover in the design
doc); git is the rollback lever.

Every PR runs the same local gate before push: `ruff check`, `pytest`
(coverage per `checks.yml`), and - where the chart changed -
`helm template | kubeconform`. These are the fast checks CI runs; nothing
merges red.

## Order & dependencies

```mermaid
flowchart LR
    PR1["PR 1<br/>cluster-scoped client"] --> PR2["PR 2<br/>provisioner core"]
    PR1 --> PR6["PR 6<br/>controller watch"]
    PR2 --> PR3["PR 3<br/>ensure API"]
    PR4["PR 4<br/>chart"] --> PR5["PR 5<br/>API integration"]
    PR3 --> PR5
    PR1 --> PR5
    PR2 --> PR7["PR 7<br/>namespace GC"]
    PR5 --> PR8["PR 8<br/>cutover cleanup"]
    PR6 --> PR8
    PR7 --> PR8
```

PR 4 (chart) has no code dependency and can proceed in parallel from day one.
PR 6 can land any time after PR 1. Everything before PR 8 is inert in
the running system: the provisioner manages nothing yet, and the legacy
namespace serves as today.

---

## PR 1 - the cluster client goes cluster-scoped

**Goal:** `Cluster` = one region's connection; namespace is per-operation.
Zero behavior change: every caller binds the legacy namespace.

1. `common/cluster/client.py`
   - Remove `self._namespace` (`__init__` keeps `region_config`/`settings`
     for connection, identity, timeouts).
   - Every namespaced method (`apply`, `get`, `list_resources`, `watch`,
     `delete`, `patch`, `pod_log`, the follow entry point) takes an explicit
     `namespace`; `namespace=None` on the read paths means all-namespaces
     (the dynamic client's native behavior).
   - `apply` gains `field_manager: str | None = None` (passed through to
     server-side apply; None keeps today's manager, so API/controller
     writes are untouched).
   - New `Cluster.in_namespace(ns) -> NamespacedCluster`: a thin view
     currying `namespace` over the same method surface, no connection of
     its own. `clusters_for` / `select_local` unchanged - they deal in
     connections.
2. Mechanical caller updates, all binding `settings.workloads_namespace` at
   the boundary where a cluster enters use:
   - `api/services/regions/deployer.py` - `resolve_targets` /
     `local_cluster` return bound views (`Deployer` binds once, from
     settings for now).
   - `api/services/regions/region_apply.py`, `region_read.py`,
     `preflight.py`, `api/services/streams/*` - signatures switch from
     `Cluster` to the view; bodies unchanged.
   - `controller/reconciler.py`, `controller/digest.py` - bind the legacy
     namespace explicitly for now (PR 6 removes this).
3. `common/names.py`: `namespace_for_group(group, suffix="-serverless")`
   - the normalized group + suffix (`{group}-serverless`, group-first so
   tenant namespaces list under their group's name), **reject > 63 chars as
   `422`** on the suffixed result, and **refuse a group beginning with
   `kube-` or `openshift-`** - group-first naming could otherwise produce a
   namespace that reads as the system's own - at the same
   edge the DNS-1123 check runs (per the plan's risk decision); surfaced on
   `/info` `naming`.
4. `common/labels.py`: `LABEL_PROVISIONER_MANAGED`,
   `ANNOTATION_TEMPLATE_HASH`, `ANNOTATION_EMPTY_SINCE`, `ANNOTATION_KEEP`.
5. `common/cluster/kinds.py`: add `NAMESPACE`, `NETWORK_POLICY`,
   `ROLE_BINDING` to `ResourceKind`.
6. `tests/factories.py`: the fake cluster records the namespace per call
   and answers namespaced/all-namespaces lists; existing tests pass with
   the legacy binding.

**Tests:** new `test_names.py` cases (the suffixed shape, length rejection, reserved system prefixes, collision
with normalization rules); every existing suite green unmodified in intent.
**Done when:** full pytest green; `git grep "_namespace"` in
`common/cluster/` returns nothing.

## PR 2 - provisioner core (config, templates, reconcile, loop)

**Goal:** the package exists, converges its local cluster from a template
directory, and does nothing in a deployment where no managed namespaces
exist.

1. `provisioner/config.py`: `ProvisionerSettings(CommonSettings)` -
   `resync_seconds`, `error_backoff_seconds` (defaults per
   `ControllerSettings`), `templates_dir`, `namespace_suffix`.
2. `provisioner/templates.py`: read the mounted directory (whole-ConfigMap
   mount), parse manifests, substitute `{{namespace}}`/`{{group}}`, compute
   the set hash (sorted, content-addressed). Mirrors the mounted-file
   pattern of `api/services/builder/runtimes.py`.
3. `provisioner/reconcile.py`:
   - `converge(cluster, group, templates) -> RegionStatus-like result`:
     SSA-apply namespace first then contents (own `field_manager`), prune
     labeled leftovers absent from the set, stamp the hash **last**.
   - `reconcile_local(cluster, templates)`: list managed namespaces
     (label), converge those whose stamp differs from the current hash.
4. `provisioner/main.py`: copy of `controller/main.py`'s shape - signal
   handling, `_MIN_PASS_SECONDS`, raise-backs-off loop - driving
   `reconcile_local`.
5. `Dockerfile.provisioner`: copy of `Dockerfile.controller`
   (`COPY common provisioner`, `python -m provisioner.main`).
6. `pyproject.toml`: `include = ["api*", "common*", "controller*",
   "provisioner*"]`; `checks.yml` builds/scans the new image alongside the
   controller's.

**Tests:** `test_provisioner_reconcile.py` - create-from-nothing, converged
hash is a no-op, hash change re-applies, prune removes a dropped template's
object and only labeled objects, label rename converges via SSA (fake
cluster asserts the applied set), crash between apply and stamp re-converges
next pass.
**Done when:** the image builds; a loop against the fake cluster with zero
managed namespaces makes zero writes.

## PR 3 - the ensure API and the two-region fan-out

**Goal:** `POST /ensure/{group}` converges the group in **both** clusters
and reports per-region results.

1. `provisioner/ensure.py`: `ensure(clusters, group, templates)` - run
   `converge` per region concurrently (thread fan-out shaped like
   `Deployer.fanout`, but small and local to the provisioner), return
   per-region status rows; idempotent by construction.
2. `provisioner/api.py`: minimal FastAPI - `POST /ensure/{group}`
   (validates the group with the shared rules from `common/names`, returns
   per-region results and the hash converged to), `/healthz`, `/readyz`
   (readiness = templates loaded; never touches a cluster, per the API's
   probe rule). Optional shared-token check
   (`SERVERLESS_PROVISIONER_TOKEN`, Vault→ESO), constant-time compare like
   `verify_admin_key`.
3. `provisioner/main.py`: uvicorn on a thread beside the loop.

**Tests:** `test_provisioner_ensure.py` - both regions converged; one
region down → its row fails, the other succeeds, call reports both; group
name validation at the edge.
**Done when:** ensure of a new group creates the namespace + set in both
fake clusters and a second call is a no-op.

## PR 4 - chart (parallel track)

**Goal:** everything the provisioner needs exists at render; nothing changes
for the running system (all new objects inert until PR 5).

1. `templates/tenant-templates-configmap.yaml`: the per-namespace set,
   rendered via **existing helpers** - extract the bodies of
   `networkpolicy.yaml` and `ca-bundle.yaml` into named partials in
   `_helpers.tpl`, included from both the legacy render and the template
   set (one source, two targets, drift impossible); labels via
   `serverless-api.namespaceLabels`; per-tenant RoleBinding; kpack build
   SA/SCC RoleBinding + registry-cred `ExternalSecret` entries gated on
   `build.enabled`.
2. `rbac.yaml`: Role → **ClusterRole** (same rules); keep the legacy
   RoleBinding. Add the read-only ClusterRole + ClusterRoleBinding for the
   cert user (KSVC/DomainMapping/Image/Build reads - the preflight,
   listings, and PR 6's watch).
3. `provisioner-rbac.yaml`: the provisioner's ClusterRole (namespaces,
   networkpolicies, rolebindings, configmaps, serviceaccounts,
   externalsecrets, quotas, limitranges; read on KSVCs) bound to its CN.
4. `certificate.yaml`: second `Certificate`, CN
   `serverless-provisioner.clients.{base_domain}`, same ACME issuer.
5. `provisioner.yaml`: Deployment (2 replicas) + Service, modeled on
   `build-controller.yaml`; mounts templates ConfigMap + client cert; a
   NetworkPolicy admitting ingress only from the API pods.
6. `kpack/ca-policy.yaml`: match tenant namespaces **by label selector**,
   keeping the name match for the legacy namespace until PR 8.
7. `values.yaml`: `tenantNamespaces:` block - `suffix`, `provisioner.*`
   (image, resources), `gc.enabled: false`, `gc.graceSeconds`. No
   `enabled` master flag - namespace-per-group becomes the only mode at
   PR 5.
8. `checks.yml`: render the embedded template set (helm template with a
   test values file, extract the ConfigMap data) through kubeconform.

**Done when:** `helm template` output is identical to before with the
default values except the new inert objects; kubeconform passes on both the
chart and the extracted template set.

## PR 5 - API integration: the cutover

**Goal:** workloads deploy into per-group namespaces. This is the one PR
that changes behavior, and it changes it outright - no flag, no fallback:
pre-GA the cost of a hard cutover is one wipe of test data, and the saving
is a permanently single code path and test matrix.

1. `common/config.py` / `api/core/config.py`: the `tenant_namespaces`
   settings block (suffix, provisioner URL).
2. One resolution point: `resolve_namespace(group, settings)` → the group
   namespace, always. `WorkloadService` calls it once per request and binds
   `cluster.in_namespace(...)` there - the *only* line that changes where
   writes land. (`workloads_namespace` survives until PR 8 only for
   anything not yet cut over.)
3. Ensure-on-create: an async client (httpx, CA bundle trusted) called from
   `assert_deployable` in `preflight.py` beside the host/name checks;
   failure → the fail-closed `503` posture ("a check could not be run").
4. Host-uniqueness preflight: the DomainMapping conflict probe lists
   cluster-scoped (`namespace=None`, label-selected) so hosts stay unique
   across all tenant namespaces.
5. Object naming: `common/names.py`'s `object_name` becomes plain
   `{name}` - the namespace scopes it, and the platform's primary key
   becomes **(namespace, name)** - and every derived name (`{name}-env`,
   `{name}-files`, `{name}-git`, `{name}-pull`, the `{name}-build` SA)
   shortens with it. The kpack Image stays the object name **verbatim**
   (`build_image_name` is unchanged): the rule that the Image name must
   fit a 63-char label value now binds plain `{name}`, so the alignment
   #69 built holds with more headroom. The group stays explicit in
   exactly three places: the **default host**
   (`{name}-{group}.{routeDomain}`, unchanged - DNS is global and the
   wildcard cert covers one label), the **registry repositories**
   (`image_repository(group, name)` / `cache_repository(group, name)`
   already take the group as a parameter - no change), and the
   **ownership labels** (unchanged). Reverse mapping already goes through
   the labels, so nothing ever parses a name back into halves. This lands
   in this PR because objects get their new names and new namespaces in
   one move.
6. The pair rule **moves, it does not disappear**: `validate_object_name`'s
   combined `{name}-{group}` ≤ 63 check (the one real limit since the
   per-field caps were dropped) relocates to the default-host derivation
   in `preflight.py` - a create whose *default* host would exceed 63 is a
   `422` telling the caller to supply `hostname`; with a custom hostname
   the pair length no longer matters. `/info` `naming` re-words the rule
   accordingly: `{name}` ≤ 63 alone, the group bounded by the namespace
   rule from PR 1, combined ≤ 63 for the default host only.
7. Deploy note in the PR body: per environment, delete the old test
   workloads (or the legacy namespace's contents), sync, redeploy - they
   land in `{group}-serverless`. Rollback is redeploying the previous
   chart + image.

**Tests:** `test_workload_namespaces.py` - resolution, ensure called in
preflight and its `503` on failure, host conflict detected across two
namespaces, same `{name}` in two groups coexists (distinct namespaces,
distinct hosts, distinct registry repos); the existing
`test_workload_service.py` suite updated to the group-namespace and
plain-`{name}` expectations (mechanical - the fake cluster records
namespaces since PR 1).
**Done when:** the full suite is green and a create in a fresh test
environment lands Ready in `{group}-serverless` in both regions.

## PR 6 - build controller across namespaces

**Goal:** the digest loop serves Images wherever they live.

1. `controller/reconciler.py`: resync + watch with `namespace=None` and the
   managed-by label selector - one stream, not N.
2. `controller/digest.py`: take the KSVC's namespace from each Image's own
   metadata (co-located by design) when re-applying the digest.
3. `TagGC` needs nothing: it consumes the listing it is handed.

**Tests:** extend `test_kpack_build.py` - Images in two namespaces each
roll their digest onto the right KSVC; legacy-namespace Images still work.
**Done when:** a mixed listing (legacy + tenant namespaces) reconciles both.

## PR 7 - namespace GC

**Goal:** empty tenant namespaces are collected, slowly and loudly.

1. `provisioner/gc.py`: `NamespaceGC`, modeled on `controller/gc.py
   TagGC` - own daemon thread, deadline set at sweep start, loud
   off-reasons, one failure never ends the sweep. Per managed namespace in
   the **local** cluster: KSVCs present → clear `empty-since`; absent →
   stamp it if missing; `now - empty_since > gc_grace_seconds` and no
   `keep` annotation and `gc.enabled` → delete (cascades the contents).
2. Wire into the provisioner loop via the same `maybe_sweep` hook shape the
   reconciler gives `TagGC`.

**Tests:** `test_provisioner_gc.py` - grace period honored across sweeps,
`keep` respected, a workload appearing clears the stamp, disabled-is-loud,
never touches an unlabeled namespace, subset-regions case (empty here,
populated in the peer → collected here only).
**Done when:** GC deletes exactly the fixture it should and logs the
verdict line per sweep.

## PR 8 - cutover cleanup

**Goal:** nothing legacy remains - in the chart, the code, or the docs.
No migration precedes this: PR 5 already switched the only mode, and the
test environments redeployed their workloads then.

1. Chart: remove the legacy workloads namespace from `namespaces.yaml`,
   its NetworkPolicy/CA-bundle/RoleBinding renders (the shared partials
   keep their one remaining target, the template set), and the Kyverno
   name-match.
2. Code: remove `workloads_namespace` from `common/config.py` and every
   remaining binding of it.
3. Environments: delete `serverless-workloads` in both clusters; enable
   `gc.enabled` once the cutover has settled.
4. Docs: fold both proposal documents into ARCHITECTURE.md/DEPLOYING.md
   (per docs/README.md, the code is now the source of truth).

**Done when:** the legacy namespace is gone from both clusters and from the
chart, `git grep workloads_namespace` returns nothing, and
ARCHITECTURE.md: Design Decisions reads "namespace-per-group".
