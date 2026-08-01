"""Self-contained auth component: all OIDC interaction with SSO lives here.

This is an internal module (not a separate microservice) - token validation is
stateless, so there is nothing to centralize. See docs/ARCHITECTURE.md - Authentication.
"""
