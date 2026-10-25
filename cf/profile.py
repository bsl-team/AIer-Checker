"""Style profile data model: observed features, confidence, defaults and JSON round-trip."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

PROFILE_VERSION = 1

# kind, label, default for clike, default for python
FEATURE_SPECS: dict[str, tuple[str, str, Any, Any]] = {
    "indent.type": ("str", "indent characters", "space", "space"),
    "indent.size": ("int", "indent size", 4, 4),
    "indent.continuation": ("str", "continuation indent", 4, 4),
    "indent.namespace_body": ("str", "indent inside namespace/module bodies", "indent", "n/a"),
    "width.limit": ("int", "line length limit", 100, 88),
    "brace.style": ("str", "opening brace placement", "attach", "n/a"),
    "brace.close_same_row_branch": ("bool", "} else { on one row", True, False),
    "brace.empty_block": ("str", "empty block spelling", "{}", "n/a"),
    "space.after_keyword": ("bool", "space between control keyword and (", True, False),
    "space.around_operators": ("bool", "spaces around binary operators", True, True),
    "space.around_assignment": ("bool", "spaces around = in assignments", True, True),
    "space.after_comma": ("bool", "space after comma", True, True),
    "space.before_comma": ("bool", "space before comma", False, False),
    "space.inside_parens": ("bool", "space just inside ( )", False, False),
    "space.inside_brackets": ("bool", "space just inside [ ]", False, False),
    "space.inside_braces": ("bool", "space just inside { } in literals", False, False),
    "space.before_call_paren": ("bool", "space before a call's (", False, False),
    "space.before_template_angle": ("bool", "space before a template's <", False, False),
    "space.inside_template_angle": ("bool", "space just inside a template's < >", False, False),
    "space.before_semicolon": ("bool", "space before ; inside for(...)", False, False),
    "space.after_semicolon": ("bool", "space after ; inside for(...)", True, True),
    "space.around_colon_in_for": ("str", "spacing around a meaningful colon", "spaced", "after"),
    "space.around_pointer": ("str", "* & attachment in declarations", "name", "n/a"),
    "blank.max_in_block": ("int", "max consecutive blank rows inside a block", 1, 1),
    "blank.between_functions": ("int", "blank rows between top-level definitions", 1, 2),
    "blank.between_methods": ("int", "blank rows between methods inside a class", 1, 1),
    "blank.after_open_brace": ("bool", "blank row right after an opening brace", False, False),
    "blank.before_close_brace": ("bool", "blank row right before a closing brace", False, False),
    "blank.file_start": ("bool", "file starts with a blank row", False, False),
    "blank.file_end": ("int", "blank rows at end of file", 0, 0),
    "blank.final_newline": ("bool", "file ends with a newline", True, True),
    "blank.trailing_whitespace": ("bool", "rows keep trailing whitespace", False, False),
    "comment.style": ("str", "dominant comment syntax", "//", "#"),
    "comment.space_after_marker": ("bool", "space after the comment marker", True, True),
    "comment.doc_style": ("str", "documentation comment syntax", "none", "docstring"),
    "comment.block_ratio": ("int", "percent of comments that are block comments", 0, 0),
    "quote.style": ("str", "preferred string quote", "double", "double"),
    "semicolon.required": ("bool", "statements end with ;", True, False),
    "constant.marker": ("str", "how constants are marked", "const", "none"),
    "constant.placement": ("str", "const sits before or after the type", "before-type", "before-type"),
    "statement.comma_join": ("bool", "short calls joined with , instead of ;", False, False),
    "layout.case_indent": ("str", "case labels inside switch", "flush", "n/a"),
    "layout.wrap_operator": ("str", "operator position when a row wraps", "after", "after"),
    "layout.args_per_row": ("str", "argument layout in wrapped lists", "packed", "packed"),
    "layout.trailing_comma": ("bool", "trailing comma in multi-line lists", False, True),
    "layout.single_statement_same_row": ("bool", "if (x) return; kept on one row", False, False),
    "layout.chained_call_indent": ("str", "indent of chained .method rows", "same", "same"),
    "naming.function": ("str", "function names", "snake_case", "snake_case"),
    "naming.method": ("str", "method names", "snake_case", "snake_case"),
    "naming.variable": ("str", "local variable names", "snake_case", "snake_case"),
    "naming.parameter": ("str", "parameter names", "snake_case", "snake_case"),
    "naming.constant": ("str", "constant names", "UPPER_SNAKE_CASE", "UPPER_SNAKE_CASE"),
    "naming.type": ("str", "type/class names", "PascalCase", "PascalCase"),
    "naming.member": ("str", "field / attribute names", "snake_case", "snake_case"),
    "naming.macro": ("str", "macro names", "UPPER_SNAKE_CASE", "n/a"),
    "include.order": ("str", "include/import ordering", "none", "none"),
    "include.grouping": ("str", "include/import grouping", "none", "none"),
}


RELIABILITY_K = 3.0  # shrinkage constant: confidence is pulled toward 0 for thin evidence
MIN_EVIDENCE = {"int": 1, "bool": 2, "str": 2}

# Structural facts appear only once or twice per file (a file has one namespace style), so the
# generic shrinkage would keep them permanently below any usable threshold.
LOW_COUNT_RULES = {
    "indent.namespace_body": 1.0,
    "brace.empty_block": 1.0,
    "brace.close_same_row_branch": 1.0,
    "layout.case_indent": 1.0,
    "space.around_pointer": 1.0,
    "blank.after_open_brace": 1.0,
    "blank.before_close_brace": 1.0,
    "blank.file_start": 1.0,
    "blank.file_end": 1.0,
    "blank.final_newline": 1.0,
    "comment.doc_style": 1.0,
    "include.order": 1.0,
    "include.grouping": 1.0,
}


def reliability_for(rule: str) -> float:
    return LOW_COUNT_RULES.get(rule, RELIABILITY_K)


def label_for(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


@dataclass
class Feature:
    """One style axis: a distribution of observed values plus the evidence rows behind it."""

    key: str
    counts: dict[str, int] = field(default_factory=dict)
    rows: dict[str, list[int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def value(self) -> str | None:
        if not self.counts:
            return None
        top = max(self.counts.values())
        candidates = [v for v, n in self.counts.items() if n == top]

        def rank(v: str) -> tuple[int, float]:
            try:
                return (1, float(v))  # ties resolve to the larger measured value
            except (TypeError, ValueError):
                return (0, 0.0)

        return max(candidates, key=rank)

    @property
    def support(self) -> int:
        v = self.value
        return self.counts.get(v, 0) if v else 0

    @property
    def agreement(self) -> float:
        return self.support / self.total if self.total else 0.0

    @property
    def confidence(self) -> float:
        """Agreement shrunk by how thin the evidence is, so 1 observation never looks like a rule."""
        if not self.total:
            return 0.0
        reliability = self.total / (self.total + reliability_for(self.key))
        return round(self.agreement * reliability, 3)

    @property
    def thin(self) -> bool:
        kind = FEATURE_SPECS.get(self.key, ("str",))[0]
        return self.total < MIN_EVIDENCE.get(kind, 2)

    def outlier_rows(self, limit: int = 12) -> list[tuple[str, list[int]]]:
        """Values other than the dominant one, with the rows they were seen on."""
        top = self.value
        out = []
        for val, rows in sorted(self.rows.items()):
            if val == top or not rows:
                continue
            out.append((val, rows[:limit]))
        return sorted(out, key=lambda item: -self.counts.get(item[0], 0))

    def as_typed(self) -> Any:
        v = self.value
        kind = FEATURE_SPECS.get(self.key, ("str",))[0]
        if v is None:
            return None
        if kind == "bool":
            return v == "true"
        if kind == "int":
            try:
                # ``>79`` is how an over-long row is recorded: a deviating *value* for the checker,
                # but the number a formatter needs is still 79.
                return int(float(str(v).lstrip(">")))
            except ValueError:
                return None
        return v

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "support": self.support,
            "total": self.total,
            "agreement": round(self.agreement, 3),
            "confidence": self.confidence,
            "thin": self.thin,
            "distribution": dict(sorted(self.counts.items(), key=lambda kv: -kv[1])),
            "rows": {k: v[:12] for k, v in self.rows.items() if k != self.value},
        }

    @classmethod
    def from_dict(cls, key: str, data: dict) -> "Feature":
        counts = {str(k): int(v) for k, v in (data.get("distribution") or {}).items()}
        rows = {str(k): [int(r) for r in v] for k, v in (data.get("rows") or {}).items()}
        f = cls(key=key, counts=counts, rows=rows)
        top = data.get("value")
        if top is not None and top not in rows:
            f.rows.setdefault(top, [])
        return f


SUM_STATS = {"rows", "code_rows", "tokens", "comments", "line_comments", "block_comments", "import_rows", "over_limit_rows"}


@dataclass
class Sample:
    path: str
    language: str
    rows: int
    tokens: int
    chars: int
    score: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class Profile:
    """A user's style: dominant values per feature, with per-sample provenance."""

    user: str = "default"
    language: str = "clike"
    family: str = "clike"
    features: dict[str, Feature] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    version: int = PROFILE_VERSION

    # -- values ---------------------------------------------------------------
    def value(self, key: str, default: Any = None) -> Any:
        f = self.features.get(key)
        if f is None:
            return default
        v = f.as_typed()
        return default if v is None else v

    def feature(self, key: str) -> Feature | None:
        return self.features.get(key)

    def confidence(self, key: str) -> float:
        f = self.features.get(key)
        return f.confidence if f else 0.0

    def effective(self) -> dict[str, tuple[Any, float, bool]]:
        """Every known feature resolved: observed value or the family default (marked as such)."""
        out: dict[str, tuple[Any, float, bool]] = {}
        default_key = 3 if self.family == "python" else 2
        for key, spec in FEATURE_SPECS.items():
            f = self.features.get(key)
            if f is not None and f.value is not None:
                out[key] = (f.as_typed(), f.confidence, True)
            else:
                out[key] = (spec[default_key], 0.0, False)
        return out

    # -- merging --------------------------------------------------------------
    def record(self, key: str, value: Any, row: int | None = None) -> None:
        f = self.features.setdefault(key, Feature(key))
        sv = ("true" if value else "false") if isinstance(value, bool) else str(value)
        f.counts[sv] = f.counts.get(sv, 0) + 1
        if row is not None:
            f.rows.setdefault(sv, [])
            if len(f.rows[sv]) < 200:
                f.rows[sv].append(row)

    def merge(self, other: "Profile") -> "Profile":
        for f in other.features.values():
            dst = self.features.setdefault(f.key, Feature(f.key))
            for val, n in f.counts.items():
                dst.counts[val] = dst.counts.get(val, 0) + n
                src_rows = f.rows.get(val, [])
                dst.rows.setdefault(val, [])
                for r in src_rows:
                    if len(dst.rows[val]) < 200 and r not in dst.rows[val][-20:]:
                        dst.rows[val].append(r)
        known = {s.path for s in self.samples}
        for s in other.samples:
            if s.path not in known:
                self.samples.append(s)
        self.merge_stats(other.stats)
        self.updated = time.time()
        return self

    @staticmethod
    def combine(profiles: Iterable["Profile"], user: str = "combined") -> "Profile":
        base: Profile | None = None
        for p in profiles:
            if base is None:
                base = Profile.from_dict(p.to_dict())
            else:
                base.merge(p)
        if base is None:
            base = Profile(user=user)
        base.user = user
        return base

    # -- serialisation --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "user": self.user,
            "language": self.language,
            "family": self.family,
            "created": self.created,
            "updated": self.updated,
            "samples": [s.to_dict() for s in self.samples],
            "stats": self.stats,
            "features": {k: f.to_dict() for k, f in sorted(self.features.items())},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        p = cls(
            user=data.get("user", "default"),
            language=data.get("language", "clike"),
            family=data.get("family", "clike"),
            created=data.get("created", time.time()),
            updated=data.get("updated", time.time()),
            version=data.get("version", PROFILE_VERSION),
        )
        p.features = {k: Feature.from_dict(k, v) for k, v in (data.get("features") or {}).items()}
        p.samples = [Sample(**s) for s in (data.get("samples") or [])]
        p.stats = dict(data.get("stats") or {})
        return p

    def merge_stats(self, other: dict) -> None:
        """Counts add up across samples; width percentiles keep the widest value seen."""
        for k, v in other.items():
            cur = self.stats.get(k)
            if isinstance(v, (int, float)) and isinstance(cur, (int, float)):
                self.stats[k] = cur + v if k in SUM_STATS else max(cur, v)
            elif cur is None:
                self.stats[k] = v
        self.stats["samples"] = len(self.samples)

    # -- reporting ------------------------------------------------------------
    def table(self, min_confidence: float = 0.0) -> list[dict]:
        rows = []
        for key, (value, conf, observed) in sorted(self.effective().items()):
            if observed and conf < min_confidence:
                continue
            f = self.features.get(key)
            rows.append(
                {
                    "key": key,
                    "label": FEATURE_SPECS[key][1],
                    "value": label_for(value) if isinstance(value, bool) else value,
                    "confidence": conf,
                    "observed": observed,
                    "support": f.support if f and observed else 0,
                    "total": f.total if f and observed else 0,
                }
            )
        return rows

    def summary(self, min_confidence: float = 0.4) -> dict:
        obs = {k: f for k, f in self.features.items() if f.value is not None and not f.thin and f.confidence >= min_confidence}
        return {
            "user": self.user,
            "language": self.language,
            "samples": len(self.samples),
            "confident_features": len(obs),
            "top_features": {
                k: obs[k].as_typed() for k in sorted(obs, key=lambda x: -obs[x].confidence)[:12]
            },
        }
