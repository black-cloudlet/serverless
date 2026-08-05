"""Reading a workload's stored and live state back out of a site.

The kept-values reads fail loud - a ``{}`` for a Secret that exists would make a
valid "keep" look unset and lose it. The decoration reads are best-effort.

Every function here blocks, and is called through ``asyncio.to_thread`` or the
deployer's fan-out.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

from api.models.common import (
    ANNOTATION_GIT_BRANCH,
    ANNOTATION_GIT_PATH,
    ANNOTATION_GIT_URL,
    ANNOTATION_HOST,
    ANNOTATION_RUNTIME,
    ANNOTATION_RUNTIME_VERSION,
)
from api.services.manifests import secrets as secret_svc
from api.services.manifests.env import env_secret_name
from api.services.manifests.files import files_name
from api.services.state import describe as describe_svc
from api.services.state import metrics as metrics_svc
from api.services.state.ksvc_state import extract_image
from common.cluster import Cluster, ResourceKind
from common.errors import NotFoundError, ServiceUnavailableError

if TYPE_CHECKING:  # a type hint only - offering imports this module at runtime
    from api.services.offering import Offering


def existing_state(obj: dict, cluster: Cluster, offering: Offering, oname: str) -> dict:
    """Read an existing workload's carried-forward state + backing secret values."""
    ann = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
    ps_name = describe_svc.pull_secret_name(obj)
    state = {
        "image": extract_image(obj),
        "runtime": ann.get(ANNOTATION_RUNTIME),
        "version": ann.get(ANNOTATION_RUNTIME_VERSION),
        "gitUrl": ann.get(ANNOTATION_GIT_URL),
        "branch": ann.get(ANNOTATION_GIT_BRANCH),
        "path": ann.get(ANNOTATION_GIT_PATH),
        "host": ann.get(ANNOTATION_HOST),
        "pull_secret": ps_name,
        # Existing secret values, so an update can keep a redacted secret the
        # client sent back without a value (see resolve_env/_files). Env values
        # are text; file content is bytes, which is why they read differently.
        "env_values": secret_text(cluster, env_secret_name(oname)),
        "files_values": secret_data(cluster, files_name(oname)),
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
    state.update(offering.read_extra_state(cluster, oname))
    return state


def secret_data(cluster: Cluster, name: str) -> dict[str, bytes]:
    """Raw ``data`` of a Secret (base64 -> bytes); ``{}`` if it doesn't exist.

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


def secret_text(cluster: Cluster, name: str) -> dict[str, str]:
    """:func:`secret_data` as text, for the values that genuinely are text."""
    out: dict[str, str] = {}
    for key, raw in secret_data(cluster, name).items():
        try:
            out[key] = raw.decode("utf-8")
        except UnicodeDecodeError:  # noqa: S112 - not text, so not an env value
            continue
    return out


def describe_spec(cluster: Cluster, obj: dict):
    """Read the desired-state spec (secrets redacted) from a KSVC."""
    configmaps: dict[str, dict] = {}
    for cm_name in describe_svc.configmap_refs(obj):
        try:
            cm = cluster.get(ResourceKind.CONFIG_MAP, cm_name)
            configmaps[cm_name] = cm.get("data") or {}
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


def revision(cluster: Cluster, name: str | None) -> dict | None:
    """Best-effort fetch of the Knative Revision the KSVC points at."""
    if not name:
        return None
    try:
        return cluster.get(ResourceKind.KNATIVE_REVISION, name)
    except Exception:  # noqa: BLE001 - best-effort, never fatal
        return None


@dataclass(frozen=True)
class SiteUsage:
    """One site's usage read: whether it could be taken, and what it showed."""

    measured: bool
    total: metrics_svc.Usage | None


def site_usage(cluster: Cluster, oname: str) -> SiteUsage:
    """Best-effort live cpu/memory summed over one site's running pods.

    Never raises: an unreadable metrics API must not fail a status that is
    otherwise worth returning.
    """
    try:
        items = cluster.get(
            ResourceKind.POD_METRICS,
            label_selector=f"serving.knative.dev/service={oname}",
        )
    except Exception:  # noqa: BLE001 - usage is best-effort, never fatal
        return SiteUsage(measured=False, total=None)
    return SiteUsage(measured=True, total=metrics_svc.total_usage(items))
