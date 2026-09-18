"""hotline-registry: contact the people who consented to be contacted."""

from __future__ import annotations

__version__ = "0.1.0"

from .cli import main
from .registry import Person, Registry, RegistryError

__all__ = [
    "Person",
    "Registry",
    "RegistryError",
    "__version__",
    "main",
]
