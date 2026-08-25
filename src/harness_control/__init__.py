"""The LLM Harness control plane: auth, model supervision, and a streaming proxy."""

from importlib.metadata import version

__version__ = version("harness-control")
"""Reported by ``GET /healthz`` as the contract version (docs/API-CONTRACT.md §3)."""
