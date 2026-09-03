"""The tenant-namespace controller (docs/ARCHITECTURE.md - Tenant Namespaces).

A deployment beside the API, with its own cluster identity: it is the only
component that creates and deletes Namespaces and RoleBindings
(docs/DEPLOYING.md - RBAC). Level-triggered: the loop converges every managed
namespace to the Helm-rendered template set, keyed by the set's hash stamped on
each namespace, so a ``helm upgrade`` reaches namespaces that already exist.
"""
