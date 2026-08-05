"""Interpreting what came back, without reaching a cluster again.

Every module here takes objects the caller already fetched and returns a value -
which is what makes these the cheapest rules in the service to test: hand them a
dict, assert on the answer.
"""
