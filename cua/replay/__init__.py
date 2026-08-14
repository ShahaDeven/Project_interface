"""Artifact interpreter — the production execution path.

Executes a saved artifact against a live surface with zero model calls: resolves
each target through its ordered strategy chain, verifies checkpoints, scans for
declared business outcomes and global runtime conditions after every step, and
applies the wait/retry policy. Waiting is executor policy and never model-decided.

Not implemented yet — Day 3.
"""
