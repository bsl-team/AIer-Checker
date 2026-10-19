"""Style extraction: turn a document into observed feature values with evidence rows."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from .document import Document, expand_width
from .naming import classify, roles
from .profile import Feature, Profile, Sample
from .tokens import CHR, CMT, ID, KW, NUM, PUNCT, PRE, STR, Token

VIS_TAB = 8  # visual width used for line-length statistics

CONTROL_PAREN_KW = {"if", "for", "while", "switch", "catch", "synchronized", "match", "case", "unless", "elif", "with", "do"}
BIN_OPS = {
    "+", "-", "*", "/", "%", "==", "!=", "<", ">", "<=", ">=", "&&", "||", "!==", "===",
    "<<", ">>", "&", "|", "^", "->", "=>", "==", "!=", "<=>",
}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=", ":=", "**="}


@dataclass
class Extractor:
    doc: Document
    p: Profile = field(default=None)

    def __post_init__(self):
        self.p = Profile(language=self.doc.spec.name, family=self.doc.spec.family)
        self._space_like = [r for r in self.doc.rows if not r.blank and not r.in_trivia and r.tokens]
        self._levels = None
        self.unit = 4

    # -- helpers ---------------------------------------------------------
    def rec(self, key: str, value, row: int | None = None) -> None:
        self.p.record(key, value, row)

    def is_python(self) -> bool:
        return self.doc.spec.family == "python"

    def code_rows(self) -> list:
        return self._space_like

    def indent_levels(self) -> dict[int, int]:
        if self._levels is None:
            if self.is_python():
                from .py_lang import python_levels

                self._levels = python_levels(self.doc.src)
            else:
                self._levels = {r.num: self.doc.depths[r.num - 1][0] for r in self.doc.rows}
        return self._levels

    # -- entry ----------------------------------------------------------
    def run(self) -> Profile:
        self._indent()
        self._namespace_indent()
        self._width()
        self._braces()
        self._spacing()
        self._blank_lines()
        self._comments()
        self._quotes()
        self._layout()
        self._naming()
        self._imports()
        self._semicolons()
        self.p.samples = [
            Sample(
                path=self.doc.path,
                language=self.doc.spec.name,
                rows=len(self.doc.lines),
                tokens=len(self.doc.tokens),
                chars=len(self.doc.src),
            )
        ]
        self.p.stats["samples"] = 1
        return self.p

    # -- indentation ----------------------------------------------------
    def _indent(self) -> None:
        levels = self.indent_levels()
        rows: list[tuple[int, int, int]] = []  # (width, level, row)
        for r in self.code_rows():
            if r.directive or not r.indent:
                continue
            if self.doc.paren_depth[r.num - 1]:
                continue  # wrapped row: measured as continuation
            ind = r.indent
            has_tab, has_space = "\t" in ind, " " in ind
            kind = "mixed" if (has_tab and has_space) else ("tab" if has_tab else "space")
            self.rec("indent.type", kind, r.num)
            if kind == "mixed":
                continue
            width = ind.count("\t") if kind == "tab" else len(ind)
            if width > 0:
                rows.append((width, levels.get(r.num, 0), r.num))
        self.unit = _indent_unit([(w, lv) for w, lv, _n in rows])
        for width, _level, num in rows:
            fits = width % self.unit == 0
            self.rec("indent.size", self.unit if fits else f"irregular:{width}", num)
        self._continuation()

    def _continuation(self) -> None:
        row_group = _innermost_groups(self.doc)
        for num, (open_tok, opener_row, aligned_col) in sorted(row_group.items()):
            r = self.doc.row(num)
            if not r.tokens:
                continue
            first = r.tokens[0]
            if first.col == aligned_col or first.col == open_tok.col + 1:
                self.rec("indent.continuation", "aligned", num)
                continue
            base = self._indent_measure(self.doc.row(opener_row))
            delta = self._indent_measure(r) - base
            if 0 < delta <= 24:
                self.rec("indent.continuation", delta, num)

    def _indent_measure(self, row) -> int:
        """Indent expressed in the file's own indent unit, so tab files stay in tabs."""
        ind = row.indent
        if "\t" in ind and " " not in ind:
            return ind.count("\t")
        return row.indent_width(VIS_TAB)

    def _row_level(self, row_num: int) -> int:
        if self.is_python():
            return self.indent_levels().get(row_num, 0)
        return self.doc.depths[row_num - 1][0]

    # -- line length ----------------------------------------------------
    def _width(self) -> None:
        measured = [(r.num, r.width(VIS_TAB)) for r in self.code_rows()]
        widths = [w for _, w in measured]
        limit, _fit = _line_length_limit(widths)
        for num, w in measured:
            self.rec("width.limit", limit if w <= limit else f">{limit}", num)
        stats = self.p.stats
        stats["max_line_width"] = max(widths) if widths else 0
        stats["p95_line_width"] = _percentile(widths, 95)
        stats["median_line_width"] = _percentile(widths, 50)
        stats["rows"] = len(self.doc.lines)
        stats["code_rows"] = len(measured)
        stats["over_limit_rows"] = sum(1 for _, w in measured if w > limit)
        stats["tokens"] = len(self.doc.tokens)
        stats["comments"] = sum(1 for t in self.doc.tokens if t.kind == CMT)

    # -- braces ---------------------------------------------------------
    def _braces(self) -> None:
        if self.is_python():
            return
        toks = self.doc.tokens
        for i, t in enumerate(toks):
            pr = toks[i - 1] if i else None
            nx = toks[i + 1] if i + 1 < len(toks) else None
            if pr is None:
                continue
            if t.is_punct("{"):
                if pr.is_punct("=", ",", "[", "(", "{", "?"):
                    continue  # aggregate / initialiser brace, not a block
                rtoks = [x for x in self.doc.row(t.row).tokens if x.kind != CMT]
                pos = next((k for k, x in enumerate(rtoks) if x.index == t.index), -1)
                if pos == 0 and len(rtoks) == 1:
                    self.rec("brace.style", "next", t.row)
                elif pos > 0:
                    self.rec("brace.style", "attach", t.row)
                if nx is not None and nx.is_punct("}"):
                    self.rec("brace.empty_block", "compact" if nx.row == t.row else "expanded", t.row)
                continue
            if t.is_punct("}"):
                after = _next_code(toks, i)
                if after is not None and after.kind == KW and after.text in ("else", "catch", "finally", "while"):
                    self.rec("brace.close_same_row_branch", after.row == t.row, t.row)

    # -- intra-row spacing ----------------------------------------------
    def _spacing(self) -> None:
        toks = self.doc.tokens
        depths = self.doc.token_depths()
        for i, t in enumerate(toks):
            if t.kind in (CMT, STR, PRE):
                continue
            pr = toks[i - 1] if i else None
            nx = toks[i + 1] if i + 1 < len(toks) else None
            right = nx is not None and nx.row == t.end_row and nx.kind != CMT
            left = pr is not None and t.gap >= 0 and pr.kind != CMT
            braces, parens, brackets = depths[i]
            first = self.doc.row(t.row).first
            row_is_control = first is not None and first.kind == KW and first.text in CONTROL_PAREN_KW
            row_is_label = first is not None and first.kind == KW and first.text in ("case", "default")
            if t.kind == KW and t.text in CONTROL_PAREN_KW and right and nx.is_punct("("):
                self.rec("space.after_keyword", nx.gap > 0, t.row)
            elif t.kind == ID and right and nx.is_punct("(") and t.text not in ("return",):
                self.rec("space.before_call_paren", nx.gap > 0, t.row)
            if t.is_punct(","):
                if right and not nx.is_punct(")", "]", "}"):
                    self.rec("space.after_comma", nx.gap > 0, t.row)
                if left and not pr.is_punct("("):
                    self.rec("space.before_comma", t.gap > 0, t.row)
            if t.is_punct("(") and right and not nx.is_punct(")"):
                self.rec("space.inside_parens", nx.gap > 0, t.row)
            if t.is_punct(")") and left and not pr.is_punct("("):
                self.rec("space.inside_parens", t.gap > 0, t.row)
            if t.is_punct("[") and right and not nx.is_punct("]"):
                self.rec("space.inside_brackets", nx.gap > 0, t.row)
            if t.is_punct("]") and left and not pr.is_punct("["):
                self.rec("space.inside_brackets", t.gap > 0, t.row)
            if t.is_punct("{") and right:
                close = _same_row_close(toks, i)
                if close is not None and close > i + 1:
                    self.rec("space.inside_braces", toks[i + 1].gap > 0, t.row)
                    self.rec("space.inside_braces", toks[close].gap > 0, t.row)
            if t.is_punct(";") and left and right and _inside_paren(self.doc, toks, i):
                self.rec("space.before_semicolon", t.gap > 0, t.row)
                self.rec("space.after_semicolon", nx.gap > 0, t.row)
            if t.is_punct(":") and left and right and not pr.is_punct("?", ":") and not nx.is_punct(":"):
                if (pr.kind in (ID, NUM, STR, CHR) or pr.is_punct(")", "]")) and not row_is_label:
                    if colon_context(braces, parens, brackets, row_is_control):
                        self.rec("space.around_colon_in_for", colon_style(t, nx), t.row)
            if t.kind == PUNCT and t.text in ("*", "&", "&&"):
                self._pointer(toks, i)
            if t.kind == PUNCT and (t.text in BIN_OPS or t.text in ASSIGN_OPS):
                if t.text in ("<", ">") and t.gap == 0 and nx.gap == 0:
                    continue  # template angles such as vector<int>, not a comparison
                if t.text == "**":
                    continue  # exponent / **kwargs / **pointer: spacing depends on the operands
                if left and right and self._is_binary(toks, i):
                    key = "space.around_assignment" if t.text in ASSIGN_OPS else "space.around_operators"
                    self.rec(key, t.gap > 0 and nx.gap > 0, t.row)

    def _pointer(self, toks, i) -> None:
        t = toks[i]
        pr = toks[i - 1] if i >= 1 else None
        nx = toks[i + 1] if i + 1 < len(toks) else None
        ppr = toks[i - 2] if i >= 2 else None
        if ppr is not None and ppr.row != t.row:
            ppr = None
        if not is_pointer_op(ppr, pr, t, nx):
            return
        value = None
        if t.gap > 0 and nx.gap == 0:
            value = "name"  # `int *p`: the star hugs the name
        elif t.gap == 0 and nx.gap > 0:
            value = "type"  # `int* p`: the star hugs the type
        elif t.gap > 0 and nx.gap > 0:
            value = "spaced"
        if value:
            self.rec("space.around_pointer", value, t.row)

    def _is_binary(self, toks, i) -> bool:
        t = toks[i]
        pr = toks[i - 1] if i else None
        if pr is None:
            return False
        if pr.kind in (ID, NUM, STR, CHR):
            return True
        if pr.kind == PUNCT and pr.text in (")", "]"):
            return True
        if pr.kind == KW and pr.text in ("this", "true", "false", "nullptr", "NULL"):
            return True
        return False

    # -- blank lines ----------------------------------------------------
    def _blank_lines(self) -> None:
        rows = self.doc.rows
        if not rows:
            return
        for r in rows:
            if r.blank or r.in_trivia or not r.tokens:
                continue
            self.rec("blank.trailing_whitespace", r.raw != r.raw.rstrip(), r.num)
        self.rec("blank.final_newline", self.doc.src.endswith("\n"), len(rows))
        self.rec("blank.file_start", rows[0].blank)
        trailing = 0
        for r in reversed(rows):
            if r.blank:
                trailing += 1
            else:
                break
        self.rec("blank.file_end", min(trailing, 4))
        self._blank_runs()

    def _blank_runs(self) -> None:
        rows = self.doc.rows
        run = 0
        run_start = 0
        run_in_string = False
        for idx, r in enumerate(rows):
            if r.blank:
                if run == 0:
                    run_start = r.num
                    run_in_string = r.in_string
                run += 1
                continue
            if run:
                if not run_in_string:
                    self._one_blank_run(run, run_start, idx)
                run = 0
                run_in_string = False
        if run and not run_in_string:
            self._one_blank_run(run, run_start, len(rows) - 1)

    def _one_blank_run(self, run: int, start: int, end_idx: int) -> None:
        rows = self.doc.rows
        prev_idx = None
        for k in range(start - 2, -1, -1):
            if rows[k].tokens and not rows[k].in_trivia:
                prev_idx = k
                break
        next_idx = None
        for k in range(end_idx, len(rows)):
            if rows[k].tokens and not rows[k].in_trivia:
                next_idx = k
                break
        depth_after = self._row_level(prev_idx + 1) if prev_idx is not None else 0
        depth_at_next = self._row_level(next_idx + 1) if next_idx is not None else depth_after
        if depth_at_next > 0 and next_idx is not None:
            self.rec("blank.max_in_block", min(run, 4), start)
        prev_row = rows[prev_idx] if prev_idx is not None else None
        next_row = rows[next_idx] if next_idx is not None else None
        if prev_row is not None and prev_row.tokens and prev_row.tokens[-1].is_punct("{"):
            self.rec("blank.after_open_brace", run >= 1, start)
        if next_row is not None and next_row.tokens and next_row.tokens[0].is_punct("}"):
            self.rec("blank.before_close_brace", run >= 1, start)
        if next_row is not None and self._is_def_row(next_row):
            key = "blank.between_functions" if self._row_level(next_row.num) == 0 else "blank.between_methods"
            self.rec(key, min(run, 4), next_row.num)

    def _is_def_row(self, row) -> bool:
        if not row.tokens:
            return False
        if self.is_python():
            return row.first.kind == KW and row.first.text in ("def", "async", "class")
        if self._row_level(row.num) != 0:
            return False
        return is_clike_definition([t for t in row.tokens if t.kind != CMT])

    def _namespace_indent(self) -> None:
        """Does the author indent the body of ``namespace X {``? C++ styles split evenly here."""
        if self.is_python():
            return
        rows = self.doc.rows
        for idx, row in enumerate(rows):
            first = row.first
            if first is None or first.kind != KW or first.text not in ("namespace", "module"):
                continue
            if not any(t.is_punct("{") for t in row.tokens if t.kind != CMT):
                continue
            base = _indent_chars(row.indent)
            unit = self.unit or 4
            for nxt in rows[idx + 1 :]:
                if nxt.blank or nxt.in_trivia or not nxt.tokens:
                    continue
                inner = _indent_chars(nxt.indent)
                if inner == base:
                    self.rec("indent.namespace_body", "none", nxt.num)
                elif inner == base + unit:
                    self.rec("indent.namespace_body", "indent", nxt.num)
                elif inner < base:
                    self.rec("indent.namespace_body", "flush", nxt.num)
                else:
                    self.rec("indent.namespace_body", "other", nxt.num)
                break

    # -- comments -------------------------------------------------------
    def _comments(self) -> None:
        spec = self.doc.spec
        line = block = 0
        for t in self.doc.tokens:
            if t.kind != CMT:
                continue
            is_block = any(t.text.startswith(op) for op, _ in spec.block_comments)
            if is_block:
                block += 1
                self.rec("comment.style", spec.block_comments[0][0], t.row)
                if t.text.startswith("/**") and not t.text.startswith("/***/"):
                    self.rec("comment.doc_style", "doxygen", t.row)
            else:
                line += 1
                self.rec("comment.style", spec.line_comments[0], t.row)
                body = t.text[len(spec.line_comments[0]) :] if t.text.startswith(spec.line_comments[0]) else t.text
                if body.startswith(("/", "!")):
                    self.rec("comment.doc_style", "triple-slash", t.row)
                    continue
                self.rec("comment.space_after_marker", body[:1] in (" ", "\t") or body == "", t.row)
        stats = self.p.stats
        stats["line_comments"] = line
        stats["block_comments"] = block
        if self.is_python():
            try:
                from .py_lang import analyze_ast

                sem = analyze_ast(self.doc.src)
                if sem.docstring_functions or sem.docstring_classes:
                    self.rec("comment.doc_style", "docstring", 1)
            except Exception:
                pass

    # -- string quotes ---------------------------------------------------
    def _quotes(self) -> None:
        spec = self.doc.spec
        if not (spec.single_quote_strings and spec.double_quote_strings or spec.backtick_strings):
            return
        for t in self.doc.tokens:
            if t.kind != STR or t.multiline:
                continue
            body = t.text.lstrip("rRbBuUfF")
            if not body:
                continue
            if body.startswith(spec.line_comments[0][:1]):
                continue
            if body[0] == '"':
                self.rec("quote.style", "double", t.row)
            elif body[0] == "'":
                self.rec("quote.style", "single", t.row)
            elif body[0] == "`":
                self.rec("quote.style", "backtick", t.row)

    # -- layout ---------------------------------------------------------
    def _layout(self) -> None:
        self._wrap_operator()
        self._argument_layout()
        self._case_indent()
        self._single_statement()
        self._chained()

    def _wrap_operator(self) -> None:
        groups = _innermost_groups(self.doc)
        for r in self.code_rows():
            if r.directive or len(r.tokens) < 2:
                continue
            rtoks = [t for t in r.tokens if t.kind != CMT]
            last = rtoks[-1]
            if last.kind == PUNCT and (last.text in BIN_OPS or last.text in ASSIGN_OPS) and last.text not in ("*", "&", "++", "--", "<", ">"):
                self.rec("layout.wrap_operator", "after", r.num)
            first = rtoks[0]
            if first.kind == PUNCT and first.text in BIN_OPS and r.num in groups:
                self.rec("layout.wrap_operator", "before", r.num)

    def _argument_layout(self) -> None:
        for grp in _groups(self.doc):
            ot, ct = grp["open"], grp["close"]
            if ct.row == ot.row:
                continue
            row_obj = self.doc.row(ot.row)
            rtoks = [t for t in row_obj.tokens if t.kind != CMT]
            packed = ot.index < rtoks[-1].index
            self.rec("layout.args_per_row", "packed" if packed else "one-per-row", ot.row)
            prev = _prev_code(self.doc.tokens, grp["close_idx"])
            if prev is not None:
                self.rec("layout.trailing_comma", prev.is_punct(","), ct.row)

    def _case_indent(self) -> None:
        if self.is_python():
            return
        unit = self.unit or 4
        for r in self.code_rows():
            first = r.first
            if first is None or first.kind != KW or first.text not in ("case", "default"):
                continue
            width = r.indent_width(VIS_TAB)
            if width < unit:
                self.rec("layout.case_indent", "flush", r.num)
            else:
                self.rec("layout.case_indent", "indented", r.num)

    def _single_statement(self) -> None:
        if self.is_python():
            return
        toks = self.doc.tokens
        for i, t in enumerate(toks):
            if t.kind != KW or t.text not in ("if", "for", "while", "switch"):
                continue
            j = i + 1
            if j >= len(toks) or not toks[j].is_punct("("):
                continue
            close = _match(toks, j)
            if close is None:
                continue
            after = _next_code(toks, close)
            if after is None:
                continue
            same_row = after.row == toks[close].row
            if same_row or not after.is_punct("{"):
                self.rec("layout.single_statement_same_row", same_row and not after.is_punct("{"), after.row)

    def _chained(self) -> None:
        prev_code_row = None
        for r in self.code_rows():
            first = r.first
            if first is not None and first.is_punct(".") and prev_code_row is not None:
                cur = r.indent_width(VIS_TAB)
                old = prev_code_row.indent_width(VIS_TAB)
                self.rec("layout.chained_call_indent", "same" if cur <= old else "extra", r.num)
            if r.tokens:
                prev_code_row = r

    # -- naming ---------------------------------------------------------
    def _naming(self) -> None:
        for role, names in roles(self.doc).items():
            for row, name in names:
                style = classify(name)
                if style:
                    self.rec(f"naming.{role}", style, row)

    # -- imports --------------------------------------------------------
    def _imports(self) -> None:
        runs = _import_runs(self.doc)
        for rows in runs:
            keys = [k for _, k in rows]
            ordered = keys == sorted(keys, key=lambda s: s.lower())
            self.rec("include.order", "sorted" if ordered else "as-written", rows[0][0])
            kinds = {("system" if k.startswith("<") else "local") for k in keys}
            self.rec("include.grouping", "grouped" if len(kinds) > 1 else "flat", rows[0][0])
        self.p.stats["import_rows"] = sum(len(r) for r in runs)

    # -- semicolons ------------------------------------------------------
    def _semicolons(self) -> None:
        if self.doc.spec.name not in ("javascript", "typescript"):
            return
        for r in self.code_rows():
            rtoks = [t for t in r.tokens if t.kind != CMT]
            if not rtoks:
                continue
            last = rtoks[-1]
            if last.is_punct(";"):
                self.rec("semicolon.required", True, r.num)
            elif last.kind in (ID, NUM, STR, CHR) or last.is_punct(")", "]"):
                if self.doc.paren_depth[r.num - 1] == 0 and not any(t.is_punct("{") for t in rtoks):
                    self.rec("semicolon.required", False, r.num)


