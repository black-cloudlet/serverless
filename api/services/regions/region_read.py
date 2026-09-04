"""Reading a workload's stored and live state back out of a region.

The counterpart to :mod:`api.services.regions.region_apply`: everything here fetches from
one cluster and nothing writes. Unlike :mod:`api.services.state.ksvc_state` these calls
do I/O, and their error handling differs per function:

* the **kept-values** reads (:func:`secret_data`, :func:`secret_text`) fail loud.
  Returning ``{}`` for a Secret that exists but could not be read would make a
  valid "keep" look unset and fail the update as a 400, losing a stored secret.
* the **decoration** reads (:func:`revision`, :func:`region_usage`,
  :func:`describe_spec`) are best-effort. A workload whose replica count or live
  usage could not be fetched still returns a status. ``region_usage`` also
  reports *that* it failed, because its caller sums across regions.

Every function here blocks, and is called through ``asyncio.to_thread`` or the
deployer's fan-out.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

from api.models.common import (
    ANNOTATION_GIT_PATH,
    ANNOTATION_GIT_REVISION,
    ANNOTATION_GIT_URL,
    ANNOTATION_HOST,
    ANNOTATION_PULL_STAMP,
    ANNOTATION_RUNTIME,
    ANNOTATION_RUNTIME_VERSION,
)
from api.services.manifests import secrets as secret_svc
from api.services.manifests.env import env_secret_name
from api.services.manifests.files import files_name
from api.services.state import describe as describe_svc
from api.services.state import metrics as metrics_svc
from api.services.state.ksvc_state import extract_image
from common.cluster import NamespacedCluster, ResourceKind
from common.errors import NotFoundError, ServiceUnavailableError

if TYPE_CHECKING:  # a type hint only - offering imports this module at runtime
    from api.services.offering import Offering


def existing_state(obj: dict, cluster: NamespacedCluster, offering: Offering, name: str) -> dict:
    """Read an existing workload's carried-forward state + backing secret values.

    Runs off the event loop (blocking cluster reads). ``env_values``/
    ``files_values`` back the keep-on-write path (fail loud on a transient read;
    see :func:`secret_data`). The pull-secret read is best-effort: a failure just
    degrades a registry keep to carrying the existing secret forward.

    Args:
        obj: The workload's KSVC, already fetched.
        cluster: The region to read the backing Secrets from.
        offering: The offering, for whatever it carries that this doesn't
            (:meth:`~api.services.offering.Offering.read_extra_state`).
        name: The workload's name (and its KSVC's).

    Returns:
        The carried-forward state.
    """
    ann = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
    ps_name = describe_svc.pull_secret_name(obj)
    state = {
        "image": extract_image(obj),
        "runtime": ann.get(ANNOTATION_RUNTIME),
        "version": ann.get(ANNOTATION_RUNTIME_VERSION),
        "gitUrl": ann.get(ANNOTATION_GIT_URL),
        "revision": ann.get(ANNOTATION_GIT_REVISION),
        "path": ann.get(ANNOTATION_GIT_PATH),
        "host": ann.get(ANNOTATION_HOST),
        "pull_stamp": ann.get(ANNOTATION_PULL_STAMP),
        "pull_secret": ps_name,
        # Existing secret values, so an update can keep a redacted secret the
        # client sent back without a value (see resolve_env/_files). Env values
        # are text; file content is bytes, which is why they read differently.
        "env_values": secret_text(cluster, env_secret_name(name)),
        "files_values": secret_data(cluster, files_name(name)),
    }
    # Existing registry creds (decoded from the pull secret), so a keep (token
    # omitted) can re-key them to the current image's registry.
    if ps_name:
        try:
            ps = cluster.get(ResourceKind.SECRET, ps_name)
            state["registry_username"] = secret_svc.registry_username(ps)
            state["registry_token"] = secret_svc.registry_token(ps)
        except Exception:  # noqa: BLE001, S110 - best-effort; keep degrades to carry-forward
            pass
    # Whatever else this offering carries forward - a function's stored git token,
    # so a build-input change can rebuild without the client re-supplying it.
    state.update(offering.read_extra_state(cluster, name))
    return state


def secret_data(cluster: NamespacedCluster, name: str) -> dict[str, bytes]:
    """Raw ``data`` of a Secret (base64 -> bytes); ``{}`` if it doesn't exist.

    Bytes, not text: a secret file may hold binary content (a keystore, a DER
    certificate) that does not survive a round trip through ``str``.

    Used by the update path so a redacted "keep" field echoed back is preserved. A
    genuine 404 means no stored values (``{}``). Any other error surfaces as a 503
    instead: ``{}`` would make a valid keep look unset and fail it as a 400.

    Raises:
        ServiceUnavailableError: If the Secret exists but couldn't be read.
    """
    try:
        secret = cluster.get(ResourceKind.SECRET, name)
    except NotFoundError:
        return {}  # no such Secret -> nothing stored
    except Exception as exc:  # noqa: BLE001 - transient/unknown read failure
        raise ServiceUnavailableError(
            f"could not read secret '{name}' to preserve kept values; retry"
        ) from exc
    out: dict[str, bytes] = {}
    for key, val in (secret.get("data") or {}).items():
        try:
            out[key] = base64.b64decode(val)
        except Exception:  # noqa: BLE001, S112 - skip an undecodable key
            continue
    return out


def secret_text(cluster: NamespacedCluster, name: str) -> dict[str, str]:
    """:func:`secret_data` as text, for the values that genuinely are text.

    Env values and the git token are strings by definition: a container env var and
    an HTTP basic-auth password. A stored value that is not valid UTF-8 is skipped.
    """
    out: dict[str, str] = {}
    for key, raw in secret_data(cluster, name).items():
        try:
            out[key] = raw.decode("utf-8")
        except UnicodeDecodeError:  # noqa: S112 - not text, so not an env value
            continue
    return out


def describe_spec(cluster: NamespacedCluster, obj: dict):
    """Read the desired-state spec (secrets redacted) from a KSVC.

    Fetches the file ConfigMap(s) for non-secret file contents and the pull
    secret for the registry username (never the token). Best-effort: a failed
    read just leaves the corresponding field null.
    """
    configmaps: dict[str, dict] = {}
    for cm_name in describe_svc.configmap_refs(obj):
        try:
            cm = cluster.get(ResourceKind.CONFIG_MAP, cm_name)
            # binaryData decodes to bytes; the str-vs-bytes split tells
            # describe._files which encoding to report the content under.
            entries: dict[str, str | bytes] = dict(cm.get("data") or {})
            for key, val in (cm.get("binaryData") or {}).items():
                try:
                    entries[key] = base64.b64decode(val)
                except Exception:  # noqa: BLE001, S112 - skip an undecodable key
                    continue
            configmaps[cm_name] = entries
        except Exception:  # noqa: BLE001, S110 - content is best-effort, skip silently
            pass
    registry_username = None
    ps_name = describe_svc.pull_secret_name(obj)
    if ps_name:
        try:
            secret = cluster.get(ResourceKind.SECRET, ps_name)
            registry_username = secret_svc.registry_username(secret)
        except Exception:  # noqa: BLE001, S110 - username is best-effort, skip silently
            pass
    return describe_svc.parse_spec(obj, configmaps, registry_username=registry_username)


def revision(cluster: NamespacedCluster, name: str | None) -> dict | None:
    """Best-effort fetch of the Knative Revision the KSVC points at.

    The Revision carries both the autoscaler's live scale and the specific
    rollout-failure conditions, so a single read feeds both the replica count
    and the per-region error detail.

    Returns:
        The Revision object, or None if it has no revision yet or can't be read.
    """
    if not name:
        return None
    try:
        return cluster.get(ResourceKind.KNATIVE_REVISION, name)
    except Exception:  # noqa: BLE001 - best-effort, never fatal
        return None


@dataclass(frozen=True)
class RegionUsage:
    """One region's usage read: whether it could be taken, and what it showed.

    Attributes:
        measured: Whether the read and the parse both succeeded. A caller summing
            across regions uses it to tell a real zero from a missing answer.
        total: The region's summed usage, or None when ``measured`` is False.
    """

    measured: bool
    total: metrics_svc.Usage | None


def region_usage(cluster: NamespacedCluster, name: str) -> RegionUsage:
    """Best-effort live cpu/memory summed over one region's running pods.

    Never raises. Both the read and the parse run inside the guard, so a quantity
    in a form :mod:`api.services.state.metrics` does not recognise (Kubernetes may
    render one in decimal-exponent notation) yields ``measured=False`` instead of
    escaping into the fan-out as a ``Failed`` region.

    Returns:
        The region's usage, with ``measured=False`` if the read or the parse failed.
    """
    try:
        items = cluster.get(
            ResourceKind.POD_METRICS,
            label_selector=f"serving.knative.dev/service={name}",
        )
        return RegionUsage(measured=True, total=metrics_svc.total_usage(items))
    except Exception:  # noqa: BLE001 - usage is best-effort, never fatal
        return RegionUsage(measured=False, total=None)
