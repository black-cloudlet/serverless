"""Code shared by every service that talks to the platform's clusters.

A service takes only the layers it needs: one applying kpack manifests imports
``build`` and ``cluster`` and no web framework.
"""
