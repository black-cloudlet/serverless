"""Building what gets applied: the KSVC and every object it references.

Pure builders - each turns validated request input into manifests and never
reaches a cluster. :mod:`api.services.sites` is what writes them.
"""
