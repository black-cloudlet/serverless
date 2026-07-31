"""Code shared by every service that talks to the platform's clusters.

``common`` is not "utilities". A module earns a place here only if **more than
one service needs it** - today the API, tomorrow the build service (see
docs/BUILD-PIPELINE.md §8). Anything only the API needs belongs in ``api``.

The modules are layered by what they may depend on, heaviest last.
``tests/test_layering.py`` enforces it, so an import that breaks the split fails
a test rather than being discovered by whoever writes the second service:

    1. domain      names, labels, errors, config, contract
                   plain Python plus pydantic. No FastAPI, no kubernetes.
    2. cluster     cluster
                   adds the kubernetes client. A service that reaches a cluster
                   needs it; one that only builds manifests does not.
    3. web         web, requestid, logging
                   adds FastAPI/Starlette, for services that serve HTTP.

The point is that a service takes only the layers it needs. A build service
applying kpack manifests imports ``contract`` and ``cluster`` without pulling in
a web framework; one that merely renders manifests needs neither.

Two consequences worth knowing, because both were bugs before the split:
``errors`` holds the exceptions while ``web`` holds the FastAPI handlers that
render them, and ``contract`` type-hints ``Cluster`` under ``TYPE_CHECKING``.
``logging`` sits in the web layer only because it reads the request-id context
var - it imports no framework itself.
"""
