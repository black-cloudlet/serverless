# Git Webhook - Design & Implementation Plan

> **Status: plan, not implemented.** This document is the design the webhook lands
> under. Once it is in, its content folds into FUNCTIONS.md, BUILDING.md and API.md and
> this file goes away. Until then the "not implemented yet" notes in those documents stand.

## Contents

- [Is it possible?](#is-it-possible)
- [Design decisions](#design-decisions)
- [What a push does](#what-a-push-does)
- [API surface](#api-surface)
- [Storage](#storage)
- [Implementation steps](#implementation-steps)
- [Security notes](#security-notes)
- [Deferred](#deferred)

## Is it possible?

Yes, and most of the platform's half is already there:

| Need | Already in place |
|------|------------------|
| A per-function credential stored where every region can read it | The `{workload}-git` Secret: built by the API, replicated to every region through `ApplyRequest.extra_secrets`, carried forward on `PUT` (never pruned), read back by `FunctionOffering.read_extra_state`. The webhook token follows the identical path as a second Secret. |
| Building an exact commit instead of the branch head | `BuildRequest.revision` -> `Image.spec.source.git.revision` (`common/build.py`, `common/kpack.py`; `test_manifests_use_a_pinned_revision_over_the_branch`). The tag still follows the branch, so a pinned build replaces the same tag and the build controller rolls it out unchanged. |
| A build that touches no KSVC and re-declares the `Image` in every region | `WorkloadService.apply_build` + `region_apply.apply_build_objects`: the `POST .../build` path. |
| Reconstructing the build inputs with no request body | `FunctionService._build_request` reads them off the KSVC annotations and the git Secret. |
| The convergence rule the webhook must obey | BUILDING.md - Convergence rules, rule 4: *set a SHA, never a trigger annotation*. Two API instances (or a GitLab retry) applying `revision = <sha>` produce one object and one build. |

What is missing is the token, the second way into the build endpoint, and pinning the
pushed SHA. Nothing in the engine has to change shape.

## Design decisions

| Decision | Choice | Why |
|----------|--------|-----|
| **Where the token lives** | A **Secret**, `{workload}-webhook` (Opaque, key `token`), replicated to every region exactly like `{workload}-git`. **Not** a KSVC annotation. | An annotation is readable by anyone with `get` on the KSVC, shows in `oc describe`, and is copied into every event and log line that prints the object. The platform already draws this line for the git token (ARCHITECTURE.md - Customer-provided credentials); the webhook token is a credential to the same degree - it is what lets an unauthenticated caller start a build. Replication is what keeps the hook working after a DNS switchover: the API answering is then the other region's. |
| **Not a key inside `{workload}-git`** | Its own Secret. | The git Secret is server-side applied in full by every build plan (`KpackBackend.plan`); a key the plan does not carry would be dropped on the next apply, so every writer would have to know about it. A separate object has one writer. |
| **Where the token is generated** | The API, on `POST .../functions` (synchronously, before the 202), with `secrets.token_urlsafe(32)`. | It is echoed on the create's own 202 and on every full `GET`, so the caller never has to make a second call to find it. A function created before this feature gets one on its next `PUT`, or on demand from the rotate endpoint. |
| **Returned on read** | Yes - `webhook.token` on the create 202, on `GET .../functions/{name}`, and on the rotate response. **Never** on the list. | The user's requirement, and the reason this token differs from `gitToken`: `gitToken` is the user's, and they already have it; this one is the platform's, and its only use is being pasted into GitLab. Anyone who can read the function can also `POST .../build` with their own bearer, so showing them a credential worth exactly that grants nothing new. The list stays clean because it is the one view a wide audience polls. |
| **Endpoint** | The existing `POST .../functions/{name}/build`, authenticated **either** by `Authorization: Bearer` (today) **or** by `X-Gitlab-Token`. | The user's requirement, and honest: a webhook *is* a build request. One path, one service method, two ways to prove you may call it. |
| **Which branch a push builds** | The pushed ref must **equal the stored `branch`**; the build pins the pushed SHA. Any other ref is acknowledged with `200` and ignored. | The alternative - "switch the function to whatever branch was pushed" - means a developer pushing `feature/x` redeploys the function from `feature/x`. That is a `PUT` in disguise, from an unauthenticated caller, and it changes desired state (the branch annotation, the image tag, the Image's own `spec.tag` which is immutable and forces a delete+recreate). The webhook stays what BUILDING.md says it is: the pushed SHA, nothing else. Switching branches remains a `PUT` with a bearer. |
| **How the build is started** | Apply the reconstructed plan with `revision = <sha>` in every region. **No** `additionalBuildNeeded` trigger. | Rule 4. The revision change *is* the spec change kpack builds from (`CONFIG`); a trigger on top would be a nonce and a second build. A redelivery of the same push applies an identical spec and builds nothing, which is the idempotence GitLab retries need. |
| **The pin is persisted** | On the KSVC as a metadata annotation `serverless.platform/git-revision`, patched (metadata only - no revision is cut) in every region beside the Image apply. | Without it the next `PUT` that changes nothing would re-apply `revision = main`, a spec change, and rebuild - breaking "a `PUT` rebuilds only when a build input changes". With it, every reconstruction carries the pin: the reconstruction table in BUILDING.md gains a row. |
| **Manual `POST .../build` unpins** | With a bearer, a rebuild clears the pin and builds the branch head (the annotation is removed, the plan applies `revision = branch`, and - because that is itself a spec change - the trigger is skipped). Without a pin it behaves exactly as today. | Keeps the documented meaning of the endpoint ("the branch head, now") and gives an escape hatch: a function whose GitLab hook was deleted would otherwise stay on its last pushed SHA forever. |
| **A `PUT` keeps the pin only while the source is unchanged** | `gitRepo`, `branch` and `path` unchanged -> carry `revision` forward; any of them changed -> drop it (the branch head of the new source). | The pin belongs to a (repo, branch); it means nothing for another. |
| **Response to GitLab** | `202` + the usual `FunctionResponse` when a build was scheduled; `200` + a small `WebhookOutcome` when the push was ignored; `401` on a bad or missing token; `400` on a malformed payload. | GitLab auto-disables a hook that keeps failing (`4xx` permanently, `5xx` with backoff), so "not my branch" must be a success. A bad token, on the other hand, *should* surface in the hook's delivery log. |
| **Existence is not leaked** | A wrong token and a function that does not exist both answer `401`. | The webhook caller is unauthenticated until the token matches; the 404 is a bearer caller's answer. |
| **Provider** | GitLab push events only in this phase (`X-Gitlab-Event: Push Hook`). | The requirement. The token check is one function; a GitHub `X-Hub-Signature-256` check over the same stored token is an additive second branch of it (Deferred). |

## What a push does

```mermaid
sequenceDiagram
    participant G as GitLab
    participant API as API (active region)
    participant K as kpack (each region)
    participant BC as build controller

    G->>API: POST .../functions/{name}/build<br/>X-Gitlab-Token, X-Gitlab-Event: Push Hook<br/>{ref, after, checkout_sha, project}
    API->>API: load KSVC + {name}-webhook Secret (every region)
    API->>API: constant-time compare token
    alt ref != stored branch, tag push, branch deleted, repo mismatch
        API-->>G: 200 {accepted: false, reason}
    else ref == stored branch
        API-->>G: 202 FunctionResponse (statusUrl)
        API->>API: reconstruct BuildRequest, revision = sha
        API->>K: server-side apply Image(revision = sha) + SA + git Secret, per region
        API->>API: patch KSVC metadata annotation git-revision = sha, per region
        K->>K: CONFIG build (spec changed); no-op if already at sha
        K->>BC: Image.status.latestImage = new digest
        BC->>BC: roll the digest onto the KSVC (unchanged path)
    end
```

Everything from "reconstruct" on is the existing rebuild path with two differences: the
revision is the SHA, and there is no trigger. A region the function does not run in is
skipped, as today.

## API surface

### Create - `POST .../functions` -> `202`

Unchanged request. The response gains:

```json
"webhook": {
  "url": "https://serverless.example.com/api/serverless/v1/groups/payments/functions/hello/build",
  "token": "k3Xz…",
  "provider": "gitlab",
  "events": ["push"]
}
```

`url` is absolute because GitLab needs one: it is built from a new setting
`SERVERLESS_PUBLIC_URL` (chart: `https://{api.route.host}`), falling back to the
path alone when unset (local dev).

### Read - `GET .../functions/{name}` -> `200`

Adds the same `webhook` object, plus:

```json
"revision": "9f2c1ab…"   // the pinned commit, or null while following the branch head
```

`GET .../functions` (the list) and `/stats` carry neither.

### Build - `POST .../functions/{name}/build`

| Caller | Headers | Body | Behaviour |
|--------|---------|------|-----------|
| A user or automation | `Authorization: Bearer …` | none | As today, plus: clears a pin and builds the branch head. `202`. |
| GitLab | `X-Gitlab-Token: <token>`, `X-Gitlab-Event: Push Hook` | GitLab push event | Validates the token, then: `ref` must be `refs/heads/{stored branch}`; `after` must be a commit (not the all-zero deletion marker); `project.git_http_url` must name the stored repository (trailing `.git`/`/` ignored). Match -> build pinned at `checkout_sha` (else `after`), `202`. No match -> `200 {accepted: false, reason}`. |
| GitLab, other event kinds | `X-Gitlab-Event: Tag Push Hook`, `Merge Request Hook`, … | any | `200 {accepted: false, reason: "event not handled"}`. Configure the hook for push events only; this is the safety net. |
| Both headers | | | The bearer wins; the GitLab header is ignored. |
| Neither | | | `401`, as today. |

A `202` is the same `FunctionResponse` with the same `statusUrl` create/update/build
return, so a client polls one place.

### Rotate - `POST .../functions/{name}/webhook/rotate` -> `200`

Bearer only. Generates a new token, applies the Secret in every region, returns the new
`webhook` object. Also how a function created before this feature gets its first token
without waiting for a `PUT`. The old token stops working the moment the last region's
apply lands; there is no overlap window (a hook is reconfigured in seconds).

### Errors

| Code | When |
|------|------|
| `400 VALIDATION_ERROR` | Bearer path: as today (no stored token, runtime gone). Webhook path: body is not a push event, `after` is not a hex SHA. |
| `401 UNAUTHENTICATED` | Webhook path: token missing, wrong, function absent, or no token stored yet. |
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

Applied through `ApplyRequest.extra_secrets` (create, update, rotate) - the same channel
as the git Secret, so it is never in the managed prune set and an update that carries
nothing keeps the stored copy. Read back in `FunctionOffering.read_extra_state` as
`webhook_token`, beside `git_token`.

**`serverless.platform/git-revision`** KSVC metadata annotation: the pinned SHA. Absent
means "the branch head". Stamped by `build_ksvc` on `PUT` (carried forward), patched
directly by the webhook path, removed by the bearer rebuild.

The reconstruction table (BUILDING.md - Reconstruction after a gap) gains one row:

| Input | Source |
|-------|--------|
| revision | ksvc annotation `ANNOTATION_GIT_REVISION`; absent = the branch |

## Implementation steps

Each step is independently testable and leaves the tree green; the order is the
dependency order.

### 1. The token Secret and its read-back

- `api/services/manifests/secrets.py`: `webhook_secret_name(workload) -> "{workload}-webhook"`,
  `WEBHOOK_TOKEN_KEY = "token"`, `build_webhook_secret(name, labels, token)` (Opaque, via
  `res.build_secret`), `new_webhook_token() -> secrets.token_urlsafe(32)`.
- `api/services/offering.py` (`FunctionOffering.read_extra_state`): also read the webhook
  Secret -> `existing["webhook_token"]` (None when absent).
- `api/services/regions/region_read.py` (`existing_state`): add `"owner": labels[LABEL_OWNER]`
  so a build the webhook starts is stamped with the function's owner, not a synthetic name.
- Tests: `tests/test_secrets.py` (manifest shape, type, key; the token never appears in
  `repr`), `tests/test_workload_service.py` (read-back present/absent).

### 2. Models

- `api/models/function.py`: `WebhookView(url, token, provider="gitlab", events=["push"])`;
  `FunctionResponse.webhook: WebhookView | None`, `FunctionResponse.revision: str | None`.
  `WorkloadSummary` untouched.
- `api/models/webhook.py` (new): `GitLabPushEvent` - lenient (`extra="ignore"`), fields
  `object_kind`, `ref`, `before`, `after`, `checkout_sha`, `project.git_http_url`;
  properties `branch` (strips `refs/heads/`, None for anything else), `is_deletion`,
  `sha` (`checkout_sha or after`, validated `^[0-9a-f]{40}$|^[0-9a-f]{64}$`).
  `WebhookOutcome(accepted: bool, reason: str | None, statusUrl: str | None)`.
- `api/models/common.py`: `ANNOTATION_GIT_REVISION = "serverless.platform/git-revision"`.
- Tests: `tests/test_models.py` - `ref` parsing (branch / tag / weird), deletion marker,
  SHA validation, extra payload fields ignored.

### 3. Settings and chart

- `api/core/config.py`: `public_url: str = ""` (env `SERVERLESS_PUBLIC_URL`; validator
  strips a trailing `/`, requires `http(s)://` when set).
- `charts/serverless-api/templates/api/deployment.yaml`: `SERVERLESS_PUBLIC_URL` =
  `https://` + the same expression `route.yaml` uses for the host. `values.yaml` comment.
- `api/core/paths.py`: `webhook_url(settings, group, name)`.
- Tests: `tests/test_chart_values.py` renders the env var; `tests/test_base_path.py` covers
  the URL with and without a base path and with `public_url` unset.

### 4. Create, update, read, rotate carry the token

- `FunctionService.accept`: `token = new_webhook_token()`; schedule
  `functools.partial(self.create, webhook_token=token)` as the work; echo
  `webhook=WebhookView(...)` on the 202 (through `**extra` on `accepted`).
- `FunctionService.create`: `extra_secrets = plan.replicated + [build_webhook_secret(...)]`.
- `FunctionService.update`: `token = existing.get("webhook_token") or new_webhook_token()`
  (self-heals a pre-feature function); same `extra_secrets` append; `revision` carried
  forward when `gitRepo`/`branch`/`path` are unchanged, else None (step 6 wires it into the
  KSVC).
- `FunctionOffering.describe`/the GET path: read the webhook Secret in the same per-region
  read thread that already reads the pull Secret for its username; project `webhook` and
  `revision` (from the annotation) onto `FunctionResponse`.
- `FunctionService.rotate_webhook(group, name, user)` + router
  `POST /{name}/webhook/rotate`: `load_existing` (authorizes), new token, fan out the
  Secret with `region_apply`-style owner stamping to every region the function runs in
  (skip absent, as `apply_build_objects` does), return `WebhookView`. Not a 202: the
  write is one small Secret and the caller needs the token now.
- Tests: `tests/test_workload_service.py` - create applies the Secret in every region;
  update keeps it; update of a pre-feature function creates it; GET returns `webhook`;
  list does not; rotate replaces the value in every region and answers with the new one.
  `tests/test_api.py` - the router shape and that a container has no rotate (`404`).

### 5. The second way into `/build`

- `api/auth/deps.py`: `GITLAB_TOKEN_HEADER = "X-Gitlab-Token"`, `GITLAB_EVENT_HEADER`;
  `build_caller(request) -> Principal | WebhookCaller` - a bearer that validates wins;
  otherwise, if the GitLab header is present, `WebhookCaller(token, event)`; otherwise
  raise the same `UnauthenticatedError` as `require_auth`. Define it in terms of
  `optional_auth` so `dependency_overrides` in tests replace the bearer half only, the
  way the stream dependency already does.
- `api/routers/functions.py` - `build_function`: `caller: BuildCaller`,
  `event: GitLabPushEvent | None = Body(None)`, `responses={200: {"model": WebhookOutcome}}`.
  A `Principal` -> `svc.accept_build(...)` (unchanged contract). A `WebhookCaller` ->
  `svc.accept_webhook(group, name, caller, event, background)`; a `WebhookOutcome` is
  returned as a `JSONResponse(200)`, a `FunctionResponse` as the 202.
- `FunctionService.accept_webhook`:
  1. `existing = await self._engine.load_existing(name, FUNCTION, principal, group)` with
     `principal = Principal(subject=f"webhook:{group}/{name}", username="webhook",
     groups=[group])` - group-scoped, non-admin, so the tenancy check runs as it does for
     any caller. `NotFoundError` -> `UnauthenticatedError` (no existence leak).
  2. `stored = existing.get("webhook_token")`; `not stored or not
     hmac.compare_digest(stored, caller.token)` -> `UnauthenticatedError`.
  3. Event checks, each an early `WebhookOutcome(accepted=False, reason=…)`:
     event kind not `Push Hook`; `event.branch is None` (tag or unknown ref);
     `event.is_deletion`; `event.branch != existing["branch"]`; repository mismatch
     (`common.names` gets `same_repository(a, b)`: scheme+host+path, case-insensitive
     host, trailing `.git`/`/` ignored).
  4. `req = self._build_request(name, group, existing, owner=existing["owner"],
     revision=event.sha)`; `_assert_runtime` as the bearer path does.
  5. `background.add_task(run_background, self.build_pinned, group, name, existing, req)`;
     return `self._engine.accepted(...)` with `revision=event.sha`.
  Log the delivery at info with `X-Gitlab-Webhook-UUID` and the decision, never the token.
- `FunctionService._build_request` gains `owner` and `revision` keyword arguments (the
  bearer path passes `user.username`; both default to today's behaviour).
- Tests: `tests/test_api.py` - the header routes to `accept_webhook`; bearer + header ->
  bearer; neither -> 401; an ignored push is a 200 with `accepted: false`; a scheduled
  one is the usual 202. `tests/test_function_webhook.py` (new) drives `accept_webhook`
  over the fake clusters: wrong token, missing token, absent function all 401; branch
  mismatch / tag / deletion / repo mismatch each name their reason; a match schedules a
  build whose `BuildRequest.revision` is the SHA and whose owner label is the function's.

### 6. Pin the SHA, persist it, and let the bearer path unpin

- `WorkloadService.apply_build(name, group, plan, *, trigger=True, revision=None)`:
  after the per-region apply, when `revision is not None` patch the KSVC metadata
  annotation (a merge patch of `metadata.annotations` only - `stamp_pull` is the model,
  minus its template half, so no revision is cut); when `revision is None` and the
  annotation is present, remove it (`null` in the merge patch). Call `builder.trigger`
  only when `trigger` is true.
- `FunctionService.build_pinned(...)`: `apply_build(..., trigger=False, revision=sha)`.
- `FunctionService.build` (bearer): `revision=None` always; `trigger = existing.get("revision") is None`
  (a pinned function's unpin is the spec change; an unpinned one needs the nonce as today).
- `ApplyRequest.revision` + `build_ksvc(revision=...)` stamps the annotation on `PUT`;
  `region_read.existing_state` reads it as `existing["revision"]`; `_build_request`
  defaults `revision` to `existing.get("revision")` so a `PUT` that keeps the source also
  keeps the pin in the plan it re-applies (no spurious rebuild).
- Tests: `tests/test_workload_service.py` - webhook build applies `revision=sha` to every
  region's Image and never touches a Build; annotation written, per region; redelivery
  applies an identical Image; bearer rebuild on a pinned function drops the annotation,
  re-applies `revision=branch` and does not trigger; bearer rebuild on an unpinned one
  triggers as before; `PUT` without a source change re-applies the pinned revision;
  `PUT` with a branch change drops it. `tests/test_kpack_build.py` already covers the
  manifest.

### 7. Docs and changelog

- FUNCTIONS.md: a "Git webhook" section (GitLab setup: URL, secret token, push events,
  SSL verification on; the `webhook` object; what a push does and does not do), the
  `revision` field, the rotate endpoint; update the "Take a commit SHA" row and the
  "build a pushed commit now" bullet under *Building again without changing anything*.
- BUILDING.md: `webhook` row in *Every write path is a full server-side apply* moves from
  *planned* to real; the reconstruction table gains `revision`; *What causes a new Build*
  `COMMIT` row; strike open question 5.
- API.md: endpoint table (+ rotate), auth section (the second credential on `/build`),
  error table (`401` on the webhook path).
- ARCHITECTURE.md: the open-questions bullet; secrets section lists `{workload}-webhook`.
- DEPLOYING.md: `SERVERLESS_PUBLIC_URL`; note that GitLab must reach the API's Route
  (in an airgapped install that is an allow-list entry on GitLab's outbound side, not a
  NetworkPolicy here - the API's Route is already public).
- CHANGELOG.md: `Added (git webhook)`.
- Delete this file.

**Effort:** steps 1-4 are additive and small (a Secret and two fields). Step 5 is the
endpoint. Step 6 is the one that changes an existing path (`apply_build`) and is where
the tests earn their keep. Roughly two to three days including docs.

## Security notes

- **Constant-time compare** (`hmac.compare_digest`) against the stored token; both
  values are the same length by construction.
- **Entropy:** `secrets.token_urlsafe(32)` - 256 bits, URL-safe, fits GitLab's field.
- **What the token buys an attacker:** one build of the stored branch at a SHA they name,
  in the function's own namespace, with the function's own git credential - a commit the
  stored token can already fetch. No desired state changes: not the branch, not env, not
  the image tag. Repository and branch are checked against what is stored, so the token
  cannot be pointed at another repository. It cannot read anything back: the 202 body is
  the same redacted response any authenticated reader sees, and `revision` is what the
  caller just sent.
- **Never logged, never in `repr`:** `WebhookCaller.token` and `WebhookView.token` are
  `repr=False`, as `gitToken` is. Log the delivery UUID and the decision instead.
- **Body bound:** the push event is parsed by a lenient model; the router should cap the
  body at a sane size (GitLab sends up to 20 commits; 1 MiB is generous).
- **No existence leak:** every failure before the token matches is `401`, including a
  function that does not exist.
- **Idempotent by data**, so GitLab's retries and two API replicas are safe without a
  lease (rule 4).
- **Rotation** invalidates the old token in every region on the next apply; there is no
  grace period and none is needed.

## Deferred

- **Monorepo path filter.** A push to the branch rebuilds every function that builds from
  that branch, whatever `path` they build from. GitLab's payload lists changed files per
  commit (first 20 commits only), so "ignore a push that touched nothing under `path`" is
  a small, optional follow-up. Not in this phase: a false negative (a change outside
  `path` that matters - a shared lockfile at the root) is worse than an extra build.
- **GitHub / generic providers.** `X-Hub-Signature-256` is an HMAC over the body with the
  same stored token as the key; add a second branch to `build_caller` and a
  `GitHubPushEvent` with the same properties. The storage, the pin and the build path are
  provider-neutral already.
- **Delivery record.** Exposing the last delivery (UUID, SHA, outcome, time) on the GET
  would help debugging a hook; it needs a place to write it (an annotation would do).
  Wait for a real need.
