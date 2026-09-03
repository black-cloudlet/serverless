"""Interpreting objects the caller already fetched, without reaching a cluster.

Every module here takes Kubernetes objects as plain dicts and returns a value:
the status rollups, the desired-state read-back, resource-usage totals, the
listing merge, and the ownership predicate. Nothing here performs I/O.
"""
