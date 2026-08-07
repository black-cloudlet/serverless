"""Code shared by the services in *this* repository.

``common`` is not "utilities". A module earns a place here only if **more than
one service in this repository needs it** - the API and the build controller
(see docs/BUILDING.md - Ownership). Anything only the API needs belongs in
``api``.

What is shared with the platform's *other* APIs is a different question, and
lives in the ``cloudlet-apis`` package rather than here: the error envelope, the
log format, request-id correlation, the ``/healthz`` + offline-docs wiring, the
name/group rules and SSO auth. Two modules here are the seam - :mod:`common.errors`
re-exports the shared catalog and adds ``SiteTotalFailure``, and
:mod:`common.names` re-exports the group rules and keeps what only this platform
derives (object names, image and cache repositories, OCI tags).

The modules are layered by what they may depend on, heaviest last.
``tests/test_layering.py`` enforces it, so an import that breaks the split fails
a test rather than being discovered by whoever deploys the controller:

    1. domain   names, labels, errors, config, build, kpack      (pydantic only)
    2. cluster  cluster                                          (+ kubernetes)

There is no web layer here any more - it is ``cloudlet_apis.web``, behind that
package's ``[web]`` extra, which the controller does not install. That is what
now keeps a web framework out of an image that serves no HTTP, and the layering
test checks both halves: that nothing in the domain layer reaches FastAPI, and
that nothing reaches the ``[auth]`` dependencies either.

Two consequences of the layering, both bugs before the original split: ``errors``
holds the exceptions while ``cloudlet_apis.web`` holds the FastAPI handlers that
render them, and ``build`` type-hints ``Cluster`` under ``TYPE_CHECKING``.
"""
