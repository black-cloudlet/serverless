# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Live observability is now Server-Sent Events, and logs are per pod.** Three
  endpoints: `GET .../{name}/pods` (the workload's pods on the current site),
  `GET .../{name}/logs/pods/{pod}` (follow one pod's log), and
  `GET .../{name}/stats/stream` (the existing `/stats` body, pushed). The first
  two **stream by default** and answer once under `?follow=false`; `/stats` keeps
  its JSON form as the cheap poll target. Streaming was designed but deliberately not built
  (docs/ARCHITECTURE.md - Open Questions), because three things had to be
  answered first; each is why a piece of this looks the way it does.

  **BREAKING: `GET .../{name}/logs` is gone**, replaced by
  `GET .../{name}/logs/pods/{pod}` - the same read, aimed at one pod. The
  workload-level follow is gone too: it has to reconcile a *set* of pods that
  changes underneath it, which forced a per-stream pod cap and an arbitrary rule
  for which pods win when a workload is wider than it. Per pod, a stream is one
  pod, one thread, nothing to reconcile, and the client can say "just the noisy
  one". The cost is that the client must learn a pod name first, which is what
  `/pods` is for: until now nothing in the API returned one, and the only way to
  find out was to read every pod's logs.

  **`?follow=false`** on both endpoints answers once, in JSON, and ends. Not a
  convenience: it is the only form available to a caller that cannot hold a
  connection open, and the architecture has one - a ServiceNow workflow attaching
  a failing function's logs to a ticket cannot consume an event stream. It is on
  both deliberately, because a log snapshot alone would be unreachable: finding a
  pod name would still require opening a stream. The snapshot returns the same
  lines a follow would have delivered, so a client renders one shape either way,
  and it takes **no stream slot** - it ends, so rationing it against the pool that
  bounds held-open connections would let streams throttle a caller that is not
  holding one. What it cannot skip is authorization: both forms go through one
  `_pod_authorizer`, so `follow=false` is not a way around the pod-ownership
  check. What a snapshot can return is still bounded by what the node holds -
  Kubernetes keeps no ring buffer beyond its rotated file - so it is the recent
  past, never the whole history, which is the same limit a follow starts from.

  Streaming is the **default** on both because the answer expires - Knative
  replaces a workload's pods on every revision and removes them all on
  scale-to-zero - so a roster fetched once quietly stops being true. `/pods` reports
  name, revision, phase, ready, restarts, startedAt and per-pod usage, joining
  the metrics API on by name; a pod too new to have been scraped is still listed
  with `usage: null`, because that is exactly the pod someone is most likely to
  want to follow. Both are local-site only, like the logs they feed.

  **A held-open stream holds a thread.** The Kubernetes client is synchronous, so
  a followed log is a thread blocked on a socket for as long as the client stays
  connected - not for the length of a request. Left on the default executor that
  `asyncio.to_thread` uses, a few idle log tails would sit on the threads every
  ordinary create, read and delete needs, and the API would stop answering while
  looking healthy. So streaming has a pool of its own and admission is capped
  before it can be exhausted: `stream.maxConcurrent` (32) streams, with the pool
  derived from that rather than configured, since one smaller than the admissions
  it must serve turns a bound into a stall. A stream past the limit is refused
  with 503 and a retry. What the bounds cost is reported, never hidden: lines
  dropped when a client reads slower than the pod logs arrive as a `warning`
  event carrying the count.

  **The router would have cut them.** OpenShift times a connection out after 30s
  by default, severing every stream half a minute in while the client reconnects
  forever without surfacing why. The chart sets
  `haproxy.router.openshift.io/timeout` from `api.route.timeout` (65m) and
  **fails to render** if it does not exceed `stream.maxSeconds` - the two live in
  different sections of `values.yaml`, so the relationship is asserted rather
  than left to whoever edits one. Streams end themselves at `maxSeconds` with an
  `end` event and heartbeat so nothing in the path reaps an idle one.

  **Browsers cannot send an `Authorization` header.** `EventSource` has no API
  for one, which leaves the credential in the URL - and the SSO token is the
  wrong thing to put there: valid against every endpoint, and a URL reaches the
  router's access log, our own log line and the user's history. So the token buys
  a ticket: `POST /api/v1/stream-tickets` spends it on a request that can carry a
  header and returns something worth almost nothing - one path, one minute, an
  identity the caller already had. Signed rather than stored, because two
  replicas serve behind one Route and either may take the stream. New secret
  `SERVERLESS_STREAM_TICKET_KEY`; empty disables minting the way an empty admin
  key disables key auth, and the header still works, so a curl follow needs no
  new configuration.

  Owning a workload is not owning every pod, and all workloads' pods share a
  namespace - so the log stream checks the KSVC's ownership labels **and** that
  the named pod carries this workload's service label. A pod that fails either is
  a 404 identical to one that does not exist, so the response never confirms that
  a pod by that name is running. The pod name is a path segment that reaches a
  request to the cluster's API server, so it is constrained at the edge to what
  Kubernetes itself accepts.

  Everything that can fail with a status code is settled before the response
  begins, so a missing workload is a 404 envelope and not a stream that opens and
  immediately errors; after the first byte the status line is spent and a failure
  arrives as an `error` event carrying the code `/info` already publishes. A pod's
  log *ending* is an `end` event, not an error - on Knative a scale-down or a new
  revision is routine, and a client that reddens for it reddens for a successful
  deploy. No new RBAC: `pods/log` and `pods` were already read.
- `env[].name` and `files[].mountPath` are validated at the edge, the last two
  caller-supplied strings that reached a cluster without a rule. An env name now
  follows Kubernetes' own `IsEnvVarName` (`[-._a-zA-Z][-._a-zA-Z0-9]*`), which is
  also what the `{workload}-env` Secret accepts as a key, since a secret var is
  stored under its own name; a mount path must be non-empty and carry no `:` or
  `..` segment. Both were previously accepted (202) and failed in the background
  apply as a per-site error about a field the caller never sees - the failure
  mode every other validator in `common/names.py` exists to prevent.
- `dev/runtimes.yaml`, so the local-development flow in the README works. The
  runtimes file is required and has no fallback, so `uvicorn api.main:app` had
  been exiting at startup with `RuntimeConfigError` unless the operator happened
  to have `/etc/serverless/runtimes/runtimes.yaml` on their machine.
  `.env.example` now points at it.
- **The build controller** (`controller/`, `python -m controller.main`), a second
  Deployment that closes the last gap in the build path: a finished build now
  reaches the running function. It watches kpack `Image` objects in the local
  cluster and applies each `status.latestImage` to that function's Knative
  Service in *every* site. Nothing in a request/response path could do this -
  `STACK` and `BUILDPACK` rebuilds (the CVE patches kpack was chosen for) fire
  with nobody asking - which is why it is a loop and not an endpoint.

  One pass is a full relist followed by a watch resumed from it, so a dropped
  stream or an expired `resourceVersion` costs one extra relist rather than a
  function stuck on an old digest; `buildController.resyncSeconds` (300) is both
  the watch's lifetime and the relist interval, because they are the same
  number. It composes no KSVC - the API owns that spec and the controller owns
  one field of it - so it applies the live object with the image replaced,
  stripped of the metadata the server owns and of any pinned revision name.
  Two conditions stop a write: the digest is already deployed, or the workload is
  not a function (a container that reused a deleted function's name must not
  inherit its image). No leader election, by the same convergence rules as every
  other writer.

  It ships as **its own image** (`Dockerfile.controller`), installing only the
  base dependencies - `pydantic`, `pydantic-settings`, `kubernetes`. The API's
  `fastapi`, `uvicorn`, `httpx` and `pyjwt[crypto]` moved behind a new `api`
  extra that only its image installs. The controller holds a certificate that
  can write every site's Knative Services and now cannot load a web framework or
  `cryptography` at all: ~23 MB it never imported, and the steadiest source of
  advisories, against a pod with no HTTP surface. CI proves it - it imports each
  service out of its own image and asserts the controller's ships none of them.
  Both images are built from one tag by the release job, so they cannot disagree
  about `common/`. Its RBAC is the API's client certificate and Role, whose
  verbs it uses a subset of.

  Each resync also **prunes the `Image` objects a switchover stranded** in the
  other sites. They keep firing `STACK`/`BUILDPACK` rebuilds for a function the
  site no longer builds for, and since builds are not bit-reproducible they push
  a different digest from the same source - so both sites' controllers would
  publish and each swap would roll a revision of identical code. The newer
  `creationTimestamp` wins, so exactly one site prunes and the two can never
  delete each other's; a tie or an unreadable timestamp prunes nothing. It
  deletes outward rather than deleting its own on losing, because the stranded
  site is the one that may be down. A site that cannot be listed stops the whole
  pass - deciding what is stranded from a partial view is how a transient read
  failure deletes every live build. `buildController.pruneOrphans: false` turns
  it off.
- `POST /api/v1/groups/{group}/containers/{name}/pull` - re-resolve the image
  tag, with no request body. Knative pins a revision to the digest it resolved
  when the revision was created, so re-pushing `orders-api:1.4.2` changed
  nothing: a `PUT` with the same image produced no new revision, and
  `imagePullPolicy: Always` would re-pull the digest the Deployment is already
  pinned to. The endpoint writes one annotation - on the pod template, which is
  what Knative diffs - so it cuts a revision that resolves the tag again, in
  every site. Nothing else about the workload changes.

  The same value is stamped on the KSVC's own metadata, and re-applied from
  there on every update: dropping it would itself be a template change and cut a
  second revision nobody asked for. A digest-pinned container is a `400` - there
  is nothing newer to pull - and functions have no such endpoint, since their
  digest reaches the workload through the build controller.
- Every kpack `Image` now carries an explicit `successBuildHistoryLimit` /
  `failedBuildHistoryLimit`, from the new `build.history.success` /
  `build.history.failed` chart values (both **3**). Nothing set them before,
  which was not "unbounded" but kpack's own default of 10 and 10 - 20 `Build`
  objects per function, each holding a completed pod until it is collected. That
  is invisible at ten functions and is the whole namespace at three hundred.
  Failed builds keep their own quota because their pods are the only place the
  per-phase build log exists. The limits are a constant from configuration, so
  they converge like the rest of the spec; lowering them takes effect per
  function on its next build, since kpack prunes when it creates a `Build`.
- `POST /api/v1/groups/{group}/functions/{name}/build` - build a function's
  current source again, with no request body. Until now the only way to rebuild
  was to change a build input: a `PUT` re-applies the `Image` on every call, but
  an unchanged spec is a no-op kpack does not build from, so picking up a patched
  base image, retrying a failed build, or building a commit before kpack next
  re-resolves the branch meant editing something that did not need editing.

  Every input is read back off the workload - `gitRepo`/`branch`/`path`/
  `runtime`/`version` from the KSVC's annotations, the token from the
  `{workload}-git` Secret - which is the same reconstruction a site that has
  never built the function does after a switchover. Nothing is accepted from the
  request: a rebuild that took inputs would be a `PUT` in disguise. Missing
  stored inputs, no stored token, or a runtime that has since left the ConfigMap
  are synchronous `400`s; a container of the same name is a `404`, like every
  other read. The response is the same `Pending` 202 with the same `statusUrl`.

  Stored inputs that no longer validate - a hand-edited annotation, or a rule
  tightened since the function was created - are a `400` naming the fields,
  not the `500` the catch-all handler would otherwise render. Rebuild is the
  only path that builds a `BuildRequest` out of stored state rather than a
  validated request body, so it is the only one where that can happen.

  The trigger is an annotation on the latest kpack `Build`, never on the
  `Image`: kpack reads it there, and a nonce in the `Image` spec would look like
  a change to every apply (rebuilding forever under active/active) and would be
  dropped again by the next ordinary `PUT`, rebuilding once more. So the desired
  state stays a pure function of the function definition, and the `Image` apply
  that precedes the trigger keeps the path self-healing: a site that has never
  built the function gets the `Image` created and builds from that, with no
  `Build` to annotate. It is also the only function write that leaves the KSVC
  alone - the workload does not change, so no revision is spawned and the
  running one keeps serving until the new digest is rolled out. New RBAC: `patch`
  on `builds.kpack.io` (still never create or delete - kpack owns their
  lifecycle).
- `GET /api/v1/groups/{group}/{type}/{name}/stats` - a lightweight endpoint to
  poll for live numbers: `overallStatus`, workload-wide `replicas` and `usage`,
  and the same three per site. Nothing else. Until now a client watching a
  workload had to call the full GET, which also reads the file ConfigMaps and the
  backing Secret to rebuild the redacted spec - on a two-second refresh that
  pulls secret material out of the cluster on a loop for config that only changes
  when the client changes it.

  A function's build is still read even though it is not a field here, because
  that is what makes a running build `Building` instead of the `Degraded` its
  not-yet-pushed image would otherwise produce - on the rollup and on the per-site
  rows alike, matching the full GET. Usage sums each pod's user container only,
  never the queue-proxy sidecar. Totals are summed across sites **before**
  rounding, so they need not equal the sum of the printed per-site figures, and
  are `null` if any site could not be measured rather than quietly missing one.
  No new RBAC. Everything here is still polled; streaming is a separate follow-up
  (docs/ARCHITECTURE.md - Open Questions / Future Work), and `usage` is never
  fresher than the metrics-server scrape either way.
- Function builds cache their layers in the registry rather than in a
  PersistentVolumeClaim. Every kpack `Image` now carries an explicit
  `spec.cache.registry.tag` at `{base}/{group}/{name}_cache`; nothing set
  `spec.cache` before, so the choice fell to kpack's own default of a PVC per
  `Image` - storage that grew with the function count instead of with what was
  cached. New chart value `build.cache` (`registry`, the default, or `inherit`
  to write no cache spec). Existing `Image` objects pick the new spec up on
  their next apply; the first build after the switch is a cold one.
- Deleting a function now deletes its registry repositories - both the image
  repository and the `{name}_cache` one - instead of leaving them behind forever;
  nothing in the cluster owns registry content, so a KSVC delete never reached
  them. It uses Quay's management API (`DELETE /api/v1/repository/{ns}/{repo}`),
  which removes the repository itself rather than only its manifests, and so
  needs a Quay OAuth token with `repo:admin` - robot accounts are registry
  credentials and cannot call `/api/v1`. Note how Quay scopes that token: it acts
  as the user who authorized it, and with `registry.organization` empty each group
  is its own Quay namespace, so that user needs admin on each. Both paths are
  derived from config and the validated group/name, never from request input, and
  only the function offering is touched. Best-effort: a registry that refuses
  never fails the delete. New chart value `registry.deleteOnFunctionDelete`
  (default true) and an `ExternalSecret` delivering the token to the API
  namespace - **without that secret the step is skipped**, so an install that does
  not wire it is unaffected. The repository half of an image reference is now a
  named rule in `common/names.py` (`image_repository` / `cache_repository`)
  beside `image_tag`, rather than an f-string inlined at each use, so the code
  that pushes to a repository and the code that deletes one cannot disagree.
- Functions can select a language version: `version` on create and update,
  validated against the runtime's advertised `versions` (the same list
  `GET /api/v1/functions/info` publishes) and reported back on GET. The list was
  published and mirrored but never consumable - every build used
  `defaultVersion`, so an airgapped install carried toolchains for versions
  nothing could ask for. A runtime that offers no choice (empty `versions`, or no
  `versionEnv`) rejects a supplied version rather than ignoring it.
- Container `image` is validated at the edge against the OCI distribution
  grammar (optional `registry[:port]/`, lowercase path components, optional
  `:tag` and/or `@sha256:...` digest). It was the one caller-supplied string that
  becomes a cluster identifier without a rule, so an empty or whitespace-padded
  reference was accepted (202) and only failed minutes later as a bare
  `ErrImagePull` on the revision.
- Binary file mounts. `contentBase64` exists so a caller can mount a keystore or
  a DER certificate; content is now carried as bytes end to end, and a non-secret
  file whose bytes are not UTF-8 is written to the ConfigMap's `binaryData`.
- Platform-info discovery is now split into two public per-offering endpoints,
  `GET /api/v1/containers/info` and `GET /api/v1/functions/info` (replacing the
  single `GET /api/v1/info`). Both return the shared options (`version`, `sites`,
  `sizes`, `scaling`, `routeDomain`, `defaultHostTemplate`) plus identical
  `port` rules (bounds and the applied default); the function document adds
  `runtimes`. The port rules are derived from the same constants the request
  validator uses, so they can't drift.
- Both offerings take a container `port` (1–65535, default `8080`), stamped as
  the container's `containerPort` so the queue-proxy routes to it, and read back
  on GET. One rule for both: nothing about an image's port depends on how it was
  produced. `8080` is the default because it is what Knative injects as `$PORT`
  when a container declares none, so it is the port a workload already ran on -
  it is now written into the manifest rather than left as a convention a client
  has to know. It is not a build input (`BuildRequest` carries no port), so
  changing it costs a revision, not a rebuild.

  Sending a port matters only when the image serves on a different one: nothing
  can detect that, so the mismatch surfaces as a revision that never becomes
  ready, with the cause on the per-site `error`, rather than as a rejected
  request. That is the trade for not requiring the field.
- **Breaking:** workload update (`PUT`) is now a true full replace for both
  offerings - the body is the complete desired state. For containers, `image` is
  required on update just like on create, and an omitted `port` returns to the
  default rather than keeping the deployed one. For functions, the build
  inputs `gitRepo` and `runtime` are required and `branch` resets to `main` when
  omitted; they no longer carry forward from the deployed workload. In both cases
  the only keep-on-omit is redacted secret material that can't be read back to
  re-send - the registry/git token and secret env/file values. Functions still
  rebuild only when a build input actually changes or the token is rotated, so a
  config-only edit (that re-sends the same build inputs) keeps the current image.
- Request correlation: the error envelope's `requestId` is now populated (was
  always `null`). A middleware adopts an inbound `X-Request-ID` (e.g. from the
  OpenShift router) or mints a UUID, echoes it in the `X-Request-ID` response
  header on every response, and binds it into the server logs - so a `requestId`
  from an error body greps straight to that request's log lines.
