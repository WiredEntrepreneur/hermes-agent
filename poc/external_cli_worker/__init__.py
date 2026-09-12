"""Isolated external-CLI Kanban worker proof of concept."""

from .antigravity import AntigravityAdapter
from .lane import ExternalCliWorkerLane

__all__ = ["AntigravityAdapter", "ExternalCliWorkerLane"]