def _indent_unit(pairs: list[tuple[int, int]]) -> int:
    """Infer the indent step from ``(width, level)`` pairs.

    Depth-anchored scoring first: it is exact for python (tokenize reports the level) and for
    brace-aligned clike code.  When depth lies - a file that does not indent namespace bodies,
    or ``case`` labels flush with their switch - fall back to scoring candidate units (the
    distinct widths and their differences) by how many indents are an exact multiple of them.
    """
    widths = [w for w, _ in pairs if w > 0]
    if not widths:
        return 4
    distinct = sorted(set(widths))
    usable = [(w, lv) for w, lv in pairs if lv >= 1]
    if len(usable) >= 3:
        best_unit, best_hits = 0, 0
        for unit in range(1, 13):
            hits = sum(1 for w, lv in usable if w == unit * lv)
            if hits > best_hits:
                best_unit, best_hits = unit, hits
        if best_hits / len(usable) >= 0.6:
            return best_unit
    cands = set(distinct) | {b - a for a, b in zip(distinct, distinct[1:])}
    cands = {c for c in cands if 1 < c <= 12}
    if cands:
        def score(u: int) -> tuple[float, int]:
            fit = sum(1 for w in widths if w % u == 0) / len(widths)
            return (fit, u)

        best = max(cands, key=score)
        if score(best)[0] >= 0.6:
            return best
    return distinct[0] if distinct[0] <= 8 else 4


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[k]