- Every function/container now gets CA-trust env vars pointed at the mounted
  trusted-CA bundle so tooling trusts internal TLS out of the box:
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`,
  `GIT_SSL_CAINFO`. A var the caller sets themselves is left untouched (their
  value wins); the injected defaults are transparent - recorded in a
  `serverless.platform/injected-env` annotation and hidden from the workload's
  GET response.
- Configurable labels on the chart-created namespaces: `namespaces.labels`
  (applied to both) plus `namespaces.apiLabels` / `namespaces.workloadsLabels`
  (per-namespace, override the shared set) - e.g. to set
  `pod-security.kubernetes.io/enforce` or a `namespaceSelector` target.

- Functions accept `port` on the same terms as containers (see the port entry
  above). Previously a function's port was described as "the build's
  responsibility", but nothing in the build path ever set one - not
  `BuildRequest`, not the runtimes ConfigMap, not the kpack `Image` - so an app
  that did not read `$PORT` had no way to say so and simply never became ready.


- `build.scc` ships a least-privilege OpenShift `SecurityContextConstraints` for
  kpack build pods, off by default. A build pod runs as the builder image's CNB
  user and group - uid 1001, gid 1000 on the Paketo jammy images, which are not
  the same number - and `restricted-v2` allocates uids from the namespace's own
  range and rejects an explicit one outside it. With nothing else available the
  pod is refused at admission and the build never starts, per function rather
  than per install: a function build runs as the `fn-{workload}` account the API
  creates at request time. The SCC grants those two ids and nothing more, and
  carries no priority, so pods `restricted-v2` can admit are unaffected.
  `anyuid` would also have worked, and would also have permitted uid 0
  (docs/DEPLOYING.md - OpenShift SCC for builds).

### Changed

- **The parts every API on the platform repeats now come from `cloudlet-apis`.**
  The error envelope, logging, `X-Request-ID` correlation, the `/healthz` +
  `/readyz` + offline-docs wiring, the name/group rules and SSO auth moved into
  a shared package and are installed rather than vendored. Its extras mirror the
  split the two images already had: the API installs `cloudlet-apis[web,auth]`,
  the controller installs it bare and still ships no web stack -
  `tests/test_layering.py` now checks both, that no domain module reaches a web
  framework and that none reaches the `[auth]` dependencies either.

  What stayed is what is ours. `common/errors.py` re-exports the shared catalog
  and defines `SiteTotalFailure`, which is picked up by `error_catalog()` walking
  subclasses, so `/info` publishes it unchanged. `common/names.py` re-exports
  `normalize_group`/`validate_name`/`validate_group` and keeps everything this
  platform derives from them - object names, image and cache repositories, OCI
  tags, the git/image/path validators - because those change when the build
  pipeline changes. Every `from common.errors import ...` and
  `from common.names import ...` in this repository still resolves.

  **Behaviour is unchanged**, including the response envelope, the correlation
  id and the admin-key path. Two things to know when upgrading: the auth
  component is now built from settings by `api.auth.deps.get_auth()` instead of
  resolving a module-level singleton per request, so it holds one JWKS cache per
  process rather than one per interpreter; and `SSOConfig.issuer` is required in
  the shared package, with this deployment's default re-declared on
  `api.core.config.SSOSettings`. No environment variable changed name or meaning.

- **Documentation corrected against the code**, which is the source of truth.
  The substantive drift: ARCHITECTURE.md and FUNCTIONS.md still described the
  FaaS build as Knative Functions (`func`) with a synchronous build returning a
  digest and a `201`, three engine changes ago - it is kpack, declarative, and
  the digest arrives from the build controller; ARCHITECTURE.md claimed function
  code changes could not be made via `PUT` ("recreate") and that a container's
  `image` defaulted to the deployed one when omitted, both the opposite of what
  the models do; its partial-failure table published `201`/`207` status codes
  that the async 202-and-poll flow never returns; its site-config sample carried
  a per-site `apiServer` that does not exist (the endpoint is derived from the
  cluster name); the error table was missing `503`, which is the code every
  fail-closed path returns. Two `PUT` recipes in FUNCTIONS.md were bodies the
  API would reject for missing required fields. Also: `GET /api/v1/info` (long
  split per offering), the `func` glossary entry, a table broken mid-render, and
  stale symbol names (`BuildBackend.build`, `_secret_data`, `_DEFAULT_RUNTIMES`).
- `.env.example` claimed the admin API key is hashed before comparison and that
  it "defaults to a well-known dev value". Neither is true: the configured value
  *is* the credential, compared raw in constant time, and the default is empty,
  which disables key auth. It also shipped a non-empty key that contradicted its
  own comment, listed a `SERVERLESS_SITES` field the settings ignore, and omitted
  `SERVERLESS_BASE_DOMAIN` and `SERVERLESS_RUNTIMES_FILE`, without which the app
  cannot reach a cluster or start.
- The git **webhook** is documented as planned rather than as shipped. It was
  named as a live write path in three tables (the Image-CR writer, the causes of
  a new `Build`, and the write-path matrix) while the code has only the
  `BuildRequest.revision` field it will use. The convergence rule it must follow
  is kept, marked as the constraint on the endpoint's future shape.
- The changelog's `[Unreleased]` section had three `### Added`/`### Changed`/
  `### Fixed` headings each; merged into one of each, in Keep a Changelog's
  order, with every entry preserved verbatim.
