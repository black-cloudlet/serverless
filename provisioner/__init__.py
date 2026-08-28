"""The tenant-namespace provisioner (docs/proposals/namespace-per-group.md).

A second deployment beside the API, like the build controller, existing for one
reason: privilege separation. Creating and deleting Namespaces, NetworkPolicies
and RoleBindings is cluster-scoped power the internet-facing API must not hold,
so it lives here, behind the provisioner's own client-cert identity.

Level-triggered by design: the reconcile loop converges every managed namespace
in the local cluster to the Helm-rendered template set, keyed by the set's hash
stamped on each namespace - which is what carries a ``helm upgrade`` to
namespaces that already exist, not only to new groups.
"""