def _line_length_limit(widths: list[int]) -> tuple[int, float]:
    """Smallest conventional limit that keeps the p95 row inside and leaves <=1% over."""
    if not widths:
        return 80, 0.0
    p95 = _percentile(widths, 95)
    total = len(widths)
    for cand in (79, 80, 88, 100, 110, 120, 140, 160, 200):
        over = sum(1 for w in widths if w > cand)
        if cand >= p95 and over <= max(0, int(0.01 * total)):
            return cand, 1 - over / total
    for cand in (79, 80, 88, 100, 110, 120, 140, 160, 200, 240, 300):
        over = sum(1 for w in widths if w > cand)
        if over / total <= 0.05:
            return cand, 1 - over / total
    return max(widths), 0.4


TYPEISH_KW = {
    "class", "struct", "enum", "union", "namespace", "interface", "impl", "trait",
    "template", "module", "extern", "func", "fn", "def", "record",
}


def is_clike_definition(toks: list[Token]) -> bool:
    """A row that opens a top-level definition: it must contain ``{`` and look like a header."""
    if not toks:
        return False
    if not any(t.is_punct("{") for t in toks):
        return False
    if any(t.is_punct(")") for t in toks):
        return True
    return toks[0].kind == KW and toks[0].text in TYPEISH_KW