- **BREAKING (chart values):** the chart now renders two Deployments, so the
  per-deployment values moved into a section each. `replicaCount`, `resources`,
  `service`, `route`, `deployment.*` and `image.repository`/`image.tag` are now
  under `api`, and the build controller's equivalents under `buildController`;
  both take the same `labels`/`annotations` (Deployment) and `podLabels`/
  `podAnnotations` (pods), and neither touches the selector, which is immutable
  once the Deployment exists. The root `image`
  section keeps `registry`, `tag` and `pullPolicy`, so a mirrored install still
  overrides those once for both, while each names its own `repository` - the two
  services ship as two images. Existing values files need the keys re-nested;
  nothing else changed shape.
- **One writer per phase for a function's KSVC image.** A create writes it once,
  at `{registry.url}/{organization}/{builderRepository}/{group}/{name}:{branch}`;
  after that the build controller is the only thing that writes it, and only
  ever as a digest. A `PUT` now keeps whatever the workload is running, whatever
  changed - it used to write the branch tag so that *something* eventually ran
  the new build, which cut a revision of the code already running (the tag
  resolves to the deployed digest until the new build finishes) with the real
  rollout arriving minutes later regardless. `POST .../build` writes no KSVC at
  all, as before.

  The controller correspondingly stopped comparing repositories before it
  writes. That guard existed so an `Image` under an old registry layout could
  not pull a workload backwards, but as the only writer it also made a layout
  change unfixable - nothing else would re-point the workload, so the function
  would sit on a repository nothing pushes to. Stranded Images are now handled
  where they come from, by the prune.
