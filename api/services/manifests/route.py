"""Workload external exposure via a Knative DomainMapping.

We never create OpenShift Routes by hand - the Serverless Operator creates one
per Knative ingress. To expose a custom, cluster-independent host we create a
``DomainMapping`` (identical in both clusters) and the operator provisions the
Route. A wildcard DNS record forwards to the active region.
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

    Deliberately does not check the pair's length: read paths recompute a
    default host for workloads that already exist, and a read is no place to
    discover a create-time rule. The check lives on the create path, in
    ``preflight.resolve_host``.
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
