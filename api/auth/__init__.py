"""This API's auth wiring.

The component is :mod:`cloudlet_apis.auth`, shared with every API on the platform
- token validation is stateless, so there is nothing to centralize in a service
of its own, and nothing here that another API would not repeat verbatim. What is
left is :mod:`api.auth.deps`: which of *this* service's settings it is built from.

See docs/ARCHITECTURE.md - Authentication.
"""