- `common.requestid` imports the Starlette ASGI types under `TYPE_CHECKING`.
  They were only ever annotations, but the runtime import meant `common.logging`
  - which reads the request-id context var from there - pulled a web framework
  in behind every log line, and so did anything that logged. `common/__init__.py`
  already claimed logging imported no framework; now it does not. The layering
  test covers the controller's modules, so this cannot come back silently.
- `Deployer` no longer has its own copy of "build a client per site" and "which
  of these is local"; both are `common.cluster.clusters_for` / `select_local`,
  which the build controller needs to mean exactly the same thing by.
- `api/services/` is grouped by responsibility instead of being a flat directory
  of 22 modules: `manifests/` builds what gets applied, `sites/` talks to the
  clusters, `state/` interprets what came back, and `builder/` covers the
  function image build. The workload engine and the two offering services stay
  at the top level, since they are what the routers hold. Module filenames are
  unchanged and every import kept its existing local alias, so the diff is moves
  and import lines - no call site changed. (`builder/`, not `build/`: the latter
  is gitignored as a Python artifact directory.)
- The check that a fetched workload belongs to the caller had five copies - the
  single GET, the stats view, the log snapshot, the update's state load, and the
  delete. It is one function now, `services.state.ownership.owned_by`. What the
  copies actually differed in was the *reaction* to a denial (404, or recording
  the site so the fan-out can tell denied from unreachable), so that stays at
  each call site; the rule itself does not. A read path added later cannot now
  be the one that forgets to check the offering label alongside the group.
