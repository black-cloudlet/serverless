# Code review: error handling, correctness, efficiency

Review of the application code on `claude/code-review-error-analysis-14oe0t`
(`api/`, `common/`, ~7,200 lines). Findings only — nothing here is fixed.

Baseline: `pytest` is green (289 passed) and `ruff check .` is clean, so
everything below slips past the current gates.

Verification note: findings marked **verified** were reproduced by driving the
real service objects against fake clusters. `pyproject.toml` requires Python
>= 3.14, which is not available here, so the repro ran on 3.13 with the runtime
dependencies installed directly. Nothing in the findings depends on the
interpreter version.

---

## Bugs

### 1. `GET .../functions/{name}` always returns 500 — CRITICAL, verified

`api/services/workloads.py:1043` reads `spec.path`:

```python
return FunctionResponse(
    ...
    path=spec.path if spec else None,
```

`spec` is a `WorkloadSpec` (`api/models/common.py:433`), and that model has no
`path` field — only `scaling, env, files, port, registryUsername, gitRepo,
branch`. `describe.parse_spec` (`api/services/describe.py:247`) *passes*
`path=meta.get(ANNOTATION_GIT_PATH)`, but Pydantic's default `extra="ignore"`
drops it silently, so the attribute never exists:

```
AttributeError: 'WorkloadSpec' object has no attribute 'path'
  File "api/services/workloads.py", line 1043, in get
    path=spec.path if spec else None,
```

Blast radius is the whole function lifecycle, not one field:

- `GET /api/v1/groups/{group}/functions/{name}` is unconditionally a 500.
- That URL is the `statusUrl` every function create/update returns on its 202,
  so a client can never observe the outcome of *any* function deploy.
- `build`, `overallStatus`, per-site status and `_with_build_status` are all
  unreachable as a result.

Containers are unaffected — `ContainerResponse` doesn't read `spec.path`.

