# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Changed

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
### Fixed

- DNS was blocked in the workloads namespace on OpenShift: `allow-egress-dns` opened
  53, but a NetworkPolicy matches the destination pod's port and OpenShift's CoreDNS
  listens on 5353. New `networkPolicy.dnsPorts`, default `[53, 5353]`.
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

### Added

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