- The listing's merge - per-site KSVCs into one summary per workload - moved out
  of `WorkloadService.list` into `services.state.summaries`. It was already pure
  dict work with the I/O above it, and its rules are the interesting part: a
  workload on one site of two reads `Ready` rather than `Degraded`, a site that
  did not answer is skipped, a running build outranks the KSVC status. Those are
  now stated against plain dicts instead of fake clusters, and `list` is 63 lines
  instead of 110. No behaviour change.
- Function images and their build caches now sit under
  `build.builderRepository`, the same root the composed Builder images use:
  `{registry.url}/{registry.organization}/{build.builderRepository}/{group}/{name}`.
  One value covers both because they are pushed by the same credential,
  mirrored together, and cleaned up against the same root; a function cannot
  collide with a Builder, which is one path component below the base where a
  function is two. Either prefix may be empty and is skipped, so a flat install
  is unaffected. `RegistryConfig.path` is now the single derivation of the
  segment between host and `{group}/{name}` - the image reference hangs off it
  and the Quay repository delete addresses the same string with the host
  removed, so cleanup cannot delete a different repository than the build
  pushed to.

  Changing it on an install that already has functions is handled by the
  re-tag path below: any `PUT` per function, and the old repositories are
  reclaimed.


- **Breaking:** the single-workload `GET` no longer returns `sites[].usage`. Live
  usage moved to `/stats`, which is where a client polling for it should be
  looking. It was a PodMetrics call **per site on every GET**, including the GETs
  that render a workload's configuration, where the number is never read - so
  this takes one cluster round trip per site off that path. `sites[].replicas`
  stays: it rides along on the Revision read the per-site failure detail already
  needs, so it costs nothing.