POINTER_PPR_PUNCT = {"(", ",", ";", "{", "}", "[", "*", "&", "::", "->", "<", ">"}
POINTER_PPR_KW = {
    "const", "constexpr", "static", "extern", "inline", "virtual", "explicit", "mutable",
    "typedef", "unsigned", "signed", "register", "thread_local", "volatile", "auto",
    "struct", "class", "union", "enum",
}


def is_pointer_op(ppr, pr, tok, nx) -> bool:
    """Is ``tok`` a ``*``/``&`` declarator rather than multiplication or bitwise-and?

    Requires a type-ish token on the left, a name on the right, and a left neighbour that
    still makes declaration sense: ``return a * b`` and ``c = a * b`` are arithmetic.
    """
    if tok is None or tok.kind != PUNCT or tok.text not in ("*", "&", "&&"):
        return False
    if pr is None or nx is None:
        return False
    if pr.kind not in (ID, KW) or nx.kind != ID:
        return False
    if pr.row != tok.row or nx.row != tok.row:
        return False
    if ppr is None:
        return True
    if ppr.kind == PUNCT:
        return ppr.text in POINTER_PPR_PUNCT
    if ppr.kind == KW:
        return ppr.text in POINTER_PPR_KW
    return False


def colon_style(colon: Token, following: Token) -> str:
    """How the author spaces a meaningful ``:`` - one boolean cannot describe dict habits."""
    before, after = colon.gap > 0, following.gap > 0
    if before and after:
        return "spaced"
    if after:
        return "after"
    if before:
        return "before"
    return "tight"


