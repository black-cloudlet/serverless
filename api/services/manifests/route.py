"""Workload external exposure via a Knative DomainMapping.

The Serverless Operator creates one OpenShift Route per Knative ingress, so no
Route is written here. A ``DomainMapping`` - identical in both clusters - binds
the workload's custom, cluster-independent host to its KSVC, and the operator
provisions the Route from it. A wildcard DNS record forwards to the active
region (docs/ARCHITECTURE.md - Route host convention).
"""

from __future__ import annotations

from common.labels import workload_labels
from common.names import default_host_label

DOMAIN_MAPPING_API = "serving.knative.dev/v1beta1"

# The composition host_for uses, surfaced on GET /info so a UI can preview the
# default host as the user types. Composed from the same helper, so the two
# cannot drift.
HOST_TEMPLATE = default_host_label("{name}", "{group}") + ".{routeDomain}"


def host_for(name: str, group: str, route_domain: str) -> str:
    """The default external host for a workload: ``{name}-{group}.{route_domain}``.

    Composes the host without checking the name/group pair's length; that check
    runs on the create path, in ``preflight.resolve_host``. Read paths call this
    to recompute the default host of a workload that already exists.
    """
    return f"{default_host_label(name, group)}.{route_domain}"


def build_domain_mapping(
    *,
    name: str,
    group: str,
    owner: str,
    offering: str,
    host: str,
) -> dict:
    """Build a DomainMapping binding ``host`` to the workload's KSVC.

    The Serverless Operator creates the OpenShift Route for it.

    Args:
        name: The object name of the workload (KSVC) to bind to.
        group: Owning group (for labels).
        owner: Creating username (for labels).
        offering: The offering (for labels).
        host: The external host (and the DomainMapping's name).

    Returns:
        The DomainMapping manifest dict.
    """
    return {
        "apiVersion": DOMAIN_MAPPING_API,
        "kind": "DomainMapping",
        "metadata": {
            "name": host,
            "labels": workload_labels(group, owner, name, offering),
        },
        "spec": {
            "ref": {
                "name": name,
                "kind": "Service",
                "apiVersion": "serving.knative.dev/v1",
            }
        },
    }