- The build contract is renamed for what it contracts: `common/contract.py` ->
  `common/build.py`, the `Builder` protocol -> `BuildBackend`,
  `api/services/builder.py` -> `api/services/kpack_backend.py`, and
  `KpackBuilder` -> `KpackBackend`. "Builder" now unambiguously means kpack's own
  `Builder` CR, which is what the docs use it for throughout. `BuildBackend` also
  declared `pull_secret -> str` while the implementation returned `str | None`;
  corrected, and covered by a test comparing every member's signature, since the
  dev extra has no type checker to notice the next drift.
- `api/services/workloads.py` is split by responsibility into `ksvc_state` (read a
  Knative object, pure), `preflight` (the guards that run before a write),
  `site_apply` (write one workload into one site) and `site_read` (read its state
  back), leaving the orchestration behind. The offering-specific behaviour the
  engine used to branch on seven times - response shaping, a container's pull-Secret
  prune, a function's git-token read, build status and build-object cleanup - is now
  the `Offering` protocol in `api/services/offering.py`, implemented once per
  offering. `apply_workload` takes an `ApplyRequest` instead of 25 keyword
  arguments. The offering constants move to `common/labels.py`, beside the
  `LABEL_OFFERING` they are the values of. No behaviour change.

- `ClusterStack` and `ClusterStore` moved out of this chart into the kpack
  chart (`clusterBuild.stacks` / `clusterBuild.stores`), along with the
  ServiceAccount and `ExternalSecret` they pull the base images and
  buildpackages with. Both objects are cluster-scoped singletons, and a per-site
  application release cannot own something cluster-wide without two releases
  eventually fighting over the same object. This chart keeps everything
  namespaced or per-site - the `Builder`s, the `kpack-builder` ServiceAccount
  and its registry `ExternalSecret`, and the Kyverno CA-injection policy - and
  now references the stack and store by name through `build.stack.name` /
  `build.store.name`. `build.stack.{create,id,buildImage,runImage}` and
  `build.store.{create,sources}` are gone; move those settings to the kpack
  release. The `Builder` -> `ClusterStore` id contract now spans two releases,
  so a missing buildpack id surfaces as a permanently not-Ready `Builder`
  instead of a chart error.
- The stack and the 21 buildpackages moved to the kpack chart repository, where
  `examples/clusterbuild-values.yaml` carries them as a worked example to seed
  your kpack release values from. Keep the stack and store names in step with
  `build.stack.name` / `build.store.name`, and every buildpack id the orders
  here name present as a store source.
- `scripts/mirror/` moved to the kpack chart repository. Everything it carries
  across an airgap is named by that chart's values now, so the tooling follows
  the values it reads: run it from a kpack checkout with `-v` pointed at the
  overlay above (docs/BUILDING.md - Airgapped Mirror Inventory). The dependency
  pull no longer reads `runtimes[].versions` at all - it mirrors what the
  store's buildpackages declare, keeping the newest z-stream of each `X.Y`. It
  can therefore carry a version no runtime advertises, which is the safe
  direction: advertising narrows what a caller may select, not what exists.
- The `BP_*_VERSION` build variable is now always written, including when the
  caller omits `version` - it gets the platform `defaultVersion`. Leaving it
  unset handed the choice to the buildpack's own default, which moves with the
  buildpackage: an untouched function could silently rebuild on a different
  language version, and airgapped it could ask for a toolchain that was never
  mirrored. Precedence is caller, then an operator `buildEnv` pin, then the
  platform default.
- `version` is replaced on update like `branch` and `runtime`, not kept like
  `gitToken`: omitting it on a PUT returns the function to the platform default,
  and - like any build-input change - rebuilds.
- `DELETE` no longer reports 404 when every site is unreachable. A missing answer
  is not evidence of absence, so an unconfirmed delete is now a 503 and the
  caller retries (delete is idempotent). A partial delete - some sites removed,
  one unreachable - reports 503 for the same reason, where it previously
  reported success. A function's build objects are reaped only once every site
  has answered, instead of before the outcome was known: that ordering could
  destroy the kpack `Image`, build `ServiceAccount` and git Secret of a workload
  that was still serving, leaving it unable to rebuild.
- The host-availability and name-absence pre-flight now run as one visit per site
  instead of two fan-outs describing two different instants. Both checks are
  kept, at both accept time (for an immediate 409) and immediately before the
  apply (the guard); one create across two sites drops from 5 sequential
  cross-site round trips to 3.
- `GET` no longer builds a `ThreadPoolExecutor` per site per request nested
  inside the executor its own worker came from, no longer chains the spec and
  build reads, and takes the per-site status from the apply response rather than
  re-reading the object it just wrote.
- Go runtimes are now 1.23/1.24/1.25 (default 1.24), replacing 1.21/1.22.
- `push-airgapped.sh` pushes images only. The runtime tarballs are artifact
  server content, not registry content - a different system with different
  credentials - and are published separately; see the mirror README in the kpack
  chart repository. A missing `images.tar.gz` is now an error rather than a
  silent no-op.
- The lint workflow derives its ruff version from `pyproject.toml` instead of
  pinning it a second time, so the formatter that gates a PR cannot disagree with
  the one `pip install -e ".[dev]"` provides.
- **Keep-on-write for secrets on `PUT`.** Reads stay redacted, but a workload
  update now treats a redacted/absent secret field as "keep the stored value", so
  the redacted GET body can be sent straight back without wiping anything: a
  `secret: true` env var or file sent without a value/content keeps what's stored;
  the git token is now **stored** in a `{workload}-git` Secret so a build-input
  change (`gitRepo`/`branch`/`runtime`) rebuilds using it - the client no longer
  re-sends `gitToken` (sending it rotates the token). Registry creds mirror a
  secret env var (username = identifier, token = value): **username + token** sets/
  rotates; the **stored username only** keeps (re-keyed to the current image's
  registry if the image moved); **neither** removes the pull secret and makes the
  image public; a token without a username, or a *different* username without a
  token, is rejected. To change a secret, send its new value; to
  remove an env var or file, drop it from the list. `FunctionUpdate` no longer
  rejects a build-input change made without a token.