Introduced by `f0d471a` ("Add an in-repository path so a monorepo can hold
several functions"), which added the `path=` read and the `parse_spec` argument
but not the model field.

Why the suite misses it: `tests/test_api.py` stubs `FunctionService` entirely,
and `tests/test_auth_and_deployer.py` only ever calls
`engine.get("container", ...)` — `engine.get("function", ...)` is never
exercised anywhere.

### 2. DELETE reports 404 during a total site outage, after destroying build state — HIGH, verified

`api/services/workloads.py:1186`:

```python
statuses = await self.deployer.fanout(targets, remove)
if offering == OFFERING_FUNCTION:
    await asyncio.to_thread(self._delete_build_objects, self.deployer.local_cluster(), oname)
if all(s.error is not None for s in statuses):
    raise NotFoundError(f"{kind} '{name}' not found")
```

Two problems.

**a. "all sites errored" is treated as "does not exist."** Every other
multi-site check in this file is scrupulous about this — `assert_host_available`,
`assert_workload_absent`, `load_existing` and `get` all route through
`_assert_all_sites_checked`, which raises `ServiceUnavailableError` (503)
precisely because *a missing answer is not evidence of absence* (the module's
own words, line 910). `delete` is the one path that doesn't. With both clusters
unreachable the caller gets `404 not found` for a workload that is still running.

**b. The build objects are deleted before that check.** `_delete_build_objects`
runs unconditionally, one line earlier. So a delete that reports "this never
existed" has already removed the kpack `Image`, the build `ServiceAccount` and
the `{workload}-git` Secret from the local site. Reproduced with both sites down:

```
NotFoundError (HTTP 404): function 'app' not found
build objects deleted on the local site anyway:
  [(KPACK_IMAGE, 'fn-app-team'), (SERVICE_ACCOUNT, 'fn-app-team'), (SECRET, 'app-team-git')]
```

The KSVC survives and keeps serving; its build inputs are gone. Because the git
token lived only in that Secret, a later update can't rebuild without the client
re-supplying it — which is exactly the recovery story `BuildPlan.replicated`
exists to protect.

Same swallow also hides authorization: `_assert_access` raises `ForbiddenError`
*inside* `remove`, so `fanout` converts it to a per-site error string rather
than propagating. A cross-group hit therefore reports 404 (acceptable on its
own) but still ran the build-object deletion first.

### 3. Binary secret files are impossible to upload, and fail as a 500 — HIGH, verified

`api/services/files.py:114` decodes uploaded content with `surrogateescape`:

```python
raw = base64.b64decode(f.contentBase64).decode("utf-8", "surrogateescape")
```

`api/services/resources.py:48` then re-encodes it strictly:

```python
encoded = {k: base64.b64encode(v.encode("utf-8")).decode("ascii") for k, v in data.items()}
```

Any non-UTF-8 byte produces lone surrogates on the way in and blows up on the
way out:

```
UnicodeEncodeError: 'utf-8' codec can't encode character '\udc82' in position 1: surrogates not allowed
```

`contentBase64` exists so a caller can mount a keystore, a DER certificate or a
`.p12` — none of which are UTF-8. So the field cannot carry the content it was
added for, and the failure is an unhandled 500, not a 400.

The same round-trip breaks *keep-on-update* for any secret already holding
binary: `_secret_data` (`workloads.py:1098`) decodes stored values with
`surrogateescape`, hands them to `resolve_files` as `kept`, and the re-encode
fails identically. The `# skip an undecodable key` guard at line 1099 only
catches `b64decode` failures, not the surrogate re-encode.

### 4. Malformed `contentBase64` returns 500 instead of 400 — MEDIUM, verified

`api/services/describe.py:56` decodes without a guard:

```python
content = base64.b64decode(f.contentBase64).decode("utf-8", "surrogateescape")
```

`resolve_files` is careful here — it catches `ValueError` and re-raises
`ValidationError` (`files.py:115`) — but `redact_files` runs *first*.
`_echo(spec)` is evaluated as an argument expression to `accept_create` /
`accept_update`, so it executes before the `validate_spec` call inside them:

```python
return await self._engine.accept_create(..., **self._echo(spec))
```

A client sending `"contentBase64": "abc"` gets:

```
POST /api/v1/groups/dev/containers -> 500 Internal Server Error
```

where the ordering-correct answer is the 400 the code clearly intends.

### 5. Unhandled exceptions bypass the error envelope — MEDIUM

`common/web.py:146` registers handlers for `APIError`, `RequestValidationError`
and `StarletteHTTPException` — and nothing else. Every 500 above therefore
returns Starlette's plain-text `Internal Server Error`: no `error.code`, no
`details`, and no `requestId`.

That undercuts two things the codebase deliberately built. `GET
/api/v1/{offering}/info` publishes `errorCodes` as the complete vocabulary an
envelope can carry (`errors.error_catalog`, walked off the subclasses precisely
so it can't go stale), and `RequestIDMiddleware` threads a correlation id through
every response and log line. A client that switches on the envelope gets an
unparseable body exactly when something has gone wrong, with no id to grep for.

A catch-all `Exception` handler rendering `INTERNAL`/500 with the request id
would close this — and would have made findings 1, 3 and 4 diagnosable from the
response instead of only from the pod log.

### 6. A failed background deploy is invisible to the client — MEDIUM

`accept_create` returns 202 + `statusUrl`, then `run` (`workloads.py:297`)
swallows everything:

```python
except Exception:  # noqa: BLE001 - background work; surfaced via status polling
    logger.exception("background deploy failed for %s", args)
```

The comment says "surfaced via status polling", but when the deploy fails
*before anything is applied* — `SiteTotalFailure` from `aggregate`, the
`ValidationError` from `function.update:257` for a missing rebuild token,
`ServiceUnavailableError` from a pre-flight — there is nothing in the cluster to
poll. The `statusUrl` 404s forever and the reason exists only in a log line the
caller can't see.

Related dead code: `apply_workload` still computes and returns
`status_code_for(overall, created)` (207/202/201), and `run` discards the return
value. That mapping has had no consumer since create/update went async.

### 7. `container.update` can write a null registry username — LOW

`api/services/container.py:206`:

```python
elif spec.registryUsername and existing.get("registry_token"):  # keep: re-key
    pull = secret_svc.build_pull_secret(
        pull_name, labels, secret_svc.registry_of(image),
        existing.get("registry_username"),   # may be None
        existing["registry_token"],
    )
```

`secrets._registry_field` returns `None` when the stored dockerconfigjson has a
password but no username, and `build_pull_secret` interpolates it unchecked:
`f"{username}:{token}"` yields `base64("None:<token>")` and the JSON carries
`"username": null`. The branch is guarded by `spec.registryUsername` being
truthy, so the caller's username is known and available — it's just not the one
used.

Adjacent: `secrets.registry_of` maps a bare Docker Hub reference to `docker.io`,
but a dockerconfigjson entry for Docker Hub must be keyed
`https://index.docker.io/v1/`. Moot in an airgapped install pointed at the
internal mirror; the helper is written as a general-purpose one.

### 8. `load_existing` can raise `StopIteration` — LOW, latent (not reproduced)

`api/services/workloads.py:751`:

```python
present = {s.site for s in statuses if s.status == "Present"}
cluster = by_site[local if local in present else next(iter(present))]
```

`present` is non-empty whenever `found["obj"]` is set — except under one race.
`asyncio.to_thread` is not cancellable: when `asyncio.wait_for` times a site out,
that thread keeps running to completion. A thread that finishes just after the
timeout fires has already executed `found.setdefault("obj", obj)` while its
`SiteStatus` was discarded and replaced with `status="Timeout"`. `obj` is then
truthy, `present` is empty, and `next(iter(present))` raises `StopIteration`
inside a coroutine — a bare 500.

This is from reading the code; the window is narrow and I did not reproduce it.

---

## Inefficiency

### 9. Every create runs its cross-site pre-flight twice

The accept path and the background path each run the same probes. Instrumented
with two sites, one `POST .../containers`:

```
--- after the synchronous accept (before the 202) ---
  1x (site-a, get, DomainMapping, app-team...)   1x (site-a, get, Service, app-team)
  1x (site-b, get, DomainMapping, app-team...)   1x (site-b, get, Service, app-team)
--- after the background deploy ---
  2x (site-a, get, DomainMapping)  3x (site-a, get, Service)  1x apply Service  1x apply DomainMapping
  2x (site-b, get, DomainMapping)  3x (site-b, get, Service)  1x apply Service  1x apply DomainMapping
reads during accept: 4; total cluster calls: 14
```

The duplication:

- `accept_create` calls `assert_host_available` + `assert_workload_absent`;
  then `ContainerService.create` / `FunctionService.create` call
  `assert_workload_absent` *again* and `apply_workload` calls
  `assert_host_available` *again*.
- `accept_update` calls `load_existing` + `assert_host_available`;
  `apply_workload` re-runs `assert_host_available`.

Each repeat is a full fan-out across clusters — on the WAN hop between the two
sites this is the dominant cost of a deploy. It also buys nothing: the accept
path already failed closed, and re-checking doesn't close the TOCTOU window
either (the apply is still not atomic with the check). One pass, at accept, with
the result threaded into the background work, matches what the code already does
for `existing` in `accept_update`.

### 10. A thread pool is constructed per site, per request

`api/services/workloads.py:968`, inside `fetch`, which is itself already running
on a borrowed `asyncio.to_thread` worker:

```python
with ThreadPoolExecutor(max_workers=2) as pool:
    rev_f = pool.submit(self._revision, cluster, revision)
    usage_f = pool.submit(self._site_usage, cluster, oname)
```

Two thread creations and a pool teardown per site on *every* GET — and the GET
is the documented poll target, so a portal hits it in a loop. It also nests
pools inside the default executor, which is capped at `min(32, cpu+4)`; enough
concurrent polls and the outer pool starves. A single shared, process-lifetime
executor (or making these two reads async) removes both costs.

### 11. `get()` serializes reads that could overlap

`_describe_spec` (line 1020) and `_build_status` (line 1037) are separate
sequential `await asyncio.to_thread(...)` calls, and `_describe_spec` itself
loops `configmap_refs` one blocking read at a time (line 1111). On a function
with several file mounts that is a chain of round trips where the module already
demonstrates (line 968) it knows how to overlap them.

### 12. `_apply_to_site` re-reads the object it just applied

Line 697: `obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)` — but
`cluster.apply(ksvc)` returned the applied object at line 661, and `applied[0]`
is already used for `owner_reference`. If the intent is to observe status
written after the apply, that's worth a comment; the KSVC won't have reconciled
in that window anyway, which is why the response is `Deploying`.

### 13. Dead conditionals

Past line 999 in `get()`, `obj` cannot be `None` (`reps` is non-empty and every
entry holds one), yet lines 1028, 1036 and 1049 all guard `... if obj else ...`.
Harmless, but it implies a nullable that isn't, which is how a reader ends up
adding a fourth guard instead of noticing the invariant.

---

## Test gaps that let these through

Coverage is otherwise good — 289 tests, real settings and manifest round-trips,
a deliberate `conftest` that loads a genuine runtimes file. The gaps are
specific:

| Finding | Missing test |
|---|---|
| 1 | `engine.get("function", ...)` — the container variant is tested ~10 times, the function variant zero |
| 2 | `delete` when every site errors (asserting 503, and that build objects survive) |
| 3, 4 | any create/update carrying `contentBase64` — neither the binary nor the malformed case |
| 5 | that a non-`APIError` exception still renders the envelope |

Findings 1 and 4 are both cases where the accept path's argument evaluation or a
response projection runs code no test drives, while every test that *would* have
caught them stubs the service out one layer higher.

---

## Summary

| # | Severity | Finding |
|---|---|---|
| 1 | Critical | `GET .../functions/{name}` always 500s — `WorkloadSpec` has no `path` field |
| 2 | High | DELETE reports 404 on total outage, after deleting the build objects |
| 3 | High | Binary secret file content fails with a 500 (surrogate re-encode) |
| 4 | Medium | Malformed `contentBase64` → 500 instead of 400 (`_echo` runs before validation) |
| 5 | Medium | No catch-all handler: unhandled errors skip the envelope and the request id |
| 6 | Medium | Failed background deploys are unobservable; `status_code_for` is dead |
| 7 | Low | `container.update` can write `"username": null` into a pull secret |
| 8 | Low | `load_existing` `StopIteration` under a fan-out timeout race (latent) |
| 9 | Efficiency | Create/update run their cross-site pre-flight twice (14 calls for one create) |
| 10 | Efficiency | `ThreadPoolExecutor` built per site, per request, nested in the default executor |
| 11 | Efficiency | `get()` serializes spec/build/ConfigMap reads |
| 12 | Efficiency | `_apply_to_site` re-GETs the object it just applied |
| 13 | Cleanup | Dead `if obj else` guards in `get()` |
