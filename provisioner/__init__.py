"""The tenant-namespace provisioner (docs/proposals/namespace-per-group.md).

A second deployment beside the API, like the build controller, existing for
privilege separation: creating and deleting Namespaces and RoleBindings is
cluster-scoped power the internet-facing API must not hold. Level-triggered:
the loop converges every managed namespace to the Helm-rendered template set,
keyed by the set's hash stamped on each namespace - which is what carries a
``helm upgrade`` to namespaces that already exist.
"""
