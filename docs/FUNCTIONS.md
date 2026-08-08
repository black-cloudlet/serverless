# Functions (FaaS)

Running a function built from source. How the image is built is BUILDING.md;
what functions share with containers is ARCHITECTURE.md.

## Contents

- [Overview](#overview)
- [API - create & update](#api---create--update)
- [Building again without changing anything](#building-again-without-changing-anything)
- [Function Status Resolution](#function-status-resolution)

## Overview

**Inputs (request body):**

| Field | Required | Notes |
|-------|----------|-------|
| `gitRepo` | yes | HTTPS Git repository URL (internal Git, airgapped). `http://` is accepted for an internal host, but sends the token in the clear. SSH/scp-style refs (`git@host:org/repo.git`) are rejected: the clone authenticates with a basic-auth Secret, which only applies over http(s). Credentials embedded in the URL are rejected rather than stripped - it is written verbatim to the kpack `Image`, which is readable far more widely than the Secret; send the token as `gitToken`. |
| `branch` | no | Branch / ref to build. Defaults to **`main`**, and is *replaced* on `PUT` - omitting it returns the function to `main` and rebuilds. |
| `path` | no | Directory inside the repository holding the application, for a monorepo (e.g. `services/api`). Defaults to the repository root. Surrounding `/` are stripped; `..` is rejected. Changing it rebuilds. |
| `gitToken` | yes on create | Repo access token; used to clone and **stored** in the `{workload}-git` Secret so a later edit can rebuild without re-sending it. Never returned on read (see ARCHITECTURE.md: Secrets Management). The one keep-on-omit field on `PUT`: omitting it reuses the stored token, sending it rotates it (and rebuilds). |
| `runtime` | yes | One of the platform's configured runtimes (the chart ships `python`, `go`, `node`). The set is **data**: a ConfigMap mounted as a YAML file (`api/services/builder/runtimes.py`), validated against the live registry in the service layer and advertised on `GET /api/v1/functions/info`. Adding a runtime is a ConfigMap edit, not a code change. |
| `version` | no | Language version, which must be one of that runtime's advertised `versions`. Omitted takes the platform `defaultVersion` for the runtime - never the buildpack's own default, which drifts with the buildpackage. A runtime offering no choice (empty `versions`, or no `versionEnv`) **rejects** a supplied version rather than ignoring it. Replaced on `PUT` like `branch`, and changing it rebuilds. |
| `name` | yes | Logical workload name (DNS-1123). `{name}-{group}` must fit in 63 characters together - see `naming` on `GET /api/v1/functions/info`. |
| `sites` | no | Which sites to deploy to; defaults to all of them (HA). Each of them **builds its own copy**, into its own registry - a site builds what it runs (BUILDING.md: Ownership: API vs Build Service). |
| `port` | no | Container port the workload listens on. Defaults to **8080** - what Knative injects as `$PORT`, and what most images serve on - and is stamped explicitly on the KSVC so a read reports it rather than leaving it to convention. Send it only when the image serves elsewhere: nothing can detect that, so a mismatch shows up as a revision that never becomes ready (the cause lands on the per-site `error`), not as a rejected request. Replaced on `PUT`, so omitting it returns the workload to 8080. Bounds and the default are advertised on `GET /api/v1/functions/info`. | Identical to a container's: an app either serves on 8080 or it does not, and which offering built it changes nothing. It is **not** a build input, so changing it costs a revision, not a rebuild.
| `env`, `files`, `scaling` | no | Shared capabilities, see ARCHITECTURE.md: Shared capabilities. |

**Build flow (kpack / Cloud Native Buildpacks):**

The API does not *run* builds; it **declares** them. Full detail is in BUILDING.md - this
is the shape:

1. The API validates the request and returns **`202 Accepted`**. Nothing is built yet.
2. In the background it applies, to the **local** site only, the function's kpack `Image`
   plus the per-function build `ServiceAccount`; the `{workload}-git` Secret holding
   `gitToken` goes to **every target site**, so each can build and rebuild on its own.
3. **kpack** does the rest on its own: clone `gitRepo@branch`, run the runtime's `Builder`
   (the mirrored Paketo stack and buildpackages - ARCHITECTURE.md: Airgapped Considerations),
   and push to **that site's** registry at
   `{site registry base}/{group}/{name}:{branch}` (BUILDING.md: Registry layout).
4. In the same pass as step 2, the API applies the **KSVC** to every target site, pointing
   at that **tag**. Until a build lands there is no image to pull, which is why a new
   function reads `Building` rather than `Degraded` (see *Function Status Resolution*).
5. When a build finishes, **that site's** build controller - a separate Deployment watching
   `Image.status.latestImage` - rolls the resulting **digest** onto the function's KSVC
   **there** (BUILDING.md: Digest propagation). After the create, it is the only thing that
   writes that field.

> The tag is a projection of the branch, not the commit: an OCI tag may not contain `/`,
> so `feature/login` pushes to `feature-login` while the build still compiles that exact
> ref. Each site builds and pulls within itself, so nothing crosses a site boundary to run
> a function - at the cost of the two sites holding different digests of the same commit.

```mermaid
sequenceDiagram
    autonumber
    participant U as Client
    participant API as FastAPI API
    participant ZA as Site A (kpack + Knative + registry)
    participant ZB as Site B (kpack + Knative + registry)

    U->>API: POST /api/v1/groups/{group}/functions (git, runtime, ...)
    API->>API: AuthN (JWT) + AuthZ (group) + pre-flight
    API-->>U: 202 Accepted { overallStatus: "Pending", statusUrl }
    par Deploy to all target sites, each at its OWN registry's tag
        API->>ZA: apply Image + build SA + KSVC + DomainMapping
        API->>ZB: apply Image + build SA + KSVC + DomainMapping
    end
    par Each site builds into its own registry and publishes to itself
        ZA->>ZA: clone, build, push @digest -> its controller applies the KSVC
        ZB->>ZB: clone, build, push @digest -> its controller applies the KSVC
    end
    U->>API: GET {statusUrl} (poll)
    API-->>U: 200 { overallStatus: "Building" -> "Ready" }
```

## API - create & update

Request:

```json
{
  "name": "image-resizer",
  "gitRepo": "https://git.internal/team/image-resizer.git",
  "branch": "main",
  "gitToken": "<repo-access-token>",
  "runtime": "python",
  "env": [ { "name": "MAX_PX", "value": "2048" } ],
  "scaling": { "minScale": 0, "maxScale": 20 }
}
```

Response `202 Accepted` (deploy runs in the background; poll `statusUrl`):

```json
{
  "name": "image-resizer",
  "type": "function",
  "runtime": "python",
  "hostname": "image-resizer-team.serverless.example.com",
  "overallStatus": "Pending",
  "sites": [],
  "statusUrl": "/api/v1/groups/team/functions/image-resizer"
}
```

Then `GET /api/v1/groups/team/functions/image-resizer` once Ready:

The response is a **`FunctionResponse`** - flat, mirroring the `FunctionCreate`
body (secrets redacted) with the live status alongside:

```json
{
  "name": "image-resizer",
  "group": "team",
  "type": "function",
  "hostname": "image-resizer-team.serverless.example.com",
  "overallStatus": "Ready",
  "size": "small",
  "createdAt": "2026-06-21T15:00:00+03:00",
  "runtime": "python",
  "gitRepo": "https://git.example.com/team/image-resizer.git",
  "branch": "main",
  "scaling": { "minScale": 0, "maxScale": 3, "metric": "concurrency", "target": 100 },
  "env": [
    { "name": "LOG_LEVEL", "value": "debug", "secret": false },
    { "name": "API_KEY", "value": null, "secret": true }
  ],
  "files": [
    { "mountPath": "/etc/app/config.yaml", "readOnly": true, "secret": false,
      "content": "level: debug\n" },
    { "mountPath": "/etc/app/token", "readOnly": true, "secret": true, "content": null }
  ],
  "sites": [
    { "site": "central", "status": "Ready", "revision": "image-resizer-00001", "replicas": 2 },
    { "site": "south", "status": "Ready", "revision": "image-resizer-00001", "replicas": 1 }
  ]
}
```

A **`ContainerResponse`** is the same idea mirroring `ContainerCreate`: instead of
`gitRepo`/`branch`/`runtime` it carries `image` and `registryUsername`. (Functions
expose **no image** - the built image is an internal artifact; the client deals in
source, not images.)

> **Shape.** Each offering has its own response model (`FunctionResponse` /
> `ContainerResponse`) so the response is the same shape as the create body - no
> irrelevant fields (a container never shows `gitRepo`; a function never shows
> `registryUsername`). Both share `WorkloadBase` (name, group, type, hostname,
> overallStatus, size) with the list summary. `hostname` is the bare external host
> (no scheme), mirroring the create body's `hostname`; reach the workload at
> `https://{hostname}`. The desired-state fields (`scaling`, `env`, `files`, plus
> the source fields) are read from the **local site** (uniform across sites); the
> per-site `sites[]` status/`replicas` come from fanning out to every site. Live
> **usage** is not here - see the `/stats` endpoint below.
>
> **Redaction & keep-on-write.** Secret material is never returned: secret-backed env
> values and secret file contents come back `null` with `secret: true`; the **git token**
> is omitted and the **registry token** is not shown (`registryUsername` is). Non-secret env
> values and non-secret file contents (from the workload's ConfigMap) are returned in full.
> Because the read is redacted, `PUT` treats a **redacted/absent secret field as "keep the
> stored value"**: a `secret: true` env var or file sent without a value/content keeps what's
> stored; echoing the stored `registryUsername` back without a token keeps the credential -
> re-keyed to the current image's registry if the image moved (sending a *different* username
> without a token is a `400`, since there's no token to rotate with); omitting `gitToken` keeps
> the stored git token. So the redacted GET body can be sent straight back on `PUT` without wiping a secret.
> To change a secret, send its new value; to remove an env var or file, drop it from the list;
> to make a private image public, send **neither** registry cred (dropping the username, like
> dropping an env var, removes the pull secret). `scaling.target` reflects the *effective*
> target deployed (an omitted cpu/memory target shows `70`).
>
> **Keep is `null`, not `""`.** Only an omitted/`null` value is a keep; an empty string is a
> real value that **sets** the secret to empty. So a secret var/file must be sent with its
> `value`/`content` omitted (`null`) to keep it - never `""`. A **new** secret (one not
> already stored) sent with a `null` value is a synchronous `400` (`"…has no value and none is
> stored to keep"`): keep only applies to something already stored, so a new secret must carry
> its value. A non-secret var/file always requires a value. These checks run in the update
> pre-flight, so they surface as an immediate `400`, not a background deploy failure.
>
> **Live status.** `replicas` is the autoscaler's live scale
> (`Revision.status.actualReplicas`), best-effort and `null` when it cannot be read.
> It rides along on a read the per-site error detail needs anyway, which is why it
> is on this response and live **usage** is not: measuring usage is a PodMetrics
> call per site, and the full GET is not the endpoint to poll.
>
> **Polling live state: `GET .../{name}/stats`.** A lightweight view of what
> changes on its own - the rollup, replica count and resource usage, nothing else:
>
> ```json
> {
>   "overallStatus": "Ready",
>   "replicas": 3,
>   "usage": { "cpu": "210m", "memory": "355Mi" },
>   "sites": [
>     { "site": "central", "status": "Ready", "replicas": 2,
>       "usage": { "cpu": "120m", "memory": "180Mi" } },
>     { "site": "south", "status": "Ready", "replicas": 1,
>       "usage": { "cpu": "90m", "memory": "175Mi" } }
>   ]
> }
> ```
>
> `overallStatus` matches the full GET's, `Building` included - the build is still
> read, it is just not a field here. Usage covers each pod's user container only,
> never the queue-proxy sidecar, and is `null` when scaled to zero or the metrics
> API could not be read. The top-level totals are summed across sites **before**
> rounding, so they need not equal the sum of the printed per-site figures; and a
> total is `null` if any site could not be measured, rather than one quietly
> missing a site. Usage is never fresher than the cluster's metrics-server scrape.

### Editing a workload: `PUT` request recipes

All `PUT`s are a **full replace** of the mutable spec: whatever you send is the new
desired state, with the keep-on-write rules above for secrets. The list of `env`/`files`
entries you send is the complete set (drop one to remove it). Each example is a body for
`PUT /api/v1/groups/{group}/{containers|functions}/{name}`.

**Config-only edit - keep every secret (echo the redacted GET straight back).** The
secret env value and the registry token were `null` in the GET; sending them back unchanged
keeps them:

```json
{
  "image": "reg.example.com/team/orders:1",
  "registryUsername": "svc-team",
  "scaling": { "minScale": 1, "maxScale": 5 },
  "env": [
    { "name": "LOG_LEVEL", "value": "debug", "secret": false },
    { "name": "API_KEY", "secret": true }
  ]
}
```

**Rotate one secret, keep another, remove a third.** `API_KEY` gets a new value; `DB_URL`
is kept (no value); the previously-present `OLD_FLAG` is simply absent, so it's removed:

```json
{
  "image": "reg.example.com/team/orders:1",
  "registryUsername": "svc-team",
  "env": [
    { "name": "API_KEY", "value": "sk-new-value", "secret": true },
    { "name": "DB_URL", "secret": true }
  ]
}
```

**Add a new secret - must carry a value** (a new `secret: true` with no value is a `400`):

```json
{ "image": "reg.example.com/team/orders:1", "registryUsername": "svc-team",
  "env": [ { "name": "NEW_TOKEN", "value": "s3cret", "secret": true } ] }
```

**Rotate registry credentials** (username + token together):

```json
{ "registryUsername": "svc-team", "registryToken": "new-registry-pat" }
```

**Move to a different registry, keep the same creds** (username only → kept creds are
re-keyed to the new image's registry):

```json
{ "image": "reg-b.example.com/team/orders:2", "registryUsername": "svc-team" }
```

**Make a private image public** (drop both registry creds → the pull secret is removed):

```json
{ "image": "docker.io/library/nginx:1.27" }
```

**Rebuild a function from a new branch - no token needed** (the stored git token is reused).
`gitRepo` and `runtime` are required on every function `PUT`, as on create - the body is
the full desired state, so they are re-sent unchanged rather than carried forward:

```json
{
  "gitRepo": "https://git.internal/team/image-resizer.git",
  "runtime": "python",
  "branch": "release",
  "scaling": { "minScale": 0, "maxScale": 3 }
}
```

**Rotate the git token** (sending it also triggers a rebuild). Note that omitting `branch`
here would reset it to `main`, so send the branch you are on:

```json
{
  "gitRepo": "https://git.internal/team/image-resizer.git",
  "runtime": "python",
  "branch": "release",
  "gitToken": "ghp_new-token"
}
```

## Building again without changing anything

A `PUT` rebuilds only when a build **input** changes (or the token rotates), because
re-applying an unchanged spec is a no-op kpack does not build from. That leaves the
opposite need unserved: build the *same* definition again, against today's base image and
dependencies. That is a `POST`, and it takes **no body**:

```
POST /api/v1/groups/{group}/functions/{name}/build   ->   202 Accepted
```

Every input comes back off the workload itself - `gitRepo`, `branch`, `path`, `runtime`
and `version` from the KSVC's annotations, the token from the `{workload}-git` Secret -
which is the same reconstruction a site that has never built the function does after a
switchover (BUILDING.md: Reconstruction after switchover). Nothing is accepted from the
request: a rebuild that took inputs would be a `PUT` in disguise.

Use it to:

- pick up a **base-image or buildpack** change on a function nobody is editing (kpack does
  this on its own for `STACK`/`BUILDPACK` updates; this is the on-demand version);
- **retry a failed build** without inventing a spec change to force one;
- build a **pushed commit now** rather than when kpack next re-resolves the branch.

The response is the same `Pending` 202 as create and update, with the same `statusUrl`, so
a client polls one place: `GET .../functions/{name}` reports `build.state` as `Building`
and then `Ready` or `Failed`.

What it deliberately does **not** do:

| | |
|---|---|
| Touch the workload | Nothing about the desired state changes, so no KSVC is applied and no revision is spawned. The running revision keeps serving its current digest until the new one is rolled out (BUILDING.md: Ownership: API vs Build Service) - as for any build kpack starts on its own. |
| Take a commit SHA | A rebuild builds the branch head, which is what create and update do. Pinning an exact commit is the job of the git webhook, which is **not implemented yet** (`BuildRequest.revision` already carries the field for it - BUILDING.md: Who writes the ksvc image). |
| Change the spec | Send a `PUT` for that. A rebuild is the one function write that carries no desired state at all. |

**Errors.** `404` if there is no such function (including a *container* of the same name -
`{name}-{group}` is shared by both offerings). `400` if there is nothing to build with: no
stored git token (send one with a `PUT`), or a `runtime` that has since been removed from
the runtimes ConfigMap. Both are decided synchronously, before the `202`.

And `GET /api/v1/groups/team/functions` to list the group's functions - general
info only (no live usage/replicas; use the single-workload GET for those):

```json
[
  {
    "name": "image-resizer",
    "group": "team",
    "type": "function",
    "hostname": "image-resizer-team.serverless.example.com",
    "overallStatus": "Ready",
    "size": "small",
    "createdAt": "2026-06-21T15:00:00+03:00",
    "sites": ["central", "south"]
  }
]
```

> The list **fans out to all sites** and merges by workload name (best-effort):
> each workload's `sites` lists the sites that returned it and `overallStatus` is
> rolled up across them (`Ready`/`Deploying`/`Degraded`, or `Terminating` while a
> workload is being deleted). A site that is unreachable is skipped; only if
> **every** site is down does the call fail (502). It returns general info only (no
> live replicas/usage) - use the single-workload GET for per-site live health.

## Function Status Resolution

`GET` on a function resolves status **build-first, deployment-second**:

```
GET /functions/{name}
  1. look up the Image in the LOCAL cluster
       Building -> overallStatus "Building"
       Failed   -> overallStatus "Degraded" (+ the condition message on build.message)
  2. no Image found, or the build succeeded
       -> fall through to the Knative Service status
```

The `build` object on the response carries `state` and `message` only. Per-phase build
logs are not on this endpoint - they live in the `Build`'s pod, one container per
lifecycle phase (BUILDING.md: Inside the build pod).

Two properties this gives us:

- A function whose first build is still running reports **building** rather than a
  confusing "not ready" ksvc state.
- A site with **no** `Image` - one the function was never deployed to, or whose build
  objects have not landed yet - contributes nothing, and the handler falls through to its
  ksvc. The absence of a build is not an error.

**Build state is per site.** Every site builds its own copy, so it is read in the same
per-site thread that already fetches that site's KSVC - no extra round trip, and no way to
attribute one site's build to another. Each `sites[]` row folds against **its own** build:
a build running in one site says nothing about whether another site's image exists, and a
shared verdict would mask a real failure next to a healthy neighbour.

The workload-level `build` is then rolled up (`ksvc_state.roll_up_builds`): a **failure
anywhere wins**, carrying its own message, then `Building`, then whatever is left.
Reporting `Ready` because the other site managed it would hide the site that did not.

**As implemented.** `KpackBackend.status` returns `None` for a site with no `Image`, and
`with_build_status` folds the rollup:
`Building` wins over whatever the ksvc says, `Failed` reports `Degraded`, and anything else
hands the verdict back to the ksvc. The response carries a `build` object
(`state`/`image`/`message`), so a failed build explains itself instead of surfacing as a
bare image-pull error. `Building` maps to HTTP `202`, like `Deploying`.

The first build is the case that motivates the ordering: the ksvc is already applied and is
failing to pull an image kpack has not pushed yet. Read deployment-first, every new function
would report `Degraded` for the whole of its first build.

**The rule applies to every surface that reports status, not just the rollup.** Two of them
used to escape it, and both showed a red failure for a perfectly normal build:

- The **per-site rows**. `sites[]` is read straight off each ksvc, so a response could say
  `Building` in `overallStatus` and `Failed` - `Unable to fetch image "..."` in the `sites`
  table directly below it. While a build is in flight a failing site now reports `Building`
  with `error: null` (`ksvc_state.sites_with_build_status`): that pull failure *is* the
  running build, not an independent one. Only a **running** build masks anything - a failed
  build leaves the rows untouched, because then the image genuinely never arrives.
- The **listing**. `GET .../functions` had no build read at all, so every new function
  was `Degraded` on the list while being `Building` on its own GET. It now folds the same
  way, using `BuildBackend.statuses` - one label-selected read per site for the whole
  group, keyed by object name (`{name}-{group}`), paired with that site's ksvc read rather
  than chained onto it, and rolled up across the sites that answered. A listing that cannot
  read kpack falls back to the ksvc statuses, exactly as a single GET does.

`Building` is therefore a *site* status as well as a workload one, and
`GET /api/v1/functions/info` publishes it in both vocabularies (`statuses.workload` and
`statuses.site`) so a client hardcodes neither.

---