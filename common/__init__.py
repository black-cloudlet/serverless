"""Code shared by the services in *this* repository.

``common`` is not "utilities". A module earns a place here only if both the API
and the build controller need it (docs/BUILDING.md - Ownership). What is shared
with the platform's *other* APIs is in the ``cloudlet-apis`` package instead;
:mod:`common.errors` and :mod:`common.names` are the seam, each re-exporting the
shared half and keeping what is only ours.

Layered by what each module may import: domain (pydantic, and httpx for the
registry's management API - no kubernetes), then cluster (+ kubernetes). There
is no web layer - that is ``cloudlet_apis.web``, behind an extra the controller
does not install. ``tests/test_layering.py`` enforces it.
"""
