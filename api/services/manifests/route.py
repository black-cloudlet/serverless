"""Workload external exposure via a Knative DomainMapping.

We never create OpenShift Routes by hand - the Serverless Operator creates one
per Knative ingress. To expose a custom, cluster-independent host we create a
``DomainMapping`` (identical in both clusters) and the operator provisions the
Route. A wildcard DNS record forwards to the active site.
"""

from __future__ import annotations

from common.labels import workload_labels

DOMAIN_MAPPING_API = "serving.knative.dev/v1beta1"

# The composition host_for uses, surfaced on GET /info so a UI can preview the
# default host as the user types. Keep in sync with host_for.
HOST_TEMPLATE = "{name}-{group}.{routeDomain}"


def host_for(name: str, group: str, route_domain: str) -> str:
    """The default external host for a workload: ``{name}-{group}.{route_domain}``."""
    return f"{name}-{group}.{route_domain}"


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
