"""Code shared by every service that talks to the platform's clusters.

``common`` is not "utilities". A module earns a place here only if **more than
one service needs it** - today the API, tomorrow the build service (see
docs/BUILDING.md - Ownership). Anything only the API needs belongs in ``api``.

The modules are layered by what they may depend on, heaviest last.
``tests/test_layering.py`` enforces it, so an import that breaks the split fails
a test rather than being discovered by whoever writes the second service:

    1. domain   names, labels, errors, config, contract, kpack   (pydantic only)
    2. cluster  cluster                                          (+ kubernetes)
    3. web      web, requestid, logging                          (+ FastAPI)

A service takes only the layers it needs: one applying kpack manifests imports
``contract`` and ``cluster`` and no web framework.

Two consequences, both bugs before the split: ``errors`` holds the exceptions
while ``web`` holds the FastAPI handlers rendering them, and ``contract``
type-hints ``Cluster`` under ``TYPE_CHECKING``. ``logging`` is in the web layer
only because it reads the request-id context var; it imports no framework.
"""
