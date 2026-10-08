"""stoatdb — a small relational database engine in pure Python 3."""

from .engine import Database, Result

__version__ = "0.1.0"

__all__ = ["Database", "Result", "__version__"]
