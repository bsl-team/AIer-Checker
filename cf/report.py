"""Human-readable rendering of profiles, inconsistency reports and format runs."""
from __future__ import annotations

import difflib
import time

from .format import FORMAT_RULES, NOT_REWRITTEN
from .profile import FEATURE_SPECS, Profile

BAR_WIDTH = 10
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_TAG = {"high": "HIGH", "medium": "MED ", "low": "LOW "}


def bar(confidence: float) -> str:
    filled = int(round(max(0.0, min(1.0, confidence)) * BAR_WIDTH))
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + f"] {confidence:.2f}"


def when(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (ValueError, OSError):
        return "?"


def render_profile(profile: Profile, min_confidence: float = 0.0, show_rows: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"profile: {profile.user}    language: {profile.language} ({profile.family})")
    lines.append(
        f"samples: {len(profile.samples)}    updated: {when(profile.updated)}    "
        f"created: {when(profile.created)}"
    )
    stats = profile.stats
    if stats:
        lines.append(
            "measurements: "
            + "  ".join(
                f"{k}={stats[k]}"
                for k in ("rows", "code_rows", "max_line_width", "p95_line_width", "over_limit_rows", "tokens", "comments")
                if k in stats
            )
        )
    lines.append("")
    lines.append(f"{'rule':32} {'value':22} {'evidence':>10}  confidence")
    lines.append("-" * 78)
    observed = 0
    defaults: list[str] = []
    for row in profile.table():
        if not row["observed"]:
            defaults.append(f"{row['key']}={row['value']}")
            continue
        if row["confidence"] < min_confidence:
            continue
        observed += 1
        value = str(row["value"])
        evidence = f"{row['support']}/{row['total']}"
        lines.append(f"{row['key']:32} {value:22} {evidence:>10}  {bar(row['confidence'])}")
        if show_rows:
            feat = profile.feature(row["key"])
            for value_other, rows in (feat.outlier_rows(6) if feat else []):
                lines.append(f"{'':32} deviating: {value_other} at rows {', '.join(str(r) for r in rows)}")
    lines.append("-" * 78)
    lines.append(f"{observed} rules observed from the samples")
    if defaults:
        lines.append(f"unset (family default used when formatting): {', '.join(defaults[:12])}")
        if len(defaults) > 12:
            lines.append(f"{'':46}... and {len(defaults) - 12} more")
    return "\n".join(lines)


def render_store(summaries, store_path: str) -> str:
    if not summaries:
        return f"no profiles stored in {store_path}"
    lines = [f"profile store: {store_path}", ""]
    lines.append(f"{'user':20} {'language':12} {'samples':>8} {'rows':>7} {'confident':>10}  updated")
    lines.append("-" * 78)
    for s in summaries:
        lines.append(
            f"{s.user[:19]:20} {s.language:12} {s.samples:>8} {s.rows:>7} {s.confident_features:>10}  {when(s.updated)}"
        )
    return "\n".join(lines)


def render_consistency(report, max_findings: int = 40, show_all: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"consistency: {report.score * 100:.1f}% over {report.samples} sample(s), baseline {report.baseline.user}")
    lines.append("")
    lines.append(f"{'file':44} {'score':>7} {'rows':>6} {'findings':>9}")
    lines.append("-" * 70)
    for f in report.files:
        shown = f.file if len(f.file) <= 43 else "..." + f.file[-40:]
        lines.append(f"{shown:44} {f.score * 100:>6.1f}% {f.rows:>6} {f.findings:>9}")
    lines.append("")
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in report.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    lines.append(
        f"findings: {len(report.findings)} total  "
        + "  ".join(f"{counts.get(k, 0)} {k}" for k in ("high", "medium", "low"))
    )
    if not report.findings:
        lines.append("no style conflicts found")
        return "\n".join(lines)
    lines.append("")
    shown = 0
    by_file: dict[str, list] = {}
    for finding in sorted(report.findings, key=lambda f: (f.file, SEVERITY_ORDER.get(f.severity, 3), f.rule)):
        by_file.setdefault(finding.file, []).append(finding)
    for path, items in by_file.items():
        lines.append(path)
        for finding in items:
            if shown >= max_findings and not show_all:
                continue
            shown += 1
            rows = ", ".join(str(r) for r in finding.rows)
            more = f" (+{finding.extra_rows} rows)" if finding.extra_rows else ""
            tag = SEVERITY_TAG.get(finding.severity, "?    ")
            scope = "mixed in file" if finding.kind == "intra-file" else "differs from profile"
            lines.append(f"  [{tag}] {scope:20} {finding.rule:28} line {rows}{more}")
            lines.append(f"          {finding.message}")
        lines.append("")
    if shown < len(report.findings):
        lines.append(f"... {len(report.findings) - shown} more findings suppressed (use --all or --max-findings)")
    return "\n".join(lines)


def rule_summary(report) -> list[str]:
    """Per-rule agreement, for the --rules view of a check run."""
    out = []
    for row in report.rules:
        if row["findings"]:
            out.append(
                f"{row['rule']:32} value={str(row['value']):14} agreement={row['agreement']:.2f} findings={row['findings']}"
            )
    return out


def render_format(results, diffs: bool = False) -> str:
    """results: iterable of (path, FormatResult, before_text)."""
    lines: list[str] = []
    changed = 0
    for path, res, before in results:
        if res.error:
            lines.append(f"{path}: REFUSED - {res.error}")
            continue
        if res.changed:
            changed += 1
            applied = ", ".join(res.applied) or "-"
            lines.append(f"{path}: reformatted ({applied})")
        else:
            lines.append(f"{path}: already matches the profile")
        if res.skipped:
            lines.append(f"    not applied (low confidence): {', '.join(res.skipped)}")
        if diffs and res.changed:
            lines.append(indent_block(diff_for(path, before, res.text)))
    lines.append("")
    lines.append(f"{changed} file(s) would change")
    return "\n".join(lines)


def diff_for(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (current)",
            tofile=f"{path} (formatted)",
            n=2,
        )
    )


def indent_block(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.rstrip("\n").split("\n"))


def render_rules() -> str:
    lines = [f"{'rule':34} {'kind':6} {'formatter':11} description", "-" * 86]
    for key, spec in sorted(FEATURE_SPECS.items()):
        kind, label = spec[0], spec[1]
        role = "applies" if key in FORMAT_RULES else "reports only"
        lines.append(f"{key:34} {kind:6} {role:11} {label}")
    lines.append("")
    lines.append("why some rules are never rewritten:")
    for pattern, reason in NOT_REWRITTEN.items():
        lines.append(f"  {pattern:36} {reason}")
    return "\n".join(lines)


def render_languages(specs) -> str:
    lines = [f"{'language':14} {'family':8} {'file names':30} notes", "-" * 78]
    exts = {
        "c": ".c .h",
        "cpp": ".cc .cpp .hpp .hh",
        "go": ".go",
        "javascript": ".js .jsx .mjs",
        "typescript": ".ts .tsx",
        "java": ".java",
        "rust": ".rs",
        "csharp": ".cs",
        "shell": ".sh .bash",
        "python": ".py .pyi",
        "clike": "(fallback)",
    }
    notes = {"python": "tokenize + ast", "clike": "used when nothing else matches"}
    for name, spec in specs:
        lines.append(f"{name:14} {spec.family:8} {exts.get(name, ''):30} {notes.get(name, '')}")
    return "\n".join(lines)
