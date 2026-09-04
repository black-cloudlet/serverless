# API Service

The REST service both offerings share: the endpoint surface, how a request is validated and
accepted, how one write reaches both regions, who may make it, and what an error looks like.
Per-offering detail is in FUNCTIONS.md and CONTAINERS.md, the streaming endpoints in
STREAMING.md, the platform as a whole in ARCHITECTURE.md.

## Contents

- [What the service is](#what-the-service-is)
- [REST API Specification](#rest-api-specification)
- [How a request is handled](#how-a-request-is-handled)
- [Multi-Region (Active/Active HA) Design](#multi-region-activeactive-ha-design)
- [Authentication & Authorization](#authentication--authorization)
- [Error model](#error-model)

## What the service is

A FastAPI application that turns a REST request into Knative objects in two OpenShift
clusters. It runs active/active in both; a DNS record fronts the active instance. Every write
is applied to **both** regions.

**One engine, two offerings.** The engine (`api.services.workloads.WorkloadService`) never
branches on which offering it serves. Everything that differs is a member of
`api.services.offering.Offering`, implemented once per offering and passed to the engine per
call: the response class, which derived Secrets are pruned, the state an update carries
forward (a function's git token), the build status folded into a read, and the cleanup after
a delete. The engine is a process-wide singleton, so the offering travels as an argument.
Routers hold one object per offering, a `FunctionService` or a `ContainerService`
(`api.services.offering_service.OfferingService`); the base class fixes the offering at class
level and forwards every read, stream and delete.

The orchestration is one class in one file (`api.services.workloads.service`), and what it
orchestrates is split so each part is testable on its own: `manifests/` (pure builders),
`regions/` (fan-out and per-region I/O), `state/` (pure interpretation) and the builder.
`ApplyRequest` carries the whole apply input as one value - the union of both offerings'
needs, a container leaving the build metadata `None` and a function carrying the pull-secret
manifest.

## REST API Specification

Base path: **`/api/serverless/v1`** - the chart's `basePath` followed by the version. The
docs, the OpenAPI document, the SSO token proxy and the health probes sit under the same base
path, and the chart builds the kubelet's probe paths from the same `basePath` value it hands
the code. Two consequences:

1. **Whatever fronts the API must forward the path whole** - a plain Route with `spec.path`,
   no `rewrite-target`. A router that strips the leading segments leaves nothing that matches.
2. A different `basePath` moves every path with it. A local run leaves it empty and calls
   `/v1/...`.

There is one path per endpoint and nothing answers beside it. All endpoints require a valid
SSO bearer token (*Authentication & Authorization*) **except the public discovery endpoints
`GET /api/serverless/v1/{containers,functions}/info` and the health probes**. All responses
are JSON. Times are RFC 3339 with a timezone offset; workload timestamps (`createdAt`) are
rendered in **Israel local time** (IDT `+03:00` / IST `+02:00`, daylight-saving aware).

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/serverless/v1/groups/{group}/functions` | Create a function (build from Git). `202`; poll `statusUrl`. |
| `GET` | `/api/serverless/v1/groups/{group}/functions` | List the group's functions: general info per workload, status rolled up across regions. `?sort=name\|createdAt` (default `name`). |
| `GET` | `/api/serverless/v1/groups/{group}/functions/{name}` | One function: spec + per-region status. |
| `PUT` | `/api/serverless/v1/groups/{group}/functions/{name}` | Replace the mutable spec; a build-input change rebuilds from source. `202`. |
| `POST` | `/api/serverless/v1/groups/{group}/functions/{name}/build` | Build the current source again, no body (FUNCTIONS.md: Building again without changing anything). `202`. Also the function's **git webhook**: with `X-Gitlab-Token` instead of a bearer, a push builds its commit; a delivery this function does not want is `200` with `accepted: false` (FUNCTIONS.md: Git webhook). |
| `POST` | `/api/serverless/v1/groups/{group}/functions/{name}/webhook/rotate` | Replace the function's webhook token and return the new one. `200`. |
| `DELETE` | `/api/serverless/v1/groups/{group}/functions/{name}` | Delete the function in every region. |
| `POST` | `/api/serverless/v1/groups/{group}/containers` | Create a container from an image. `202`; poll `statusUrl`. |
| `GET` | `/api/serverless/v1/groups/{group}/containers` | List the group's containers; same shape and `?sort` as the function list. |
| `GET` | `/api/serverless/v1/groups/{group}/containers/{name}` | One container: spec + per-region status. |
| `PUT` | `/api/serverless/v1/groups/{group}/containers/{name}` | Replace the mutable spec; registry-cred rotation/keep rules are under FUNCTIONS.md: Editing a workload. `202`. |
| `POST` | `/api/serverless/v1/groups/{group}/containers/{name}/pull` | Re-resolve the image tag by cutting a new revision, no body (CONTAINERS.md: Pulling the tag again). A digest-pinned image is a `400`. `202`. |
| `DELETE` | `/api/serverless/v1/groups/{group}/containers/{name}` | Delete the container in every region. |
| `GET` | `/api/serverless/v1/groups/{group}/{type}/{name}/stats` | Live state only - the lightweight endpoint to poll (FUNCTIONS.md: Polling live state). |
| `GET` | `/api/serverless/v1/groups/{group}/{type}/{name}/stats/stream` | The same body pushed as Server-Sent Events every `interval` seconds (STREAMING.md: Streaming). |
| `GET` | `/api/serverless/v1/groups/{group}/{type}/{name}/pods` | The workload's pods on the current region - the only source of pod names. Streams by default; `?follow=false` returns one roster. |
| `GET` | `/api/serverless/v1/groups/{group}/{type}/{name}/logs/pods/{pod}` | One pod's log from the current region. Follows by default; `?follow=false` returns a snapshot. |
| `POST` | `/api/serverless/v1/stream-tickets` | Mint a short-lived `?ticket=` for one streaming path (STREAMING.md: Browsers cannot send an `Authorization` header). |
| `GET` | `/api/serverless/v1/{containers,functions}/info` | Public (no auth) capability discovery for dynamic UI rendering; config/code-derived, no cluster calls. |
| `GET` | `/api/serverless/{healthz,readyz}` | Liveness/readiness (no auth), under the base path like everything else. Constant responses; they never touch a cluster. |
| `GET` | `/api/serverless/{docs,redoc,openapi.json}` | Swagger UI / ReDoc from vendored assets (no CDN, for airgap). |

Parameters worth knowing beyond the table:

- The list endpoints fan out to every region and merge by workload. An unreachable region is
  skipped; only when all are down does the call fail (`502`).
- `/stats` sums totals across regions **before** rounding, so a total need not equal the sum
  of the printed per-region figures. Totals are `null` when any region could not be measured.
  A workload scaled to zero reads `replicas: 0`, `usage: null`.
- The log endpoint takes `container` (default `user-container`), `sinceSeconds`, `tailLines`
  (clamped to `stream.snapshotTailLines`), `ticket`, and - snapshot only - `limitBytes`
  (clamped to `stream.snapshotMaxBytes`). `tailLines` opens on the newest lines however old
  they are. A follow ends with an `end` event when the pod goes away; on Knative that is
  routine, not an error.
- `/stream-tickets` takes `{"path": "..."}`. A non-streaming path is a `400`; a deployment
  with no signing key answers `503`, and streams then accept the `Authorization` header only.
- `/stats/stream` pushes on a clamped `interval` (STREAMING.md: A requested interval is
  clamped, never rejected). One connection replaces a client's poll loop, so the cross-region
  fan-out happens once per interval however many clients are watching.

**`/info` exists so a client never hardcodes a vocabulary.**

| Field | Contents |
|---|---|
| `statuses.workload` | The `status` set. It is the `Literal` the responses are typed with, so it cannot drift from what is sent. |
| `statuses.region` | The per-region status set. |
| `statuses.terminal` | The subset a poller stops on; anything else is still in flight. |
| `statuses.reasons` | Values of the machine-readable `reason` field - the cause behind a `Failed` status, on the workload and on each failing region row, in the full GET and `/stats` alike. |
| `errorCodes` | Walked off the `APIError` subclasses, so an error added in code is published without a second edit. |
| `naming` | The rule no per-field schema expresses: `name` and `group` are each valid at 63 characters, but it is `{name}-{group}` that becomes the host's first DNS label, and `group` is a path parameter, not a body field. |

`BuildFailed` is set authoritatively off the kpack Image; the other reasons are derived
best-effort from the failing Kubernetes/Knative conditions, so an unrecognized cause is `null`
with the raw text on the region's `message`. Per-field rules (pattern, maxLength, description,
examples) are on `/openapi.json`.

### Request semantics

Workload secrets and config files are **not** separate endpoints. They are derived inline from
the deploy request (`env` with `secret: true`, and `files`) and created by the API as
`{workload}-env` / `{workload}-files` objects (ARCHITECTURE.md: Secrets Management).

**Async: submit and poll.** `POST`/`PUT` validate synchronously, so the caller gets an
immediate `400`/`404`/`409`, then return **`202 Accepted`** with `status: "Pending"` and a
`statusUrl`. The build/deploy runs in the background. Clients poll `GET {statusUrl}` - the
resource itself, `/api/serverless/v1/groups/{group}/{type}/{name}` - until `status` is `Ready`
or `Failed`. This suits slow FaaS builds and ServiceNow workflow patterns.

**Create is strict.** `POST /functions` and `POST /containers` fail with `409` if a workload
named `{name}` already exists in the group's namespace in any region. It is not a silent
upsert; changes go through `PUT`.

**`PUT` is a full replace** of the mutable spec, and `404`s if the workload does not exist.
The body is the complete desired state:

- Non-secret fields are **required on update exactly as on create**: `image` for a container,
  `gitRepo` and `runtime` for a function.
- An omitted optional field returns to its default rather than keeping what is deployed:
  `port` to 8080, `revision` to `main`, `version` to the platform default.
- **Only redacted secret material is keep-on-omit**, because only it cannot be read back: the
  git/registry token and secret `env`/`files` values.
- Function build inputs are part of `PUT`: changing `gitRepo`/`revision`/`path`/`runtime`/
  `version` rebuilds from source using the stored `gitToken`. To rebuild the *same* definition,
  use `POST .../functions/{name}/build`.

**Typed endpoints are offering-scoped.** `/functions/{name}` acts only on a function and
`/containers/{name}` only on a container; a name that is the other offering returns `404`. The
OpenShift object name is `{name}`; the offering is a label, not part of the name.

### Shared sub-schemas

```jsonc
// Workload shared fields (used by both functions and containers)
// The acting group is NOT a body field - it is the {group} path segment on every
// endpoint (/api/serverless/v1/groups/{group}/...). The caller must be a member (else 403).
{
  "name": "orders-api",                 // DNS-1123, required. OpenShift object name is {name}.
  "hostname": "orders",                 // optional custom host; default {name}-{group}.{route_domain}.
                                        // a single label, or one level under {route_domain}
                                        // ({label}.{route_domain}); must not be assigned (else 409).
  "env": [                              // optional; each entry is name + value
    // `name` follows Kubernetes' own env-var rule: letters, digits, '-', '_', '.',
    // not starting with a digit. It doubles as the Secret key for a secret var.
    { "name": "LOG_LEVEL", "value": "info" },                       // inline
    { "name": "DB_PASSWORD", "value": "s3cret", "secret": true }    // -> API-created Secret {workload}-env
  ],
  "files": [                            // optional: inline files to mount
    // non-secret files -> one {workload}-files ConfigMap; secret files -> one {workload}-files Secret
    // `mountPath` must be non-empty and carry no ':' or '..' segment.
    // `encoding` defaults to "text" (the string IS the file); "base64" carries binary bytes.
    { "mountPath": "/etc/app/app.yaml", "content": "log_level: info\n", "secret": false },
    { "mountPath": "/etc/tls/tls.key",  "content": "<base64>", "encoding": "base64", "secret": true }
  ],
  "scaling": {                          // optional
    "minScale": 0, "maxScale": 3,
    "metric": "concurrency",            // concurrency | rps | cpu | memory
    "target": 100
  },
  "size": "small",                      // optional; small | medium | large (default small)
  "regions": ["central", "south"]       // optional; default = all regions (HA)
}
```

These fields apply identically to both offerings, modeled on the KSVC pod spec.

#### Environment variables

A plain `env` entry is set inline on the container. An entry with `secret: true` has its value
moved into an API-created Secret (`{workload}-env`) and read via a `secretKeyRef`; the value is
never inline. The API does not expose `valueFrom`, so users cannot reference arbitrary existing
cluster Secrets or ConfigMaps.

CA-trust defaults (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`,
`NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO`) are injected automatically, pointed at the mounted
trusted-CA bundle, so cross-language tooling trusts internal TLS with no user action. A
variable the user sets themselves wins. The injected defaults are recorded in a
`serverless.platform/injected-env` annotation and hidden from the workload's GET, so the GET
body can be sent back unchanged.

#### Files (config & secret mounts)

Mounted files are always read-only: Kubernetes mounts ConfigMap and Secret volumes read-only
regardless of the pod spec, so the API offers no flag it could not honor.

The API aggregates all non-secret files into one `{workload}-files` ConfigMap and all secret
files (`secret: true`) into one `{workload}-files` Secret - one of each per workload, a key
per file - and mounts each at its path via `subPath`. Pre-existing cluster objects cannot be
referenced.

#### Scaling options

`scaling` maps to the Knative autoscaling annotations `autoscaling.knative.dev/min-scale`,
`max-scale`, `metric`, `target` and `scale-down-delay`.

| Field | Rules |
|---|---|
| `metric` | `concurrency` or `rps` (default KPA autoscaler, scale-to-zero capable), or `cpu`/`memory` (HPA class, no scale-to-zero). |
| `target` | Target value for the chosen metric. Default is metric-aware: `100` for `concurrency`/`rps`, `70` for `cpu`/`memory`. The latter are a utilization percentage, so values above 100 are rejected. |
| `minScale` | `0` enables scale-to-zero, the default, on KPA metrics only. |
| `scaleDownDelay` | Optional Go duration (`30s`/`5m`/`1h`, capped by Knative at 1h) holding a revision up before scaling it down. |

These rules are surfaced verbatim on the per-offering `/info` (per-metric `minScaleFloor`,
target default/min/max/unit), derived from the same model that validates a create.

#### Resource size

`size: small|medium|large` (default `small`) is a t-shirt size, so clients pick capacity
without Kubernetes units.

| Size | CPU request | Memory request = limit |
|---|---|---|
| `small` | 100m | 256Mi |
| `medium` | 250m | 512Mi |
| `large` | 500m | 1Gi |

Memory is set `request == limit`: a hard, predictable OOM boundary, and exceeding it restarts
that replica. CPU is request-only, so workloads are never CPU-throttled. The request is also
what lets the `cpu`/`memory` autoscaling metrics compute utilization.

### ServiceNow integration (frontend)

- **Forward the end-user token.** ServiceNow obtains the user's SSO access token
  (authorization-code / on-behalf-of) and sends it as `Authorization: Bearer`. The JWT carries
  the real user and `groups`, so group-based authorization works unchanged and actions are
  attributed to the actual requester. Register ServiceNow as an OAuth client of the SSO whose
  tokens carry `aud = serverless-api`.
- **CORS.** For a Service Portal widget calling the API from the browser, set
  `SERVERLESS_CORS_ALLOW_ORIGINS` (Helm `corsAllowOrigins`) to the ServiceNow instance
  origin(s); the API enables CORS (preflight + `Authorization` header) only then. Server-side
  calls (IntegrationHub / Scripted REST) need no CORS, and serving the API from the portal's
  host under `basePath` makes it same-origin.
- **Async submit + poll.** `202` with a `statusUrl` avoids ServiceNow REST timeouts on slow
  FaaS builds and matches its long-running-task patterns.

## How a request is handled

A create or update runs in this order:

1. **Authenticate and authorize.** The bearer token is validated; the `{group}` path segment
   is normalized and checked against the caller's groups.
2. **Validate at the edge.** Schema and validators run in the request layer.
3. **Provision the namespace.** `provision_namespace` is called from the engine
   (TENANT-CONTROLLER.md: Tenant Namespaces), not from `preflight`, which is pure cluster
   probes.
4. **Pre-flight.** Host and name availability are checked in one fan-out.
5. **Accept.** `202` with `status: "Pending"` and a `statusUrl`.
6. **Fan out.** The background deploy applies to every region concurrently, re-running the
   pre-flight immediately before it mutates.
7. **Roll up.** Reads merge the per-region results into one workload status.

### Validation at the edge

- **A pod name from the URL path** is constrained to Kubernetes' own rule
  (`validate_pod_name`): it is the one caller-supplied string on a read path that could reach
  past the resource it names. Authorization is separate (STREAMING.md: Authorizing a pod).
- **Env var names** are validated here, so a name the API server would refuse is a `400`, not
  a `202` that dies in the background apply. The 253-character cap comes from the
  `{workload}-env` Secret key; Kubernetes puts no limit on the name itself.
- **The JSON schema documents but does not validate.** The `Annotated` types carry
  `WithJsonSchema`, not a `pattern`: a pattern runs before the `AfterValidator` and would
  reject `My_Team` before `normalize_group` canonicalizes it, or `/src` before
  `validate_source_path` strips it. The validators live in `common`, are re-exported by
  `api.models.common`, and are the only authority.
- **Base64 is checked twice.** `FileMount` rejects undecodable content at the HTTP edge,
  because the accept path echoes the spec back through `describe.redact_files` before
  service-layer validation runs. The lenient decode in `resolve_files` covers non-HTTP callers.
- **The stream path pattern is compiled per call.** `validate_stream_path`
  (`api/models/stream.py`) enumerates the paths a ticket may be minted for, building its
  pattern from the base path each call because the base path is configuration.

### Inside the engine

- `assert_*`, `host_for` and `validate_spec` are engine methods delegating to
  `api.services.regions.preflight`.
- `WorkloadService.namespace_for` is the only place a group becomes a namespace, deferring to
  the shared `TenantNamespaceConfig.namespace_for`. `Deployer.resolve_targets` binds that
  namespace into every cluster view for the request; registries are read off those resolved
  clusters (`target_registries`), not from settings a second time.
- `retag_build` compares the stored tag first: one GET per region per write, and a registry
  delete only when a tag really moved.
- `DeleteContext` is one value, not a `(cluster, object name)` pair: the registry addresses a
  function by `{group}/{name}`.
- `FunctionService._assert_runtime` checks a runtime against the kpack `ClusterBuilder` it
  names, not merely the ConfigMap entry, so a mounted-ConfigMap problem is an immediate `400`
  rather than a failed background deploy. Its runtime registry is a constructor argument.
- `FunctionOffering.build_states` reads a whole group's build states in one label-selected read.
- `ContainerService.pull` logs a pull that failed in every region: the `202` is long gone, so
  the client would otherwise see a `statusUrl` that never shows a new revision. `accept_pull`
  derives the host when the stored annotation is missing.
- Everything in `api.services.state` is pure - it takes objects the caller already fetched and
  returns a value. Ownership is a predicate, kept apart from `ksvc_state`.

### Names and manifests

The host and object-name conventions are in ARCHITECTURE.md: Networking & Exposure.

- Platform-wide name and group rules live in `cloudlet_apis.names`; `common.names` re-exports
  them and adds what this platform derives - object names, image and cache repositories, the
  OCI tag projected from a revision, the validators. `DNS1123` is imported, not redeclared.
- Label values are sanitized with an ASCII test rather than the Unicode-aware `isalnum()`,
  since a label value is ASCII `[A-Za-z0-9._-]` (`common.labels._sanitize`). ConfigMap and
  Secret keys go through the same rule (`manifests.files._key`), and colliding keys are
  **rejected, not merged**: merging would mount one file's content at another's path.
- The offering label values (`OFFERING_FUNCTION`, `OFFERING_CONTAINER`) sit in `common.labels`
  beside the label key - the same string is the label, the kind in the URL path and the
  response `type`.
- `resources.build_configmap` splits text and binary fields itself: a non-UTF-8 byte in `data`
  is rejected or corrupts the file. Both mount identically.
- `summaries.merge` lists the object name verbatim and never strips a `-{group}` suffix, so a
  GET of what was listed resolves. A workload with no stored port reads as `8080`, the port
  Knative injects when none is declared (`build_ksvc` still honors `None`). Injected CA-trust
  variables are hidden from that read.

## Multi-Region (Active/Active HA) Design

The platform deploys **every** workload to **both** OpenShift clusters on each create and
update, and the API itself runs active/active on both. Both clusters trust the same CA and a
workload's Route host is identical in both, so each region is a full, independent replica; a
DNS record forwards end-user traffic to the active region.

The client certificate, CA bundle and tenant-namespace suffix are **global**, so a region
profile is just its name and its cluster. A workload's namespace is derived the same way in
both: `{group}{tenantNamespaces.suffix}`.

```yaml
baseDomain: example.com                   # each region's API server derives from this
routeDomain: serverless.{base_domain}     # shared; same host in both clusters
tenantNamespaces:                         # a group's workloads live in {group}{suffix}
  suffix: -serverless                     # global; read by the API and the tenant controller
clientCertDir: /etc/serverless/client     # tls.crt/tls.key (cert-manager), global
caBundle:                                 # OpenShift-injected, global
  configMap: ca-bundle
  key: ca-bundle.crt
  mountPath: /etc/ssl/certs
regions:
  - name: central                          # region/region
    cluster: central-0                     # cluster instance
  - name: south
    cluster: south-0
```

**There is no per-region `apiServer`.** Each region's endpoint is composed as
`https://api.{cluster}.{baseDomain}:6443` (`common.cluster.Cluster`), so the cluster name is
the only thing that varies and a region cannot be pointed at an endpoint that contradicts its
name. `local_region` names the region this instance sits in, matched on the region name first,
then the cluster name.

The API always authenticates with the **client certificate**, local cluster or peer alike;
there is no in-cluster/ServiceAccount path. Because `regions` carries no secrets, it can be
sourced from a ConfigMap.

### Fan-out & status aggregation

The API holds **one Kubernetes client per region**, built from that region's client cert and
the shared CA. On deploy it applies the KSVC and Route to both regions **concurrently**, then
aggregates the results. The `hostname` is the same in both; only readiness differs:

```json
{
  "name": "orders-api",
  "group": "team",
  "type": "container",
  "hostname": "orders-api-team.serverless.example.com",
  "regions": [
    { "region": "central", "status": "Ready", "revision": "orders-api-00001" },
    { "region": "south", "status": "Ready", "revision": "orders-api-00001" }
  ],
  "status": "Ready"
}
```

- **Admission is reserved for every target before any region starts** (`Deployer.fanout`,
  `ReadPool.reserve`), so a shed request does not leave running regions burning pool slots.
- **Per-region status comes from the apply response, not a re-read.** Server-side apply returns
  the stored object, which every derived resource's `ownerReference` is sourced from. Knative
  has not reconciled microseconds later, so a workload just written reading as `Deploying` is
  the right answer.
- **Results are returned, not written into shared state.** A per-region fetch returns a value
  (`_RegionRead`) and the fan-out keeps target order, so a region it gave up on is absent from
  the reads with only its timeout row present.
- **Per-region write ordering lives in `region_apply`.** Each constraint prevents a specific
  failure within one cluster: a stale Secret outliving the spec that dropped it, an orphaned
  resource whose owner was never applied, a half-built create holding a name. A failed
  build-object apply raises (`apply_build_objects`); `delete_build_objects` runs on every
  delete though it is normally a no-op, since it collects build objects applied unowned before
  builds followed the workload into its namespace.
- **Secret reads fail loud; decoration reads are best-effort.** `region_read.secret_data`
  turns a non-404 failure into a `503` rather than `{}`, which would make a valid "keep" look
  unset and silently lose a stored secret. Values are read as bytes - a secret file may hold a
  keystore or a DER certificate - and a value that is not valid UTF-8 is skipped. The
  revision, usage and spec reads degrade to a null field instead.
- **The usage read reports whether it happened** (`RegionUsage.measured`): a total summed over
  a region that did not answer is a smaller number that still looks authoritative. The quantity
  parse sits inside the same guard, so an unrecognized quantity form cannot turn a healthy
  workload into a `Failed` region.

### Pre-flight: what is checked before a write

- **Host and name are checked in one fan-out.** `preflight.assert_deployable` answers "is this
  host free" and "is this name unused" in one visit per region, so a region's two answers
  cannot disagree about the moment they were taken.
- **The check is not atomic with the apply.** A peer can claim the host in between, so
  `apply_workload` re-runs it immediately before mutating.
- **A host conflict is reported before a name conflict**, because an idempotent apply would
  otherwise resolve it silently by hijacking another workload's `DomainMapping`.
- **The host owner is found with a field selector** (`_host_owner`), and the conflict message
  names the holder and its namespace - the owner can live in a namespace the caller cannot see.
- **A default host label that does not fit is a `400` that suggests a `hostname`.** The check
  is in `preflight.resolve_host` and runs on the create path only; `route.host_for` composes
  the host without it, because read paths recompute a default host for existing workloads.

### Partial-failure semantics

Create and update are **asynchronous**. The pre-flight runs synchronously and the call returns
`202` with `status: "Pending"`, so the outcome of the fan-out is observed by polling
`GET {statusUrl}` (or `/stats`), not from the status code of the write.

| Scenario | What the poll reports |
|----------|-----------------------|
| Every region succeeds | `status = Ready`. A mixed `Ready` + `Deploying` is a normal rollout with one region ahead, **not** a failure. |
| One region fails | `status = Failed`; that region's entry in `regions[]` carries the `reason`/`message` pair. The succeeded region is **left running** (HA prefers availability), and DNS keeps serving from the healthy region. |
| Every region fails | `status = Failed` with an error on every region. The background deploy raises `REGION_TOTAL_FAILURE` internally; it is logged with the request id rather than returned, because the caller already holds a `202`. |

Re-apply is idempotent (server-side apply by object name), so a retry heals any partial state.

The **synchronous** read paths surface these as status codes instead. A listing whose every
region is unreachable is a `502 REGION_TOTAL_FAILURE` with the per-region errors in
`details[]`. A single `GET`/`DELETE` that cannot confirm a workload's absence because a region
was unreachable is a `503`, not a misleading `404`: a missing answer is not evidence of
absence.

**An unavailable region does not freeze the API.** Per-region work runs concurrently in
threads; every cluster call has a connect/read timeout and each region an overall operation
timeout backstop, so a down or slow region fails fast, is reported as `Timeout`/`Failed`, and
blocks neither the healthy region nor other requests. Health probes never touch clusters.
(See `cluster_connect_timeout`, `cluster_read_timeout`, `cluster_op_timeout`,
`cluster_read_op_timeout`.)

**Every region builds what it runs**, into its own registry, and publishes only to itself
(FUNCTIONS.md: FaaS - Function as a Service). The two regions run the same *commit*, not the
same digest: builds are not bit-reproducible, and the independence is what a switchover needs
(BUILDING.md: Active/Active Behaviour).

Cross-region traffic steering is the `*.serverless.{base_domain}` DNS record forwarding to the
active region, not the API.

### The read pool

**Cluster reads run on a bounded pool of their own.** `api.services.regions.read_pool.ReadPool`
is sized from `cluster_read_workers`, and admission past
`cluster_read_workers + cluster_read_max_queued` is refused with a `503`. On the process-wide
default executor a burst of page reads would make unrelated requests inherit its latency
invisibly. `ReadPoolSaturated` marks the API's own saturation (this request's `503`); a
`ServiceUnavailableError` from a region function is that region's failure row.

**A slot is released when the thread finishes, never on cancellation.** The executor cannot
interrupt a thread, so a read the caller's `wait_for` gave up on still occupies its worker.
The release hangs on the concurrent future's done callback rather than on the asyncio wrapper,
which acknowledges a cancel immediately even while the thread runs on.

### The per-region cluster client

- **A `Cluster` is a cluster, not a namespace.** Every namespaced operation on
  `common.cluster.client.Cluster` names its namespace explicitly; code working within one
  namespace binds it once through `NamespacedCluster`.
- **Lazy clients are built under a lock.** Unguarded, two fan-out threads each build an
  `ApiClient`, the loser's connection pool is never closed and the dynamic client races on its
  discovery cache. Callers may `connect()` eagerly at startup.
- **The connect timeout is installed by wrapping `pool_manager.request`.**
  `connection_pool_kw["timeout"]` does not work: urllib3 consults the pool default only for
  its own sentinel, and `kubernetes.client.rest` always passes `timeout=` explicitly - `None`
  for a call with no `_request_timeout`, which is no timeout at all. Discovery and the watch
  are exactly those calls (`common.cluster.pool._default_connect_timeout`).
- **Streams carry no read timeout; keepalive bounds them.** A watch and a log follow are idle
  between bytes by design. TCP keepalive options on every pool make the kernel probe an idle
  connection after 30s and give up within about a minute more - the only defense against a
  connection that dies with no RST. They are added to urllib3's defaults, not substituted,
  because those defaults carry `TCP_NODELAY`.
- **Closing a follow closes the socket and releases the connection.** `LogFollow.close` calls
  both: the close interrupts the pending read on the follower thread (STREAMING.md: A
  held-open stream holds a thread), the release stops the pool holding a dead connection.
- **Field selectors are applied by the apiserver**, so the host pre-flight's cluster-wide
  question stays one narrow query (`Cluster.get(field_selector=...)`).

## Authentication & Authorization

Two distinct identities are involved:

1. **End-user → API:** OIDC bearer token from SSO.
2. **API → each cluster:** client TLS certificate issued by cert-manager (ACME), whose CN is
   the DNS name `serverless-api.clients.{base_domain}`. That name is the Kubernetes user,
   bound by RBAC.

### End-user authentication (SSO OIDC)

The API is a **resource server**. It registers no OAuth client of its own to serve traffic and
validates JWTs offline against JWKS fetched from the internal SSO realm and cached, so there
is no per-request round trip.

1. The client authenticates against SSO - authorization-code flow for users, client
   credentials for machines and service accounts - and receives a JWT carrying a `groups` claim.
2. It calls the API with `Authorization: Bearer <JWT>`.
3. The API verifies the signature, `iss`, `aud` and `exp`/`nbf` against the cached JWKS.
4. It reads the `groups` claim and authorizes. Not a member of the required group: `403`.

#### The Swagger "Authorize" client (and realms that forbid public clients)

The one OAuth client the platform needs is for the interactive docs. Swagger UI's "Authorize"
button logs in with Authorization Code + PKCE and needs no secret, which makes it a **public**
client. Where the SSO realm forbids public clients, set
`SERVERLESS_SSO__SWAGGER_CLIENT_SECRET` (Vault → ESO, `swagger-client-secret`): the browser
still runs the authorization leg against SSO with PKCE, but posts the code to
`POST /api/serverless/auth/token` on the API, which adds the secret and completes the exchange
server-side, so the client can be registered confidential and the secret never reaches a
browser. Unset, the public-client flow is used unchanged. That endpoint is unauthenticated by
necessity, so it only ever completes a login: `authorization_code` and `refresh_token` are the
only grants forwarded, and the client id and secret come from configuration.

Register the client with Standard Flow on, PKCE required (`S256`), and Service Accounts and
Direct Access Grants off. **The redirect URI carries the base path**: Swagger's callback is
`https://{host}{basePath}/docs/oauth2-redirect`, so the client's valid redirect URIs must list
that exact path, and changing `basePath` moves it. Registering the old one, or only the host,
fails the login with `invalid_redirect_uri` after the user has already authenticated.

This keeps the **client secret** server-side. The user's own tokens still reach the browser,
since Swagger UI calls the API with them; it is not a BFF holding tokens in a session.

#### The git webhook's token (per function, non-OIDC)

One endpoint takes a third credential. `POST .../functions/{name}/build` accepts
`X-Gitlab-Token` in place of `Authorization`, compared in constant time against the token
stored for the function the path names (FUNCTIONS.md: Git webhook). It is *not* a way into
the rest of the API: it authorizes one build of one function, the group check still runs
against that function's own labels, and a valid bearer takes precedence where both are
sent. Every failure before the token matches - a wrong token, or no such function - is a
`401`, so the endpoint cannot be used to discover what a group has.

#### Static API keys (admin/operator automation, non-OIDC)

For admin automation that cannot do OIDC, the API also accepts a **static admin API key** in
the same `Authorization: Bearer <key>` header. It tells the two apart by shape: a structural
JWT (`header.payload.signature`) is validated as an OIDC token, an opaque token is compared
against the single configured admin key.

The key is the raw token, not a hash, sourced from Vault via ESO into
`SERVERLESS_ADMIN_API_KEY` and matched with a constant-time compare
(`cloudlet_apis.auth.verify_admin_key`). A match yields an **admin** Principal; the key is
admin-only, and regular users go through OIDC. It defaults to empty, which **disables** key
auth; set the env var to enable it.

#### Auth as a shared library (not a separate microservice)

All OIDC interaction is encapsulated in a component the API imports (`cloudlet_apis.auth`,
from the shared `cloudlet-apis` package), not a separately-deployed microservice. Token
validation is stateless, so there is no shared state to centralize. The component owns:

- SSO OIDC discovery, JWKS fetch/cache and token validation (`TokenValidator`);
- claims → group mapping and admin/tenant policy (`principal_from_claims`, `Principal`);
- **stream tickets** (`StreamTickets`), the short-lived signed credential a browser opens an
  SSE endpoint with (STREAMING.md: Browsers cannot send an `Authorization` header);
- the `SSOAuth.require_auth` dependency and the `CurrentUser` annotation the routers use.
  Per-group authorization is asserted in the service layer (`assert_group`).

It is a library rather than a copy in each API for the same reason group names are normalized
in one place: two APIs disagreeing about which groups a token carries is an authorization bug.
What stays in this repository is `api/auth/deps.py` - which of this service's settings the
component is built from - and the SSO defaults in `api/core/config.py`. The shared package
requires an issuer rather than defaulting to one, so the value deciding whose signatures we
trust is a deliberate choice here. If auth-at-the-edge is ever wanted, the OpenShift-native
drop-ins are **oauth2-proxy** or **Authorino** as a sidecar/gateway - an infra change, not an
API rewrite (ARCHITECTURE.md: Open Questions / Future Work).

### Group-based authorization (tenancy)

Tenancy is a namespace per group (TENANT-CONTROLLER.md: Tenant Namespaces), and every resource
the API creates is labeled as well, so ownership is legible inside a namespace and selectable
across regions:

```yaml
metadata:
  labels:
    serverless.platform/group: "<keycloak-group>"
    serverless.platform/managed-by: "serverless-api"
    serverless.platform/owner: "<sub or preferred_username>"
    # every resource created for a function/container also carries the workload name:
    serverless.platform/workload: "<function-or-container-name>"
```

Every resource created for a function or container - KSVC, Route, DomainMapping, the
`{workload}-env` Secret, the `{workload}-files` ConfigMap/Secret, and the imagePullSecret -
carries **both** the group label and the workload-name label.

**The caller explicitly chooses the group to act as on every request.** It is a path segment
(`/api/serverless/v1/groups/{group}/...`) on every endpoint, never a body field, so the acting
group is unambiguous for users in several groups. The API asserts the caller is a member of
that group from the `groups` claim; otherwise `403`.

- **Create/Update:** the workload is named `{name}` in the group's namespace and stamped with
  that group label.
- **Read/Delete by name:** the API verifies both that the caller is a member of `group` and
  that the resource's group label matches, else `403`/`404`.
- Admins (members of a configured **admin group**) may act for any group; tenant groups are
  limited to groups the caller belongs to.

**Group-name normalization** happens in one place (`normalize_group`), applied at both edges -
the `groups` claim and the `{group}` path segment - so the two are always comparable. In
order: strip the Keycloak path prefix (`/`), **lowercase**, strip a leading `ggd-<1-4 digits>-`,
fold `_` to `-`. Lowercasing precedes the prefix strip so an upper-case `GGD-1234-` is still
recognized. Case and `_` are legal in a Keycloak group but **not** in the DNS-1123 namespace
and host the group is interpolated into (`{group}{suffix}`, `{name}-{group}.{base_domain}`).
The DNS-1123 check runs on the **normalized** form, so what normalization cannot rescue - a
leading or trailing `_`, whitespace, non-ASCII - is still a `422`.

> Consequence: `My_Team`, `my_team` and `my-team` all name the **same** platform group. The API
> accepts any spelling in the path, and returns and deploys the lowercase hyphenated form. If a
> realm ever defines two of these as *distinct* groups they would collapse into one tenant, so
> the realm must not treat `_`/`-` or case variants as separate groups (confirmed with the SSO
> team). Configured admin groups are normalized the same way.

Isolation is enforced by the **namespace boundary**, with the group checks and ownership labels
as defense in depth. Admin listings and the host pre-flight still read cluster-wide, by label.

### Cluster-side identity (cert-manager client cert + RBAC)

- The Helm chart ships a cert-manager **`Certificate`** per region, issued via ACME (an
  internal ACME endpoint in airgap). ACME requires a DNS-name identity, so the cert's CN/SAN is
  `serverless-api.clients.{base_domain}` - and that DNS name is the **Kubernetes user**. Both
  clusters trust the same CA, so the identity is valid in either.
- The API's rights are one **ClusterRole** of least-privilege CRUD on exactly what it manages,
  bound into each tenant namespace by a RoleBinding from the tenant template set, so writes
  stay namespace-bound. A read-only ClusterRole covers the cluster-wide reads: host pre-flight,
  admin listings, and the build controller's Image watch. Covered: Knative
  `services`/`domainmappings`, `secrets`, `configmaps`, read on `pods`/`events`, and read on
  the **`pods/log`** subresource (for `/logs`). The API does **not** need `routes` permission -
  on OpenShift Serverless the operator creates the Route from the KSVC/DomainMapping.
- The cert is mounted once, global rather than per-region, at `SERVERLESS_CLIENT_CERT_DIR`
  (`tls.crt`/`tls.key`). There is no in-cluster/ServiceAccount fallback.
- The CA used to verify the API servers is the trusted CA bundle (ARCHITECTURE.md: Airgapped
  Considerations), pointed at by `SERVERLESS_CA_BUNDLE__*`, the same for every cluster.

## Error model

Standard envelope for all non-2xx responses:

```json
{
  "error": {
    "status": 502,
    "code": "REGION_TOTAL_FAILURE",
    "message": "Deployment failed in all regions.",
    "details": [
      { "region": "central", "message": "registry auth failed" },
      { "region": "south", "message": "registry auth failed" }
    ],
    "requestId": "b1c2..."
  }
}
```

A **partial** failure is not an error envelope. `207` returns the normal workload body with
`status: Failed` and the failing region's message on its per-region object (*Partial-failure
semantics*). A poller therefore parses one shape for `200`/`202`/`207` and switches to the
envelope only on a genuine non-2xx.

`status` is the numeric HTTP status, also on the response line; `code` is the machine-readable
string. Framework HTTP errors that are not domain errors - an unknown route, a method not
allowed - derive their `code` from the status name (`404` → `NOT_FOUND`, `405` →
`METHOD_NOT_ALLOWED`).

`requestId` is a per-request correlation id. The API **adopts the inbound `X-Request-ID`** -
OpenShift's router stamps one, so the id lines up with the router and ingress logs end to end -
and mints a UUID when it is absent or malformed. It is echoed in the `X-Request-ID` response
header on every response, success and error, and bound into the server logs.

This table is the authoritative prose, but a client should read `errorCodes` off
`/api/serverless/v1/{containers,functions}/info` rather than embed it: that document is walked
off the `APIError` subclasses in code, so it cannot go stale the way this can.

| HTTP | Code | When |
|------|------|------|
| `400` | `VALIDATION_ERROR` | Bad/missing fields, unsupported runtime, a rebuild with no stored token. |
| `401` | `UNAUTHENTICATED` | No credential, an invalid one - or, on the webhook path, a wrong token or a function that does not exist (deliberately indistinguishable). |
| `401` | `UNAUTHENTICATED` | Missing/invalid JWT, or an unrecognized bearer token. |
| `403` | `FORBIDDEN` | Caller not in a required/owning group, or a valid token carrying no groups. |
| `404` | `NOT_FOUND` | Workload not found in caller's group scope. A workload the caller may not see, or one of the *other* offering, is hidden as a 404 rather than a 403. |
| `405` | `METHOD_NOT_ALLOWED` | Path exists but not for that HTTP method. |
| `409` | `CONFLICT` | Name already exists for the group, or the requested `hostname` is already assigned. |
| `422` | `VALIDATION_ERROR` | Request body/path failed schema validation (FastAPI's own, rendered into the same envelope). |
| `500` | `INTERNAL` | Unexpected error. The message is a fixed string - an exception's own text routinely carries internal hostnames or secret material - so the detail is in the log, under the same `requestId`. |
| `502` | `REGION_TOTAL_FAILURE` | Every region failed (a listing whose regions were all unreachable). |
| `503` | `SERVICE_UNAVAILABLE` | A check could not be *run*, so it has not passed: a region was unreachable during a host/absence pre-flight, a delete could not be confirmed, or a stored secret could not be read back to preserve a "keep". Fail-closed by design - retry. Also: the stream pool is full, or stream tickets are not configured. |
