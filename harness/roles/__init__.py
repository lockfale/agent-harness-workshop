"""The six roles, and the loop they share.

`base.py` is plumbing: the turn loop, tool-call parsing, budget enforcement and
status reporting. The role modules are prompts plus tool bodies, which is what
the exercises are about.
"""

from .base import Pipeline, RoleOutcome, run_role

__all__ = ["Pipeline", "RoleOutcome", "run_role"]
