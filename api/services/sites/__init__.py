"""Talking to the clusters: one site at a time, and all of them at once.

:mod:`deployer` fans out and rolls up; :mod:`site_apply` and :mod:`site_read`
are the write and read halves for a single site; :mod:`preflight` holds the
guards that run before any write.
"""
