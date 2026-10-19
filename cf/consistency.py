"""Style inconsistency analysis, inside one sample and across a sample set."""
from __future__ import annotations

from dataclasses import dataclass, field

from .document import Document
from .extract import extract, extract_all
from .profile import FEATURE_SPECS, Profile


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    if not total:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total

MIN_EVIDENCE = 3
MIN_CONFIDENCE = 0.2
INTRA_THRESHOLD = 0.9  # agreement at or above this counts as consistent

# Rules where a single deviation is already a defect: mixing indent characters, breaking the
# indent unit, or leaving trailing whitespace is never "mostly fine" in a style review.
STRICT_ANY_DEVIATION = {
    "indent.type",
    "indent.size",
    "blank.trailing_whitespace",
    "width.limit",
    "quote.style",
}

HIGH_SEVERITY_RULES = {
    "indent.type",
    "indent.size",
    "brace.style",
    "quote.style",
    "space.around_pointer",
    "naming.function",
    "naming.type",
    "naming.variable",
    "naming.parameter",
    "naming.constant",
    "naming.member",
    "naming.method",
    "space.after_keyword",
    "semicolon.required",
    "comment.style",
    "width.limit",
    "blank.between_functions",
}

SEVERITY_WEIGHT = {"high": 3.0, "medium": 1.5, "low": 0.6}

STRICT_SEVERITY = {
    "indent.type": "high",
    "indent.size": "high",
    "quote.style": "medium",
    "width.limit": "medium",
    "blank.trailing_whitespace": "low",
}


@dataclass
class Finding:
    file: str
    rule: str
    kind: str            # "intra-file" | "against-profile"
    expected: str
    actual: str
    rows: list[int]
    severity: str
    message: str
    support: int = 0
    total: int = 0
    extra_rows: int = 0

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "rule": self.rule,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "rows": self.rows,
            "extra_rows": self.extra_rows,
            "severity": self.severity,
            "message": self.message,
            "support": self.support,
            "total": self.total,
        }


@dataclass
class FileScore:
    file: str
    score: float
    rows: int
    findings: int
    language: str


@dataclass
class ConsistencyReport:
    baseline: Profile
    files: list[FileScore]
    findings: list[Finding]
    rules: list[dict]
    score: float
    samples: int

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "samples": self.samples,
            "language": self.baseline.language,
            "family": self.baseline.family,
            "files": [
                {"file": f.file, "score": round(f.score, 3), "rows": f.rows, "findings": f.findings, "language": f.language}
                for f in self.files
            ],
            "rules": self.rules,
            "findings": [f.to_dict() for f in self.findings],
            "baseline": self.baseline.summary(),
        }

    def findings_for(self, path: str) -> list[Finding]:
        return [f for f in self.findings if f.file == path]


def severity_for(rule: str, agreement: float, kind: str) -> str:
    if kind == "intra-file" and rule in STRICT_ANY_DEVIATION:
        return STRICT_SEVERITY.get(rule, "medium")
    if kind == "against-profile" and rule in HIGH_SEVERITY_RULES:
        return "high"
    if rule in HIGH_SEVERITY_RULES:
        return "medium"
    return "medium" if agreement < 0.5 else "low"


def describe(rule: str, expected: str, actual: str, count: int, kind: str) -> str:
    label = FEATURE_SPECS.get(rule, (None, rule))[1]
    if rule == "indent.size" and actual.startswith("irregular:"):
        return f"indent is not a multiple of the {expected}-character unit ({actual.split(':')[-1]} characters here)"
    if rule == "width.limit" and actual.startswith(">"):
        return f"row is longer than the {expected} column limit"
    if kind == "against-profile":
        return f"{label}: this file uses {actual}, the profile says {expected}"
    return f"{label} varies: mostly {expected}, {count} row(s) use {actual}"


def analyze(
    docs: list[Document],
    baseline: Profile | None = None,
    min_evidence: int = MIN_EVIDENCE,
    min_confidence: float = MIN_CONFIDENCE,
    intra_threshold: float = INTRA_THRESHOLD,
) -> ConsistencyReport:
    """Report mixed style inside each sample and how each sample relates to the shared profile."""
    per_file = {doc.path: extract(doc) for doc in docs}
    shared = baseline or extract_all(docs)
    findings: list[Finding] = []
    scores: list[FileScore] = []
    for doc in docs:
        prof = per_file[doc.path]
        findings.extend(_intra_file_findings(doc, prof, min_evidence, min_confidence, intra_threshold))
        findings.extend(_profile_findings(doc, prof, shared, min_evidence))
        scores.append(
            FileScore(
                file=doc.path,
                score=_file_score(prof, shared),
                rows=len(doc.lines),
                findings=sum(1 for f in findings if f.file == doc.path),
                language=doc.spec.name,
            )
        )
    overall = _weighted_mean([f.score for f in scores], [max(1, f.rows) for f in scores]) if scores else 1.0
    return ConsistencyReport(
        baseline=shared,
        files=scores,
        findings=findings,
        rules=_rule_table(shared, per_file, findings),
        score=overall,
        samples=len(docs),
    )


