"""Talking to the clusters: one region at a time, and all of them at once.

:mod:`deployer` fans out and rolls up; :mod:`region_apply` and :mod:`region_read`
are the write and read halves for a single region; :mod:`preflight` holds the
guards that run before any write.
"""
