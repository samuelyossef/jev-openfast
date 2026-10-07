"""Jev chooses an observed action. Code owns execution."""

from importlib import metadata

from .agent import Agent
from .browser import Browser

try:
    __version__ = metadata.version("jev-ultrafast")
except metadata.PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["Agent", "Browser", "__version__"]
