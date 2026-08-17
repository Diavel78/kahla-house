"""THE CELLAR — the machine, running at the house.

Spec: docs/cellar-migration-spec.md

Nothing here bets until CELLAR_DRY_RUN=0 and CELLAR_LANES names a lane.
A fresh clone is inert by construction.
"""
__all__ = ["config", "lease", "journal", "lanes", "runner"]
