"""cf - a code style kernel.

Four capabilities, no UI:

1. :func:`extract` / :func:`extract_all` - read a developer's style out of a batch of code.
2. :func:`analyze_consistency` - report style that conflicts inside a sample or against a profile.
3. :class:`ProfileStore` - keep one style profile per user.
4. :func:`format_code` - reformat source to match a profile without changing the token stream.

Everything is also reachable from ``python -m cf`` (``cf`` once installed).
"""
from __future__ import annotations

from .consistency import ConsistencyReport, Finding, analyze as analyze_consistency
from .document import Document, build_document
from .extract import extract, extract_all
from .format import FormatOptions, FormatResult, format_source, token_identity
from .profile import Feature, Profile
from .store import ProfileStore, StoreError

__all__ = [
    "__version__",
    "analyze_consistency",
    "build_document",
    "ConsistencyReport",
    "Document",
    "Feature",
    "Finding",
    "FormatOptions",
    "FormatResult",
    "Profile",
    "ProfileStore",
    "StoreError",
    "extract",
    "extract_all",
    "format_code",
    "token_identity",
]

__version__ = "0.1.0"


def format_code(src: str, profile: Profile, path: str | None = None, options: FormatOptions | None = None) -> FormatResult:
    """Format ``src`` to match ``profile``; the result carries the text, applied rules, or a refusal."""
    return format_source(src, path, profile, options)