def _row_sample(doc: Document, rows: list[int]) -> list[int]:
    return rows[:6]


def _intra_file_findings(doc, prof: Profile, min_evidence, min_confidence, threshold) -> list[Finding]:
    out: list[Finding] = []
    for key, feat in sorted(prof.features.items()):
        if feat.total < 2 or feat.value is None:
            continue
        strict = key in STRICT_ANY_DEVIATION
        if not strict and (feat.total < min_evidence or feat.confidence < min_confidence * 0.5):
            continue
        if not strict and feat.agreement >= threshold:
            continue
        for value, rows in feat.outlier_rows(limit=max(1, min_evidence)):
            if feat.counts.get(value, 0) < 1:
                continue
            if not strict and feat.counts.get(value, 0) < 2 and feat.total >= 12:
                continue  # a lone mild deviation in a large, otherwise uniform file
            sev = severity_for(key, feat.agreement, "intra-file")
            out.append(
                Finding(
                    file=doc.path,
                    rule=key,
                    kind="intra-file",
                    expected=feat.value,
                    actual=value,
                    rows=_row_sample(doc, sorted(rows)),
                    extra_rows=max(0, len(rows) - 6),
                    severity=sev,
                    support=feat.support,
                    total=feat.total,
                    message=describe(key, feat.value, value, feat.counts.get(value, 0), "intra-file"),
                )
            )
    return out


def _profile_findings(doc, prof: Profile, shared: Profile, min_evidence) -> list[Finding]:
    out: list[Finding] = []
    for key, base in sorted(shared.features.items()):
        if base.value is None or base.confidence < MIN_CONFIDENCE:
            continue
        feat = prof.features.get(key)
        if feat is None or feat.total == 0 or feat.value is None:
            continue
        if feat.value == base.value:
            continue
        if feat.agreement < 0.6:
            continue  # the file itself is mixed; the intra-file pass already reported it
        sev = severity_for(key, feat.agreement, "against-profile")
        rows = sorted(feat.rows.get(feat.value, []))
        out.append(
            Finding(
                file=doc.path,
                rule=key,
                kind="against-profile",
                expected=base.value,
                actual=feat.value,
                rows=_row_sample(doc, rows),
                extra_rows=max(0, len(rows) - 6),
                severity=sev,
                support=feat.total,
                total=base.total,
                message=describe(key, base.value, feat.value, feat.total, "against-profile"),
            )
        )
    return out


def _file_score(prof: Profile, shared: Profile) -> float:
    """Weighted agreement with the shared profile: confident, important rules count most."""
    weights: list[float] = []
    values: list[float] = []
    for key, base in shared.features.items():
        if base.value is None or base.confidence < MIN_CONFIDENCE:
            continue
        weight = base.confidence * SEVERITY_WEIGHT[severity_for(key, base.agreement, "against-profile")]
        feat = prof.features.get(key)
        agree = 1.0 if feat is None or feat.total == 0 else feat.counts.get(base.value, 0) / feat.total
        weights.append(weight)
        values.append(agree)
    if not weights:
        return 1.0
    return _weighted_mean(values, weights)


def _rule_table(shared: Profile, per_file: dict[str, Profile], findings: list[Finding]) -> list[dict]:
    rows = []
    for key, base in sorted(shared.features.items()):
        if base.value is None:
            continue
        observed_in = sum(1 for p in per_file.values() if key in p.features and p.features[key].total)
        agreeing = 0
        total = 0
        for p in per_file.values():
            feat = p.features.get(key)
            if feat is None or not feat.total:
                continue
            total += feat.total
            agreeing += feat.counts.get(base.value, 0)
        rows.append(
            {
                "rule": key,
                "label": FEATURE_SPECS.get(key, (None, key))[1],
                "value": base.value,
                "confidence": base.confidence,
                "agreement": round(agreeing / total, 3) if total else 0.0,
                "samples_observing": observed_in,
                "findings": sum(1 for f in findings if f.rule == key),
            }
        )
    return rows