def colon_context(braces: int, parens: int, brackets: int, row_is_control: bool) -> bool:
    """Where a ``:`` spacing habit is meaningful.

    Slices and subscripts (``a[1:2]``) want no spaces, annotations (``x: int``) want a space
    only after, and range-for / dict literals want spaces around - one shared rule would
    rewrite one of them wrongly, so only dict/set bodies and control-paren loops are measured.
    """
    if brackets > 0:
        return False
    if braces > 0:
        return True
    return parens > 0 and row_is_control


def _indent_chars(text: str) -> int:
    """Indent measured in the file's own indent characters (tabs count as one)."""
    if text and "\t" in text and " " not in text:
        return text.count("\t")
    return len(text)


def extract(doc: Document) -> Profile:
    """All feature observations for a single document."""
    return Extractor(doc).run()


def extract_all(docs: list[Document]) -> Profile:
    """Merge per-document observations so one profile represents the whole sample set."""
    profiles = [extract(d) for d in docs]
    if not profiles:
        return Profile(language="clike", family="clike")
    base = profiles[0]
    for other in profiles[1:]:
        base.merge(other)
    langs = Counter(p.language for p in profiles)
    fams = Counter(p.family for p in profiles)
    base.language = langs.most_common(1)[0][0]
    base.family = fams.most_common(1)[0][0]
    return base