- **Breaking:** moved the acting `group` from a query/body parameter to a **path
  segment**: every workload endpoint is now `/api/v1/groups/{group}/functions…`
  (and `…/containers…`). Reads/deletes no longer take `?group=`, and create/update
  request bodies no longer carry a `group` field (responses still echo it). The
  202 `statusUrl` is now `/api/v1/groups/{group}/{type}/{name}`.
- The framework HTTP error envelope now carries the numeric `status` and a
  status-derived `code` (e.g. `NOT_FOUND`, `METHOD_NOT_ALLOWED`) instead of a flat
  `HTTP_ERROR`.
- Restructured into a monorepo: renamed the `app/` package to **`api/`** and added
  a shared **`common/`** library (build contract, cluster client + `ResourceKind`,
  `CommonSettings`, `/healthz`+`/readyz` and offline docs, labels, errors, logging)
  so a builder microservice can be added without restructuring. `api.Settings` now
  subclasses `common.config.CommonSettings`.
- Moved to **Python 3.14**: `python:3.14-slim` base image and
  `requires-python = ">=3.14"`, adopting PEP 758 (unparenthesized multi-type
  `except`; ruff derives its `py314` target from `requires-python`).
- Simplified the image to a single-stage Dockerfile (`/app/api`, `/app/common`).
- Single-sourced the Python version so it can't drift: it lives only in the
  Dockerfile base image and `requires-python`; ruff and CI derive from them, and a
  CI `version` job fails the build if the two disagree (e.g. a Dependabot
  base-image bump that `requires-python` didn't follow).
- Raised the dependency floors to the Dependabot python-deps group versions.
- Fixed README/ARCHITECTURE references left pointing at the old `app/` layout, and
  moved the revision changelog out of the architecture doc into this file.

### Removed

- `uv.lock` is no longer tracked. Nothing installed from it: both Dockerfiles run
  `pip install .`/`.[api]` and every CI job resolves from `pyproject.toml`, so the
  committed lock produced no artifact and gated nothing - which is precisely why
  it went stale twice in this cycle without failing anything. Supersedes the
  regeneration noted under Fixed below; the net effect for this release is that
  the file is gone. Local development is unaffected (`pip install -e ".[api,dev]"`,
  per the README). Worth committing again the day a deploy installs with
  `uv sync --frozen` - then it pins what actually ships and cannot drift unseen.

### Fixed

- **A healthy workload could report `Degraded` because of its usage read.**
  `site_read.site_usage` documents that it never raises - it runs inside the
  `/stats` fan-out, where an escaping exception becomes a `Failed` site and a
  `Degraded` rollup - but only the metrics *read* was inside the guard, not the
  parse. A quantity in a form `state.metrics` does not recognise (Kubernetes may
  render one in decimal-exponent notation) therefore escaped as a `ValueError`
  and failed the site. The parse is now inside the guard, so an unreadable
  figure reports `measured=False` and the site keeps the status it earned.
- `POST .../containers/{name}/pull` answered its 202 with an empty `hostname`
  when the workload carried no host annotation, where the function rebuild path
  derives the default host for exactly that case. It now derives it too.
- The release workflow pushed the version tag **before** running the check
  suite, so a failed check left a tag behind - and the workflow refuses a
  version that already has one, making that version unreleasable until someone
  deleted the tag by hand. Checks now gate the tag: `validate` (version string
  and tag-does-not-exist) -> `checks` -> `prepare` (bump, commit, tag) ->
  publish. Nothing in the suite depends on the stamped version, so this costs
  nothing.
- The chart-push step piped `helm push` into `tee` without `pipefail`, so the
  step took `tee`'s exit status and a failed push fell through to an empty
  digest, surfacing later as a confusing cosign error. It now fails at the push,
  and refuses to sign if no digest was reported.
- `.gitattributes` still marked `app/static/**` vendored - the package was
  renamed to `common/` - so the vendored Swagger UI / ReDoc blobs were being
  diffed and counted as source.
- `uv.lock` was stale: it still recorded `fastapi`, `uvicorn`, `httpx`,
  `pyjwt[crypto]`, `pyyaml` and `tzdata` as **base** dependencies, from before
  they moved behind the `api` extra for the two-image split. Nothing in CI reads
  the lock, so it broke nothing, but `uv sync` handed a developer exactly the web
  stack the controller image exists to not have. Regenerated; no version moved.
- **A moved registry layout wedged every later write to a function.** `spec.tag`
  is immutable on a kpack `Image` - `validateTag` compares against the baseline
  and rejects a change at admission - so applying a re-tagged `Image` failed.
  Not only did the documented migration never work: `PUT` and `POST .../build`
  both emit the `Image` manifest, so *any* write to an affected function was
  rejected until someone deleted the object by hand.

  The API now deletes the `Image` and lets the apply recreate it whenever the
  computed tag differs from the deployed one - one GET on the build site per
  write, a no-op in every normal case. A new `Image` has no prior `Build`, so it
  builds immediately, which makes the whole migration "change the value, send
  any `PUT`". Build history resets, since `Build`s are owned by the `Image`.

  The old image and cache repositories are **reclaimed** at the same time,
  through the Quay API the delete path already uses. Cleanup on delete derives
  the *current* layout, so without this each function leaked a repository pair
  permanently and the old mutable tag was left pointing at content nothing
  tracked. Skipped when the previous reference is on another **host** - this
  token addresses one registry, and a same-named path elsewhere belongs to
  somebody else.


- DNS was blocked in the workloads namespace on OpenShift: `allow-egress-dns` opened
  53, but a NetworkPolicy matches the destination pod's port and OpenShift's CoreDNS
  listens on 5353. New `networkPolicy.dnsPorts`, default `[53, 5353]`.
- Listing functions reported a normal first build as `Degraded`. The build-first
  rule (docs/FUNCTIONS.md - Function Status Resolution) was applied only on the
  single GET, so `GET .../functions` read the KSVC alone - and that KSVC is
  failing to pull an image kpack has not pushed yet. The list now folds the build
  state in exactly as the GET does, and the two can no longer disagree about the
  same function. It costs one extra read for the whole listing, not one per
  function: `BuildBackend.statuses` label-selects every one of a group's kpack
  `Image`s from the local site in a single call, overlapped with the site
  fan-out. A container listing does not make the call at all.
- A function's per-site rows contradicted its own header while it built: the
  header said `Building` and the `sites` table directly below said `Failed` -
  `Unable to fetch image "..."`, which reads as a broken deploy during what is a
  normal build. While a build is in flight a failing site now reports `Building`
  with no error, since that pull failure is the running build rather than a
  second, independent one; the build's own state stays on `build`. Only a
  *running* build masks anything - a failed build leaves the rows untouched,
  because then the image genuinely never arrives and the site is telling the
  truth. `Building` is published in the site-status vocabulary on `/info`
  alongside the workload one.
- `GET /api/v1/groups/{group}/functions/{name}` returned 500 unconditionally:
  the response read `spec.path`, but `WorkloadSpec` never declared the field, so
  `parse_spec`'s `path=` was silently dropped by Pydantic and the read raised
  `AttributeError`. That URL is the `statusUrl` every function 202 advertises, so
  no function deploy could be observed at all.
- Any unanticipated exception is now rendered as the documented error envelope
  (`500`/`INTERNAL`) with its `requestId`, in the body and the `X-Request-ID`
  header, instead of Starlette's plain-text `Internal Server Error` with no code
  and no correlation id.
- A secret file holding non-UTF-8 bytes (a keystore, a DER certificate) failed
  the request with a 500: content was decoded to `str` with `surrogateescape` and
  the re-encode then raised. The same round-trip broke keep-on-update for any
  such file already stored.
- Undecodable `contentBase64` returned 500 rather than 400. It is now rejected by
  the request model, which is early enough - the accept path echoes the submitted
  spec back before the service-layer validation runs.
- SSO groups whose names use `_` or mixed case (e.g. `My_Team`) were unusable: the
  caller authenticated fine and carried the group in their `groups` claim, but
  every request naming that group was rejected with a `422`, because both are legal
  in a Keycloak group and illegal in the DNS-1123 object names and hosts the group
  is interpolated into (`{name}-{group}`, `{name}-{group}.{base_domain}`).
  `normalize_group` now lowercases and folds `_` to `-` alongside the existing `/`
  and `ggd-<digits>-` stripping (lowercasing runs first, so an upper-case
  `GGD-1234-` prefix is still recognized). Because normalization is applied at both
  edges — the token claim and the `{group}` path segment — the API accepts any
  spelling in the path and they all resolve to the same group; the lowercase
  hyphenated form is what is stored, deployed, and returned. Configured admin
  groups are normalized before the admin-membership comparison too, so an admin
  group written `Platform_Admins` still matches. Names normalization can't rescue
  (a leading/trailing `_`, whitespace, non-ASCII) are still rejected — the DNS-1123
  check runs on the normalized form.
- `_creation_time` used the Python-2 `except ValueError, AttributeError:` form,
  a `SyntaxError` on any supported Python that broke importing the entire
  `workloads` module (and with it the whole API); parenthesized to
  `except (ValueError, AttributeError):`.
- A workload GET now surfaces *why* a site failed: when a reachable site's KSVC
  reports `Ready=False`, the per-site `error` carries the specific cause from the
  Revision's failing sub-condition (e.g. `ContainerHealthy` - image-pull error,
  crash, quota), falling back to the KSVC's aggregate message and then a reason
  code, instead of `status: "Failed"` with `error: null`. Reuses the Revision
  read already done for the replica count, so no extra cluster call.
