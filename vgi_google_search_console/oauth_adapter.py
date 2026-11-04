"""Connector-specific bindings for the shared Google OAuth implementation."""

from . import gsc_api as api
from .auth import SearchConsoleAuthError as AuthError

__all__ = ["AuthError", "api"]