_PAIRS = {"(": ")", "[": "]"}
_CLOSERS = {")": "(", "]": "["}
_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _next_code(toks: list[Token], i: int) -> Token | None:
    for j in range(i + 1, len(toks)):
        if toks[j].kind != CMT:
            return toks[j]
    return None


def _prev_code(toks: list[Token], i: int) -> Token | None:
    for j in range(i - 1, -1, -1):
        if toks[j].kind != CMT:
            return toks[j]
    return None


def _match(toks: list[Token], open_idx: int) -> int | None:
    """Index of the closer matching the bracket at ``open_idx`` (which must be an opener)."""
    opener = toks[open_idx].text
    want = _BRACKET_PAIRS.get(opener)
    if want is None:
        return None
    depth = 0
    for j in range(open_idx, len(toks)):
        t = toks[j]
        if t.kind != PUNCT:
            continue
        if t.text == opener:
            depth += 1
        elif t.text == want:
            depth -= 1
            if depth == 0:
                return j
    return None


def _groups(doc: Document) -> list[dict]:
    """Bracket groups that span more than one row, with the column their elements align to."""
    toks = doc.tokens
    stack: list[int] = []
    out = []
    for i, t in enumerate(toks):
        if t.kind == PUNCT and t.text in _PAIRS:
            stack.append(i)
        elif t.kind == PUNCT and t.text in _CLOSERS:
            if not stack:
                continue
            oi = stack.pop()
            ot = toks[oi]
            if t.row > ot.row and ot.kind == PUNCT and t.text == _PAIRS[ot.text]:
                nxt = toks[oi + 1] if oi + 1 < len(toks) and toks[oi + 1].row == ot.row else None
                aligned = nxt.col if nxt is not None else ot.col + 1
                out.append({"open": ot, "close": t, "open_idx": oi, "close_idx": i, "aligned": aligned})
    return out


