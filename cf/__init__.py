"""cf - a code style kernel.

Five capabilities, no UI:

1. :func:`extract` / :func:`extract_all` - read a developer's style out of a batch of code.
2. :func:`analyze_consistency` - report mixed style inside a sample or against a profile.
3. :class:`ProfileStore` - keep one style profile per user.
4. :func:`format_code` - reformat source to match a profile without changing the token stream.
5. :func:`find_duplicates` - compare submissions structurally and report how much they share.

Report and error text is selectable: :func:`set_locale` (or ``CF_LANG`` / ``cf -L zh``)
switches between English and Chinese.  Everything is also reachable from ``python -m cf``.
"""
from __future__ import annotations

from .consistency import ConsistencyReport, Finding, analyze as analyze_consistency
from .document import Document, build_document
from .extract import extract, extract_all
from .format import FormatOptions, FormatResult, format_source, token_identity
from .i18n import get_locale, set_locale, t
from .profile import Feature, Profile
from .similarity import (
    Cluster,
    Fingerprint,
    MatchSpan,
    Similarity,
    SimilarityReport,
    Submission,
    compare_documents,
    find_duplicates,
    fingerprint,
    normalize,
)
from .store import ProfileStore, StoreError

__all__ = [
    "__version__",
    "analyze_consistency",
    "build_document",
    "Cluster",
    "ConsistencyReport",
    "Document",
    "Feature",
    "Finding",
    "Fingerprint",
    "FormatOptions",
    "FormatResult",
    "MatchSpan",
    "Profile",
    "ProfileStore",
    "Similarity",
    "SimilarityReport",
    "StoreError",
    "Submission",
    "compare_documents",
    "extract",
    "extract_all",
    "find_duplicates",
    "fingerprint",
    "format_code",
    "get_locale",
    "normalize",
    "set_locale",
    "t",
    "token_identity",
]

__version__ = "0.1.0"


def format_code(src: str, profile: Profile, path: str | None = None, options: FormatOptions | None = None) -> FormatResult:
    """Format ``src`` to match ``profile``; the result carries the text, applied rules, or a refusal."""
    return format_source(src, path, profile, options)
