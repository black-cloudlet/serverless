"""Workload external exposure via a Knative DomainMapping.

On OpenShift Serverless we do NOT create OpenShift Routes by hand: the Serverless
Operator's ingress controller automatically creates the OpenShift Route for each
Knative ingress. To expose a workload at a custom, cluster-independent host we
create a ``DomainMapping`` for that host (identical in both clusters); the
operator then provisions the corresponding Route. A ``*.serverless.{base_domain}``
DNS record forwards to the active site (docs §5).
"""

from __future__ import annotations

from app.services.labels import workload_labels

DOMAIN_MAPPING_API = "serving.knative.dev/v1beta1"


def host_for(name: str, group: str, route_domain: str) -> str:
    return f"{name}-{group}.{route_domain}"


def build_domain_mapping(
    *,
    name: str,
    group: str,
    owner: str,
    offering: str,
    host: str,
) -> dict:
    """Bind a custom host to the KSVC; the operator creates the Route for it."""
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