def _innermost_groups(doc: Document) -> dict[int, tuple[Token, int, int]]:
    """Map each wrapped row to ``(innermost opener, opener row, aligned column)``."""
    out: dict[int, tuple[Token, int, int]] = {}
    for grp in sorted(_groups(doc), key=lambda g: g["open_idx"]):
        ot, ct = grp["open"], grp["close"]
        for row in range(ot.row + 1, ct.row + 1):
            first = doc.row(row).first if row <= len(doc.rows) else None
            if first is not None and first.index == ct.index:
                continue  # the row that only closes the group is not a continuation
            out[row] = (ot, ot.row, grp["aligned"])
    return out


def _same_row_close(toks: list[Token], open_idx: int) -> int | None:
    """Index of the ``}`` that closes the brace at ``open_idx``, when it is on the same row."""
    close = _match(toks, open_idx)
    if close is None or toks[close].row != toks[open_idx].row:
        return None
    return close


def _inside_paren(doc: Document, toks: list[Token], i: int) -> bool:
    return doc.token_depths()[i][1] > 0


_IMPORT_KW = {"import", "from", "using", "include", "require"}


def _import_key(row, doc: Document) -> str | None:
    """The text of an import/include row, or ``None`` when the row is not one."""
    if row.in_trivia or row.blank or not row.tokens:
        return None
    t = row.tokens[0]
    if t.kind == PRE and t.text.lstrip().startswith("#include"):
        return t.text.strip()[len("#include") :].strip()
    if t.kind == KW and t.text in _IMPORT_KW:
        return row.code.strip().rstrip(";")
    return None


def _import_runs(doc: Document) -> list[list[tuple[int, str]]]:
    """Consecutive import rows, split by blank rows so grouping becomes visible."""
    runs: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    for row in doc.rows:
        if row.blank:
            if cur:
                runs.append(cur)
                cur = []
            continue
        key = _import_key(row, doc)
        if key is None:
            if row.tokens:
                break
            continue
        cur.append((row.num, key))
    if cur:
        runs.append(cur)
    return runs
