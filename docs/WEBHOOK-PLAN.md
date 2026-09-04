# Git Webhook - Design & Implementation Plan

> **Status: plan, not implemented.** This document is the design the webhook lands
> under. Once it is in, its content folds into FUNCTIONS.md, BUILDING.md and API.md and
> this file goes away. Until then the "not implemented yet" notes in those documents stand.

## Contents

- [Part A: `branch` becomes `revision`](#part-a-branch-becomes-revision)
- [The revision/commit split](#the-revisioncommit-split)
- [Is it possible?](#is-it-possible)
- [Design decisions](#design-decisions)
- [What a push does](#what-a-push-does)
- [What each write path does to the Image](#what-each-write-path-does-to-the-image)
- [Active/active: one delivery, two builds](#activeactive-one-delivery-two-builds)
- [API surface](#api-surface)
- [Storage](#storage)
- [Implementation steps](#implementation-steps)
- [Security notes](#security-notes)
- [Deferred](#deferred)

## Part A: `branch` becomes `revision`

**Lands first, on its own.** It is a breaking rename with no webhook in it, and the
webhook's whole vocabulary depends on it.

`branch` is misleading: the field is written verbatim into `Image.spec.source.git.revision`,
which git resolves as *any* ref - a branch, a tag, or a commit SHA. The platform has always
accepted all three; only the name said otherwise. `BuildRequest.build_revision` already
called the resolved value a revision.

| Was | Becomes |
|-----|---------|
| `FunctionCreate.branch` / `FunctionUpdate.branch` / `FunctionResponse.branch` | `revision` (default still `"main"`) |
| `common.names.Branch`, `validate_branch` | `Revision`, `validate_revision` (same rules - they are git's ref rules, not a branch's) |
| `BuildRequest.branch` | `BuildRequest.revision` |
| `ANNOTATION_GIT_BRANCH = "serverless.platform/git-branch"` | `ANNOTATION_GIT_REVISION = "serverless.platform/git-revision"` |
| `image_tag(branch)` | `image_tag(revision)` - unchanged behaviour, still projects `/` to `-` |
| "branch tag" (registry GC, BUILD-CONTROLLER.md) | "revision tag" |

Nothing about the mechanics changes: the same string reaches kpack, the same tag is
derived from it. Pre-GA with nothing deployed, so it is a straight rename with no
compatibility shim and no migration - one `BREAKING` line in the changelog.

**Scope:** ~81 references across 14 modules, ~73 in tests, plus `charts/values.yaml`
and six documents. Mechanical, but touch every one: a half-renamed vocabulary is worse
than either name.

## The revision/commit split

Part A frees the word the webhook needs. Two distinct things, two names, and only one of
them is the user's:

| | `revision` | `commit` |
|---|---|---|
| **What** | What the caller asked to build: a branch, a tag, or a SHA | The exact commit a push delivered |
| **Who writes it** | The caller, on `POST`/`PUT` | The webhook, on a push |
| **Stored as** | KSVC annotation `serverless.platform/git-revision` | KSVC annotation `serverless.platform/git-commit` |
| **On read** | Always returned, always exactly what the caller sent | Returned read-only, `null` when following the revision |
| **In `Image.spec.source.git.revision`** | when there is no commit | `commit` wins when set |
| **In `Image.spec.tag`** | **always** - `image_tag(revision)` | never |

The rule that makes it all hang together: **`spec.tag` is derived from `revision` alone.**
A push moves the digest that `…/hello:main` points at; it never moves the tag. So:

- the caller's `revision` is what they typed, on every read, forever;
- `spec.tag` is immutable in kpack and never has to change, so no push ever forces the
  delete-and-recreate that a moved tag requires (`retag_build`) - build history, the layer
  cache and the controller's `latestImage` read all stay continuous;
- kpack still adds its unique `b{n}.{date}.{time}` tag beside `:main` on every successful
  build, so individual builds stay addressable and the registry GC needs no new rule.

## Is it possible?

Yes, and most of the platform's half is already there:

| Need | Already in place |
|------|------------------|
| A per-function credential stored where every region can read it | The `{workload}-git` Secret: built by the API, replicated to every region through `ApplyRequest.extra_secrets`, carried forward on `PUT` (never pruned), read back by `FunctionOffering.read_extra_state`. The webhook token follows the identical path as a second Secret. |
| Building an exact commit while the tag follows the revision | Exactly what `BuildRequest.revision`/`build_revision` do today (`common/build.py`, `common/kpack.py`; `test_manifests_use_a_pinned_revision_over_the_branch`). After Part A the two fields are `revision` and `commit`, and the behaviour is the one already tested. |
| A build that touches no KSVC and re-declares the `Image` in every region | `WorkloadService.apply_build` + `region_apply.apply_build_objects`: the `POST .../build` path. |
| Reconstructing the build inputs with no request body | `FunctionService._build_request` reads them off the KSVC annotations and the git Secret. |
| The convergence rule the webhook must obey | BUILDING.md - Convergence rules, rule 4: *set a SHA, never a trigger annotation*. Two API instances (or a GitLab retry) applying the same commit produce one object and one build. |

## Design decisions

| Decision | Choice | Why |
|----------|--------|-----|
| **Where the token lives** | A **Secret**, `{workload}-webhook` (Opaque, key `token`), replicated to every region exactly like `{workload}-git`. **Not** a KSVC annotation. | An annotation is readable by anyone with `get` on the KSVC, shows in `oc describe`, and is copied into every event and log line that prints the object. The platform already draws this line for the git token (ARCHITECTURE.md - Customer-provided credentials); the webhook token is a credential to the same degree - it is what lets an unauthenticated caller start a build. Replication is what keeps the hook working after a DNS switchover: the API answering is then the other region's. |
| **Not a key inside `{workload}-git`** | Its own Secret. | The git Secret is server-side applied in full by every build plan (`KpackBackend.plan`); a key the plan does not carry would be dropped on the next apply, so every writer would have to know about it. A separate object has one writer. |
| **Where the token is generated** | The API, on `POST .../functions`, with `secrets.token_urlsafe(32)`. | Echoed on the create's own 202 and on every full `GET`, so the caller never makes a second call to find it. Nothing is deployed yet, so every function has one from birth and there is no backfill path to write. |
| **Returned on read** | Yes - `webhook.token` on the create 202, on `GET .../functions/{name}`, and on the rotate response. **Never** on the list. | `gitToken` is the user's and they already have it; this one is the platform's, and its only use is being pasted into GitLab. Anyone who can read the function can already `POST .../build` with their own bearer, so showing them a credential worth exactly that grants nothing new. The list stays clean because it is the one view a wide audience polls. |
| **Endpoint** | The existing `POST .../functions/{name}/build`, authenticated **either** by `Authorization: Bearer` **or** by `X-Gitlab-Token`. | A webhook *is* a build request. One path, one service method, two ways to prove you may call it. |
| **Which pushes build** | The pushed branch must **equal the stored `revision`**. Anything else is acknowledged with `200` and ignored. | Falls out of the rename for free: a function whose `revision` is a tag or a SHA matches no branch push, so it is pinned and stays pinned - which is what asking for a SHA meant. Only a function tracking a branch moves. Switching what a function tracks stays a `PUT` with a bearer; a push must never do it. |
| **What a push changes** | `commit` only. Not `revision`, not `spec.tag`, not env, not scaling. | The webhook is an unauthenticated caller. The one thing it may say is "this commit of the ref you already chose". |
| **How the build is started** | Apply the reconstructed plan with `Image.spec.source.git.revision = commit`. **No** `additionalBuildNeeded` trigger. | Rule 4. The commit change *is* the spec change kpack builds from (`CONFIG`); a trigger on top would be a nonce and a second build. A redelivery of the same push applies an identical spec and builds nothing, which is the idempotence GitLab retries need. |
| **`POST .../build` returns to the revision head** | It clears `commit` and builds `revision`. | The caller's decision, and it keeps the endpoint's one meaning: *build what I asked for, now*. What they asked for is `revision`. It is also the escape hatch - a function whose hook was removed comes back to the head with one call. |
| **`PUT` returns to the revision head too** | Same: `commit` is cleared, the build follows `revision`. | A `PUT` is a full replace of the desired spec, and `commit` is not part of a desired spec anyone sent - it is a derived fact from a push. So every human write resets to the head and only a push pins. That is one rule instead of a carry-forward table, and it means `commit` has exactly one writer. |
| **Clearing is an explicit `null` patch** | Both paths patch `metadata.annotations` with `{"serverless.platform/git-commit": null}` rather than relying on a full apply to drop it. | The webhook writes the annotation with a merge patch, so it may own that field under a different field manager; a server-side apply that simply omits the key is not guaranteed to remove it. An explicit `null` removes it whoever owns it. |
| **No `DELETE .../webhook`** | Disabling the hook is done in GitLab. | Right call: the token is inert if nothing calls it, and the two jobs a delete would have done - unpin, stop building - are now `/build` and GitLab respectively. Rotate stays, because rotating a leaked token is something only the API can do. |
| **Response to GitLab** | `202` + the usual `FunctionResponse` when a build was scheduled; `200` + a small `WebhookOutcome` when the push was ignored; `401` on a bad or missing token; `400` on a malformed payload. | GitLab auto-disables a hook that keeps failing (`4xx` permanently, `5xx` with backoff), so "not my ref" must be a success. A bad token *should* surface in the hook's delivery log. |
| **Existence is not leaked** | A wrong token and a function that does not exist both answer `401`. | The webhook caller is unauthenticated until the token matches; the 404 is a bearer caller's answer. |
| **Provider** | GitLab push events only (`X-Gitlab-Event: Push Hook`). | The requirement. A GitHub `X-Hub-Signature-256` check over the same stored token is an additive second branch of one function (Deferred). |

## What a push does

```mermaid
sequenceDiagram
    participant G as GitLab
    participant API as API (active region)
    participant K as kpack (each region)
    participant BC as build controller

    G->>API: POST .../functions/{name}/build<br/>X-Gitlab-Token, X-Gitlab-Event: Push Hook<br/>{ref, after, checkout_sha, project}
    API->>API: load KSVC + {name}-webhook Secret
    API->>API: constant-time compare token
    alt pushed branch != stored revision, tag push, deletion, repo mismatch
        API-->>G: 200 {accepted: false, reason}
    else pushed branch == stored revision
        API-->>G: 202 FunctionResponse (revision: "main", commit: "9f2c1ab")
        API->>API: patch KSVC annotation git-commit = 9f2c1ab, per region
        API->>K: apply Image(source.git.revision = 9f2c1ab, tag = …/hello:main), per region
        K->>K: CONFIG build (spec changed); no-op if already at 9f2c1ab
        K->>BC: Image.status.latestImage = new digest
        BC->>BC: roll the digest onto the KSVC (unchanged path)
    end
```

The annotation is patched *before* the Image apply, so the KSVC - the replicated source of
truth - leads and the Image is derived from it: a patch that lands beside a failed apply
self-heals on the next write rather than leaving a build nobody can reconstruct. A region
the function does not run in is skipped, as today.

## What each write path does to the Image

For a function with `revision: "main"`, currently pinned at `commit: 9f2c1ab`:

| Path | `spec.tag` | `spec.source.git.revision` | `git-commit` annotation | Trigger? | kpack |
|------|-----------|---------------------------|------------------------|----------|-------|
| **Webhook push** (`c4f81de`) | `…/hello:main` *(unchanged)* | `9f2c1ab` -> `c4f81de` | patched to `c4f81de` | no | `CONFIG` build of `c4f81de` |
| **Webhook redelivery** (same SHA) | unchanged | unchanged | unchanged | no | nothing - identical spec |
| **`POST .../build`** | unchanged | `9f2c1ab` -> `main` | **cleared** | no | `CONFIG` build of the head |
| **`POST .../build`**, not pinned | unchanged | `main` (no change) | absent | **yes** | `TRIGGER` build of the head |
| **`PUT`** | unchanged unless `revision` changed | `9f2c1ab` -> `main` | **cleared** | no | `CONFIG` build of the head |
| **`PUT`** changing `revision` to `develop` | -> `…/hello:develop` (Image deleted + recreated by `retag_build`, as today) | `develop` | cleared | no | fresh Image builds on its own |

The trigger rule is one line: **trigger only when the apply changed nothing.** A cleared
pin is itself a spec change, so it builds; with nothing to clear there is nothing to
diff and the nonce on the latest `Build` is still what asks for one.

Accepted consequence: a `PUT` or a `/build` on a pinned function rebuilds even when the
head resolves to the commit already running. That is the cost of "every human write
returns to the head", and it is one build, not a loop - the second call finds nothing
pinned and takes the unpinned row.

## Active/active: one delivery, two builds

The image exists in every region, but as **two independent builds, not a copy**. A region
builds what it runs, in its own cluster, into its own registry (BUILDING.md - A region
builds what it runs), and one region per registry is enforced twice: the chart refuses to
render two regions on one registry, and the build controller refuses to sweep when it
detects one. So a push produces:

| | region-a | region-b |
|---|---|---|
| `Image` | `revision = c4f81de`, tag `regA/team/hello:main` | `revision = c4f81de`, tag `regB/team/hello:main` |
| Build | its own, in its own cluster | its own, in its own cluster |
| Digest | `sha256:aaa…` | `sha256:bbb…` - **different**; builds are not bit-reproducible |
| Rolled onto the KSVC by | that region's build controller | that region's build controller |

GitLab delivers **once**, to the shared host; DNS routes it to whichever region is active;
that API instance fans the apply out to every region the function runs in, exactly as
`POST .../build` already does. A retry landing on the other region applies the same spec
and builds nothing - rule 4.

**This is where pinning the commit earns its keep a second time.** Without it, each
region's kpack `SourceResolver` re-resolves `main` on its own schedule, so two pushes in
quick succession can leave region-a on commit 1 and region-b on commit 2 - serving
different source, invisibly, since the per-region digests are expected to differ anyway.
Handing both regions the same commit converges them on source even though they will never
converge on bytes.

A build can still succeed in one region and fail in the other, which reads as
`Failed`/`BuildFailed` beside `Building` on the same `statusUrl` the 202 hands back. The
webhook needs no partial-failure story of its own.

## API surface

### Create - `POST .../functions` -> `202`

Request: `branch` is now `revision` (Part A). Response gains:

```json
"webhook": {
  "url": "https://serverless.example.com/api/serverless/v1/groups/payments/functions/hello/build",
  "token": "k3Xz…",
  "provider": "gitlab",
  "events": ["push"]
}
```

`url` is absolute because GitLab needs one: built from a new setting `SERVERLESS_PUBLIC_URL`
(chart: `https://{api.route.host}`), falling back to the path alone when unset (local dev).

### Read - `GET .../functions/{name}` -> `200`

```json
"revision": "main",        // always exactly what the caller sent
"commit": "9f2c1ab…",      // read-only; the commit a push pinned, null when following the head
"webhook": { … }
```

`GET .../functions` (the list) and `/stats` carry none of these three.

> `commit` is additive and read-only. Drop the field if a leaner response is wanted - the
> annotation still drives the build either way; it only stops a user being able to see
> which commit they are on.

### Build - `POST .../functions/{name}/build`

| Caller | Headers | Body | Behaviour |
|--------|---------|------|-----------|
| A user or automation | `Authorization: Bearer …` | none | Clears `commit` and builds `revision`'s head. `202`. |
| GitLab | `X-Gitlab-Token`, `X-Gitlab-Event: Push Hook` | GitLab push event | Validates the token, then: the pushed ref must be `refs/heads/{stored revision}`; `after` must be a commit (not the all-zero deletion marker); `project.git_http_url` must name the stored repository (trailing `.git`/`/` ignored). Match -> build pinned at `checkout_sha` (else `after`), `202`. No match -> `200 {accepted: false, reason}`. |
| GitLab, other event kinds | `X-Gitlab-Event: Tag Push Hook`, … | any | `200 {accepted: false, reason: "event not handled"}`. Configure the hook for push events only; this is the safety net. |
| Both headers | | | The bearer wins; the GitLab header is ignored. |
| Neither | | | `401`, as today. |

### Rotate - `POST .../functions/{name}/webhook/rotate` -> `200`

Bearer only. New token, applied in every region the function runs in, returns the new
`webhook` object. The old token stops working the moment the last region's apply lands;
there is no overlap window (a hook is reconfigured in seconds). Rotation does **not**
touch `commit` - it is a credential operation, not a build one.

Disabling a hook is done in GitLab; the API has no endpoint for it.

### Errors

| Code | When |
|------|------|
| `400 VALIDATION_ERROR` | Bearer path: as today (no stored token, runtime gone). Webhook path: the body is not a push event, or `after` is not a hex SHA. |
| `401 UNAUTHENTICATED` | Webhook path: token missing, wrong, function absent, or no token stored. |
| `404` | Bearer path only, as today. |
| `503` | A region could not be read, as every other write. |

## Storage

**`{workload}-webhook` Secret** (one per function, every target region):

```yaml
apiVersion: v1
kind: Secret
type: Opaque
metadata:
  name: hello-webhook
  labels:            # workload_labels(): group / managed-by / owner / workload / offering
  ownerReferences:   # the KSVC beside it, so it cascades on delete
data:
  token: <base64>
```

Applied through `ApplyRequest.extra_secrets` (create, update, rotate) - the same channel as
the git Secret, so it is never in the managed prune set. Read back in
`FunctionOffering.read_extra_state` as `webhook_token`, beside `git_token`.

**KSVC annotations:**

| Annotation | Written by | Cleared by |
|---|---|---|
| `serverless.platform/git-revision` | `POST`/`PUT`, composed into the KSVC | never (a function always has one) |
| `serverless.platform/git-commit` | the webhook, merge patch | `POST .../build` and `PUT`, explicit `null` patch |

The reconstruction table (BUILDING.md - Reconstruction after a gap) gains one row:

| Input | Source |
|-------|--------|
| commit | ksvc annotation `ANNOTATION_GIT_COMMIT`; absent = build the revision's head |

## Implementation steps

Part A is one commit of its own. Each webhook step is independently testable and leaves
the tree green; the order is the dependency order.

### A. The rename

- `common/names.py`: `validate_branch` -> `validate_revision`, `Branch` -> `Revision`
  (schema description: "Branch, tag or commit to build."), `image_tag(revision)`.
- `common/build.py`: `BuildRequest.branch` -> `revision`; `build_revision` becomes
  `commit or revision` once step 6 adds `commit` (for now, just the rename).
- `api/models/function.py`, `api/models/common.py`: the three `branch` fields;
  `ANNOTATION_GIT_BRANCH` -> `ANNOTATION_GIT_REVISION` with the new key string.
- `api/services/`: `function.py`, `offering.py`, `manifests/ksvc.py`,
  `regions/region_read.py`, `state/describe.py`, `workloads/request.py`,
  `workloads/service.py` - the parameter and dict keys.
- `build_controller/gc.py` + `charts/values.yaml`: "branch tag" -> "revision tag".
- Docs: FUNCTIONS.md, BUILDING.md, API.md, ARCHITECTURE.md, BUILD-CONTROLLER.md,
  RUNTIMES.md, README.md. CHANGELOG: one `BREAKING` line.
- Tests: rename throughout; add one asserting a 40-hex SHA and a tag name are both
  accepted as `revision` and reach `Image.spec.source.git.revision` verbatim, with the
  tag projected by `image_tag`.

### 1. The token Secret and its read-back

- `api/services/manifests/secrets.py`: `webhook_secret_name(workload) -> "{workload}-webhook"`,
  `WEBHOOK_TOKEN_KEY = "token"`, `build_webhook_secret(name, labels, token)` (Opaque, via
  `res.build_secret`), `new_webhook_token() -> secrets.token_urlsafe(32)`.
- `api/services/offering.py` (`FunctionOffering.read_extra_state`): also read the webhook
  Secret -> `existing["webhook_token"]` (None when absent).
- `api/services/regions/region_read.py` (`existing_state`): add `"owner"` off
  `LABEL_OWNER`, so a build a push starts is stamped with the function's owner rather
  than a synthetic name.
- Tests: `tests/test_secrets.py` (manifest shape, type, key; the token never appears in
  `repr`), `tests/test_workload_service.py` (read-back present/absent).

### 2. Models

- `api/models/function.py`: `WebhookView(url, token, provider="gitlab", events=["push"])`;
  `FunctionResponse.webhook: WebhookView | None`, `FunctionResponse.commit: str | None`.
  `WorkloadSummary` untouched.
- `api/models/webhook.py` (new): `GitLabPushEvent` - lenient (`extra="ignore"`), fields
  `object_kind`, `ref`, `before`, `after`, `checkout_sha`, `project.git_http_url`;
  properties `branch` (strips `refs/heads/`, None for anything else), `is_deletion`,
  `sha` (`checkout_sha or after`, validated `^[0-9a-f]{40}$|^[0-9a-f]{64}$`).
  `WebhookOutcome(accepted: bool, reason: str | None, statusUrl: str | None)`.
- `api/models/common.py`: `ANNOTATION_GIT_COMMIT = "serverless.platform/git-commit"`.
- Tests: `tests/test_models.py` - ref parsing (branch / tag / weird), deletion marker,
  SHA validation, extra payload fields ignored.

### 3. Settings and chart

- `api/core/config.py`: `public_url: str = ""` (env `SERVERLESS_PUBLIC_URL`; validator
  strips a trailing `/`, requires `http(s)://` when set).
- `charts/.../api/deployment.yaml`: `SERVERLESS_PUBLIC_URL` = `https://` + the same
  expression `route.yaml` uses for the host. `values.yaml` comment.
- `api/core/paths.py`: `webhook_url(settings, group, name)`.
- Tests: `tests/test_chart_values.py` renders the env var; `tests/test_base_path.py`
  covers the URL with and without a base path, and with `public_url` unset.

### 4. Create, update, read and rotate carry the token

- `FunctionService.accept`: `token = new_webhook_token()`; schedule
  `functools.partial(self.create, webhook_token=token)`; echo `webhook=WebhookView(...)`
  on the 202 through `**extra`.
- `FunctionService.create`: `extra_secrets = plan.replicated + [build_webhook_secret(...)]`.
- `FunctionService.update`: re-emits the Secret with the token read back
  (`existing.get("webhook_token") or new_webhook_token()` - defensive, not a migration:
  an update targets *every* configured region, so a region that gains the workload must
  gain the token with it, exactly as it gains the git Secret).
- The GET path: read the webhook Secret in the same per-region read thread that already
  reads the pull Secret for its username; project `webhook` and `commit` onto
  `FunctionResponse`.
- `FunctionService.rotate_webhook(group, name, user)` + router
  `POST /{name}/webhook/rotate`: `load_existing` (authorizes), new token, fan the Secret
  out with owner stamping to every region the function runs in (skip absent, as
  `apply_build_objects` does), return `WebhookView`. Not a 202: one small Secret, and the
  caller needs the token now.
- Tests: `tests/test_workload_service.py` - create applies the Secret in every region;
  update keeps it and reaches a region the create did not; GET returns `webhook`; the
  list does not; rotate replaces the value in every region, answers with the new one, and
  leaves `commit` alone. `tests/test_api.py` - the router shape, and that a container has
  no rotate (`404`).

### 5. The second way into `/build`

- `api/auth/deps.py`: `GITLAB_TOKEN_HEADER = "X-Gitlab-Token"`, `GITLAB_EVENT_HEADER`;
  `build_caller(request) -> Principal | WebhookCaller` - a bearer that validates wins;
  otherwise, if the GitLab header is present, `WebhookCaller(token, event)`; otherwise
  raise the same `UnauthenticatedError` as `require_auth`. Define it over `optional_auth`
  so `dependency_overrides` replaces the bearer half alone, as the stream dependency does.
- `api/routers/functions.py` - `build_function`: `caller: BuildCaller`,
  `event: GitLabPushEvent | None = Body(None)`, `responses={200: {"model": WebhookOutcome}}`.
  A `Principal` -> `svc.accept_build(...)` (unchanged contract); a `WebhookCaller` ->
  `svc.accept_webhook(...)`, whose `WebhookOutcome` is returned as `JSONResponse(200)`
  and whose `FunctionResponse` is the 202.
- `FunctionService.accept_webhook`:
  1. `load_existing(name, FUNCTION, principal, group)` with
     `principal = Principal(subject=f"webhook:{group}/{name}", username="webhook",
     groups=[group])` - group-scoped, non-admin, so the tenancy check runs as for any
     caller. `NotFoundError` -> `UnauthenticatedError` (no existence leak).
  2. `stored = existing.get("webhook_token")`; `not stored or not
     hmac.compare_digest(stored, caller.token)` -> `UnauthenticatedError`.
  3. Event checks, each an early `WebhookOutcome(accepted=False, reason=…)`: event kind
     not `Push Hook`; `event.branch is None` (tag or unknown ref); `event.is_deletion`;
     `event.branch != existing["revision"]` (so a function on a tag or SHA never matches);
     repository mismatch (`common.names` gains `same_repository(a, b)`: scheme+host+path,
     case-insensitive host, trailing `.git`/`/` ignored).
  4. `req = self._build_request(name, group, existing, owner=existing["owner"],
     commit=event.sha)`; `_assert_runtime` as the bearer path does.
  5. `background.add_task(run_background, self.build_at_commit, group, name, existing, req)`;
     return `self._engine.accepted(..., revision=existing["revision"], commit=event.sha)`.
  Log the delivery at info with `X-Gitlab-Webhook-UUID` and the decision, never the token.
- `FunctionService._build_request` gains `owner` and `commit` keyword arguments, both
  defaulting to today's behaviour.
- Tests: `tests/test_api.py` - the header routes to `accept_webhook`; bearer + header ->
  bearer; neither -> 401; an ignored push is a 200 with `accepted: false`; a scheduled one
  is the usual 202. `tests/test_function_webhook.py` (new) drives `accept_webhook` over
  the fake clusters: wrong token, missing token, absent function all 401; a function whose
  `revision` is a tag or a SHA ignores a branch push; branch mismatch / tag push /
  deletion / repo mismatch each name their reason; a match schedules a build whose
  `BuildRequest.commit` is the SHA and whose owner label is the function's; **the
  202 echoes `revision: "main"`, not the SHA.**

### 6. Pin the commit, and return to the head on every human write

- `common/build.py`: `BuildRequest.commit: str | None`; `build_revision` returns
  `self.commit or self.revision`. `spec.tag` keeps deriving from `revision` alone, so a
  push never moves it and `retag_build` never fires for one.
- `WorkloadService.apply_build(name, group, plan, *, trigger=True, commit=None)`: patch
  each region's KSVC `metadata.annotations` **before** the Image apply - `git-commit` set
  to `commit`, or to `null` to clear it - as a merge patch of metadata only
  (`stamp_pull` minus its template half, so no Knative revision is cut). Call
  `builder.trigger` only when `trigger` is true.
- `FunctionService.build_at_commit(...)`: `apply_build(..., trigger=False, commit=sha)`.
- `FunctionService.build` (the bearer `/build`): `apply_build(..., commit=None,
  trigger=existing.get("commit") is None)` - clear the pin, and trigger only when there
  was nothing to clear, since a cleared pin is itself the spec change kpack builds from.
- `PUT`: `region_read.existing_state` reads `commit` back so `update` knows whether there
  is one to clear; the apply passes `commit=None` and the clearing patch rides along with
  the KSVC write. `_build_request` does **not** default `commit` from stored state - only
  the webhook ever sets it.
- Tests: `tests/test_workload_service.py` - a push applies `source.git.revision = sha` in
  every region with `spec.tag` unchanged, and never touches a `Build`; the annotation is
  written per region; a redelivery applies an identical Image and builds nothing;
  `POST .../build` on a pinned function clears the annotation, re-applies the revision and
  does **not** trigger; on an unpinned function it triggers exactly as today; a `PUT`
  clears the pin; a `PUT` that changes `revision` moves the tag through `retag_build` as
  it already does. `tests/test_kpack_build.py` gains the commit-wins-over-revision case.

### 7. Docs and changelog

- FUNCTIONS.md: a "Git webhook" section (GitLab setup: URL, secret token, push events,
  SSL verification on; the `webhook` object; what a push does and does not do), the
  `commit` field, rotate; the revision/commit split; update the "Take a commit SHA" row
  and the "build a pushed commit now" bullet under *Building again without changing
  anything*.
- BUILDING.md: the `webhook` row in *Every write path is a full server-side apply* moves
  from *planned* to real; the reconstruction table gains `commit`; the `COMMIT` row in
  *What causes a new Build*; strike open question 5.
- API.md: endpoint table (+ rotate), the auth section (a second credential on `/build`),
  the error table (`401` on the webhook path).
- ARCHITECTURE.md: the open-questions bullet; the secrets section lists `{workload}-webhook`.
- DEPLOYING.md: `SERVERLESS_PUBLIC_URL`, and that GitLab must reach the API's Route (in an
  airgapped install that is an allow-list entry on GitLab's outbound side, not a
  NetworkPolicy here - the API's Route is already public).
- CHANGELOG.md: `Added (git webhook)` beside Part A's `BREAKING` line.
- Delete this file.

**Effort:** Part A is a day, mostly mechanical, and worth landing and reviewing alone.
Steps 1-4 are additive and small. Step 5 is the endpoint. Step 6 is the one that changes
existing paths (`apply_build`, `build`, `update`) and is where the tests earn their keep.
Two to three days for the webhook on top of Part A.

## Security notes

- **Constant-time compare** (`hmac.compare_digest`) against the stored token.
- **Entropy:** `secrets.token_urlsafe(32)` - 256 bits, URL-safe, fits GitLab's field.
- **What the token buys an attacker:** one build of the ref the function already tracks, at
  a commit they name, in the function's own namespace, with the function's own git
  credential - a commit that credential can already fetch. No desired state changes: not
  the revision, not the tag, not env, not scaling. Repository and ref are checked against
  what is stored, so the token cannot be pointed at another repository, and a function
  pinned to a tag or a SHA cannot be moved at all.
- **It cannot read anything back:** the 202 body is the same redacted response any
  authenticated reader sees, and `commit` is what the caller just sent.
- **Never logged, never in `repr`:** `WebhookCaller.token` and `WebhookView.token` are
  `repr=False`, as `gitToken` is. Log the delivery UUID and the decision instead.
- **Body bound:** the push event is parsed leniently; cap the body at 1 MiB (GitLab sends
  up to 20 commits).
- **No existence leak:** every failure before the token matches is `401`.
- **Idempotent by data**, so GitLab's retries and two API replicas are safe without a
  lease (rule 4).
- **Rotation** invalidates the old token in every region on the next apply.

## Deferred

- **Monorepo path filter.** A push to the ref rebuilds every function tracking it,
  whatever `path` they build from. GitLab's payload lists changed files per commit (first
  20 only), so "ignore a push that touched nothing under `path`" is a small optional
  follow-up. Not now: a false negative - a change outside `path` that matters, like a
  shared root lockfile - is worse than an extra build.
- **Tag-push support.** A function whose `revision` is a tag ignores every push today. If
  moving tags turn out to be how releases are cut, `Tag Push Hook` with the same
  ref-equals-revision rule is the natural extension.
- **GitHub / generic providers.** `X-Hub-Signature-256` is an HMAC over the body keyed
  with the same stored token; add a second branch to `build_caller` and a
  `GitHubPushEvent` with the same properties. Storage, pinning and the build path are
  provider-neutral already.
- **Delivery record.** Exposing the last delivery (UUID, SHA, outcome, time) on the GET
  would help debug a hook; it needs somewhere to write it. Wait for a real need.
