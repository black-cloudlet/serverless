# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Request correlation: the error envelope's `requestId` is now populated (was
  always `null`). A middleware adopts an inbound `X-Request-ID` (e.g. from the
  OpenShift router) or mints a UUID, echoes it in the `X-Request-ID` response
  header on every response, and binds it into the server logs — so a `requestId`
  from an error body greps straight to that request's log lines.
- Every function/container now gets CA-trust env vars pointed at the mounted
  trusted-CA bundle so tooling trusts internal TLS out of the box:
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`,
  `GIT_SSL_CAINFO`. A var the caller sets themselves is left untouched (their
  value wins); the injected defaults are transparent — recorded in a
  `serverless.platform/injected-env` annotation and hidden from the workload's
  GET response.
- Configurable labels on the chart-created namespaces: `namespaces.labels`
  (applied to both) plus `namespaces.apiLabels` / `namespaces.workloadsLabels`
  (per-namespace, override the shared set) — e.g. to set
  `pod-security.kubernetes.io/enforce` or a `namespaceSelector` target.

### Fixed

- A workload GET now surfaces *why* a site failed: when a reachable site's KSVC
  reports `Ready=False`, the per-site `error` carries the specific cause from the
  Revision's failing sub-condition (e.g. `ContainerHealthy` — image-pull error,
  crash, quota), falling back to the KSVC's aggregate message and then a reason
  code, instead of `status: "Failed"` with `error: null`. Reuses the Revision
  read already done for the replica count, so no extra cluster call.
- `Cluster._dynamic_api` called the dynamic client's `resources.get()` with
  positional arguments, which raised `TypeError: get() takes 1 positional
  argument but 3 were given`; it now passes `api_version=`/`kind=` by keyword.

### Changed

- **Keep-on-write for secrets on `PUT`.** Reads stay redacted, but a workload
  update now treats a redacted/absent secret field as "keep the stored value", so
  the redacted GET body can be sent straight back without wiping anything: a
  `secret: true` env var or file sent without a value/content keeps what's stored;
  omitting `registryToken` (even while echoing `registryUsername`) keeps the
  credential (re-keyed to the current image's registry if the image moved to a
  different one); and the git token is now **stored** in a `{workload}-git` Secret so a
  build-input change (`gitRepo`/`branch`/`runtime`) rebuilds using it — the client
  no longer re-sends `gitToken` (sending it rotates the token). To change a secret,
  send its new value; to remove an env var or file, drop it from the list.
  `ContainerUpdate` now accepts `registryUsername` without a token (a token still
  requires a username), and `FunctionUpdate` no longer rejects a build-input change
  made without a token.
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

- `GET /api/v1/info` — a public, static discovery document (version, sites,
  runtimes, sizes, per-metric scaling options, `routeDomain`,
  `defaultHostTemplate`) so a UI can render its create form from the server.
- `GET /api/v1/{type}/{name}/logs` — a point-in-time, local-site snapshot of a
  workload's pod logs (needs the `pods/log` RBAC subresource).
- Config-driven FaaS runtimes: a mounted ConfigMap read into a registry, with
  `runtime` validated against it (add a runtime by editing the ConfigMap, no
  image rebuild).
- Default-deny NetworkPolicies isolating the workloads namespace.
- `scaleDownDelay` scaling option (a Knative-capped duration).
- Configurable API Route (`route.host` / `route.labels` / `route.annotations`).
- CI/CD hardening: image scanning (Trivy), keyless signing (cosign), SBOM +
  provenance, a one-click release workflow, pinned action SHAs, gitleaks,
  kubeconform (with custom CRD schemas), and a ≥90% coverage gate — split into
  `checks` / `ci` / `release` workflows.

### Changed

- Python **3.13** on a `python:3.13-slim` base (multi-arch amd64/arm64);
  dependencies consolidated into `pyproject.toml`; `__version__` derived from the
  package metadata; the sites ConfigMap wired into the Deployment.
