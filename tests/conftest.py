"""Most tests check what the agent decides and does on its own, so they run with APPROVAL_MODE=auto.
tests/test_human_approval.py switches the default (a person gives the final OK) back on."""
import os

os.environ.setdefault("APPROVAL_MODE", "auto")
