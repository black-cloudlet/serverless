"""Per-region cluster access: the client, the GVK registry, and the log stream.

One package, four concerns: :mod:`~common.cluster.client` talks to a cluster,
:mod:`~common.cluster.kinds` names what it talks about, :mod:`~common.cluster.pool`
is the urllib3 plumbing under it, and :mod:`~common.cluster.follow` is the one
response that outlives its request. Everything public is re-exported here, so
``from common.cluster import ...`` keeps working unchanged.
"""

from common.cluster.client import Cluster, NamespacedCluster, clusters_for, select_local
from common.cluster.follow import LogFollow
from common.cluster.kinds import ResourceKind

__all__ = [
    "Cluster",
    "LogFollow",
    "NamespacedCluster",
    "ResourceKind",
    "clusters_for",
    "select_local",
]