- `Cluster._dynamic_api` called the dynamic client's `resources.get()` with
  positional arguments, which raised `TypeError: get() takes 1 positional
  argument but 3 were given`; it now passes `api_version=`/`kind=` by keyword.

## [0.1.0] - 2026-07-06

### Added

- `GET /api/v1/info` - a public, static discovery document (version, sites,
  runtimes, sizes, per-metric scaling options, `routeDomain`,
  `defaultHostTemplate`) so a UI can render its create form from the server.
- `GET /api/v1/{type}/{name}/logs` - a point-in-time, local-site snapshot of a
  workload's pod logs (needs the `pods/log` RBAC subresource).
- Config-driven FaaS runtimes: a mounted ConfigMap read into a registry, with
  `runtime` validated against it (add a runtime by editing the ConfigMap, no
  image rebuild).
- Default-deny NetworkPolicies isolating the workloads namespace.
- `scaleDownDelay` scaling option (a Knative-capped duration).
- Configurable API Route (`route.host` / `route.labels` / `route.annotations`).
- CI/CD hardening: image scanning (Trivy), keyless signing (cosign), SBOM +
  provenance, a one-click release workflow, pinned action SHAs, gitleaks,
  kubeconform (with custom CRD schemas), and a ≥90% coverage gate - split into
  `checks` / `ci` / `release` workflows.

### Changed

- Python **3.13** on a `python:3.13-slim` base (multi-arch amd64/arm64);
  dependencies consolidated into `pyproject.toml`; `__version__` derived from the
  package metadata; the sites ConfigMap wired into the Deployment.
