"""Code shared by every service that talks to the platform's clusters.

``common`` is not "utilities". A module earns a place here only if **more than
one service needs it** - the API and the build controller (see docs/BUILDING.md
- Ownership). Anything only the API needs belongs in ``api``.

The modules are layered by what they may depend on, heaviest last.
``tests/test_layering.py`` enforces it, so an import that breaks the split fails
a test rather than being discovered by whoever writes the second service:

    1. domain   names, labels, errors, config, build, kpack      (pydantic only)
    2. cluster  cluster                                          (+ kubernetes)
    3. web      web, requestid, logging                          (+ FastAPI)

A service takes only the layers it needs: one applying kpack manifests imports
``build`` and ``cluster`` and no web framework.

Three consequences, all bugs before the split: ``errors`` holds the exceptions
while ``web`` holds the FastAPI handlers rendering them, ``build`` type-hints
``Cluster`` under ``TYPE_CHECKING``, and ``requestid`` does the same with the
ASGI types - it holds the context var ``logging`` reads, so importing them for
real would put a web framework behind every log line. ``logging`` is in the web
layer only for that context var; it imports no framework.
"""
