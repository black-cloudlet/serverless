"""Pure builder for the OpenShift Route exposing a workload.

The host is identical in both clusters; a *.serverless.{base_domain} DNS record
forwards to the active site (docs §5).
"""

from __future__ import annotations

from app.services.labels import ownership_labels

ROUTE_API = "route.openshift.io/v1"
# Knative ingress service (Kourier) in OpenShift Serverless.
KOURIER_SERVICE = "kourier"
KOURIER_NAMESPACE = "knative-serving-ingress"


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
    """Bind a custom host to the KSVC so kourier routes it (OpenShift Serverless)."""
    return {
        "apiVersion": DOMAIN_MAPPING_API,
        "kind": "DomainMapping",
        "metadata": {
            "name": host,
            "labels": ownership_labels(group, owner, offering),
        },
        "spec": {
            "ref": {
                "name": name,
                "kind": "Service",
                "apiVersion": "serving.knative.dev/v1",
            }
        },
    }


def build_route(
    *,
    name: str,
    group: str,
    owner: str,
    offering: str,
    host: str,
    target_namespace: str,
) -> dict:
    return {
        "apiVersion": ROUTE_API,
        "kind": "Route",
        "metadata": {
            "name": f"{name}-{group}",
            "labels": ownership_labels(group, owner, offering),
        },
        "spec": {
            "host": host,
            "to": {"kind": "Service", "name": KOURIER_SERVICE},
            "port": {"targetPort": "http2"},
            "tls": {
                "termination": "edge",
                "insecureEdgeTerminationPolicy": "Redirect",
            },
        },
    }
