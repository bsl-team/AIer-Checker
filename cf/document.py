"""Row-oriented view of a source file, shared by extraction and formatting."""
from __future__ import annotations

from dataclasses import dataclass, field

from .tokens import CMT, ID, KW, NUM, PRE, PUNCT, STR, Spec, Token, detect, tokenize_clike


@dataclass
class Line:
    num: int                     # 1-based
    raw: str                     # content without the line break
    indent: str                  # leading whitespace
    tokens: list[Token] = field(default_factory=list)
    in_trivia: bool = False      # part of a multi-line comment/string body
    in_string: bool = False      # part of a multi-line string body (layout-blind)
    continuation: bool = False   # row starts inside an open bracket group
    directive: bool = False       # preprocessor / decorator line kept verbatim

    @property
    def blank(self) -> bool:
        return not self.raw.strip()

    @property
    def code(self) -> str:
        return self.raw[len(self.indent) :]

    @property
    def first(self) -> Token | None:
        return self.tokens[0] if self.tokens else None

    @property
    def last(self) -> Token | None:
        return self.tokens[-1] if self.tokens else None

    def indent_width(self, tab: int = 8) -> int:
        return expand_width(self.indent, tab)

    def width(self, tab: int = 8) -> int:
        return text_width(self.raw, tab)

    def trailing_comment(self) -> Token | None:
        """Comment token on this line that is not the line's only token."""
        for t in self.tokens:
            if t.kind == CMT and len(self.tokens) > 1:
                return t
        return None

    def own_comment(self) -> Token | None:
        t = self.first
        return t if t is not None and t.kind == CMT and len(self.tokens) == 1 else None


def expand_width(text: str, tab: int = 8) -> int:
    """Visual width of a leading-whitespace run."""
    w = 0
    for ch in text:
        if ch == "\t":
            w += tab - (w % tab) if tab else 1
        elif ch == " ":
            w += 1
        else:
            break
    return w


def text_width(text: str, tab: int = 8) -> int:
    """Visual width of a full row: tabs advance to the next tab stop."""
    w = 0
    for ch in text:
        if ch == "\t":
            w += tab - (w % tab) if tab else w + 1
        else:
            w += 1
    return w


@dataclass
class Document:
    path: str
    spec: Spec
    src: str                      # normalised (LF) source
    original_eol: str
    lines: list[str]
    tokens: list[Token]
    rows: list[Line]
    depths: list[tuple[int, int]]  # (bracket depth before line, depth after line)
    paren_depth: list[int]         # non-bracket nesting at line start
    parse_error: str | None = None
    _tdepths: list | None = field(default=None, repr=False)
    _angles: dict | None = field(default=None, repr=False)
    _angle_set: set | None = field(default=None, repr=False)

    @property
    def is_python(self) -> bool:
        return self.spec.family == "python"

    def row(self, num: int) -> Line:
        return self.rows[num - 1]

    def code_rows(self) -> list[Line]:
        return [r for r in self.rows if not r.blank and not r.in_trivia and r.tokens]

    def token_stream(self) -> list[tuple[str, str]]:
        """Comparable identity of the file: whitespace-free, so formatters can be verified."""
        return [(t.kind, t.text) for t in self.tokens]


    def token_depths(self) -> list[tuple[int, int, int]]:
        """``(brace, paren, bracket)`` depth in front of each token, cached on first use.

        Continuation rows need this: within a single row the bracket state is not visible, so
        keyword arguments spread over several rows would otherwise look like plain assignments.
        """
        if self._tdepths is None:
            out = []
            braces = parens = brackets = 0
            for t in self.tokens:
                out.append((braces, parens, brackets))
                if t.kind in (CMT, STR, PRE):
                    continue
                if t.kind == PUNCT:
                    if t.text == "{":
                        braces += 1
                    elif t.text == "}":
                        braces = max(0, braces - 1)
                    elif t.text == "(":
                        parens += 1
                    elif t.text == ")":
                        parens = max(0, parens - 1)
                    elif t.text == "[":
                        brackets += 1
                    elif t.text == "]":
                        brackets = max(0, brackets - 1)
            self._tdepths = out
        return self._tdepths

    def template_angles(self) -> dict[int, int]:
        """Map each template ``<`` to the token index that closes it, cached on first use.

        ``<``/``>`` double as the comparison operators, so an angle only counts as a template
        when its contents can *only* be a type argument list: identifiers, keywords, numbers and
        the separators ``, : * & [ ] ( )``, with every nested ``<`` matched before anything that
        would end an expression.  A ``>>`` closes two levels at once.  This lets the spacing and
        operator rules tell ``vector<int>`` (spacing belongs to the angle) from ``i < n`` (a
        comparison) instead of relying on how tightly the tokens happen to sit.
        """
        if self._angles is None:
            self._angles = _scan_template_angles(self.tokens)
        return self._angles

    def is_template_angle(self, index: int) -> bool:
        """True when the token at ``index`` is a ``<`` or ``>`` that opens/closes a template."""
        if self._angle_set is None:
            angles = self.template_angles()
            self._angle_set = set(angles) | set(angles.values())
        return index in self._angle_set


def _scan_template_angles(tokens: list[Token]) -> dict[int, int]:
    if not tokens:
        return {}
    openers: dict[int, int] = {}
    for i, t in enumerate(tokens):
        if t.kind != PUNCT or t.text != "<":
            continue
        prev = tokens[i - 1] if i else None
        if prev is None or prev.row != t.row:
            continue
        if prev.kind not in (ID, KW):
            continue
        close = _match_angle(tokens, i)
        if close is None:
            continue
        openers[i] = close
    return openers


_ANGLE_INTERIOR = {",", "::", "*", "&", "[", "]", "(", ")"}


def _match_angle(tokens: list[Token], open_idx: int) -> int | None:
    """Walk forward from a ``<``; return the index that balances it, or None if it is not a type list."""
    depth = 0
    for j in range(open_idx, len(tokens)):
        t = tokens[j]
        if t.kind in (CMT, STR, PRE):
            return None
        if t.kind == PUNCT:
            text = t.text
            if text == "<":
                depth += 1
                continue
            if text == ">":
                depth -= 1
                if depth == 0:
                    return j
                continue
            if text == ">>":
                depth -= 2
                if depth <= 0:
                    return j
                continue
            if text in _ANGLE_INTERIOR:
                continue
            return None  # = ; { } + - operators etc: this is an expression, not a type list
        if t.kind in (ID, KW, NUM):
            continue
        return None
    return None


def normalize(src: str) -> tuple[str, str]:
    eol = "\r\n" if "\r\n" in src else ("\r" if "\r" in src and "\n" not in src else "\n")
    return src.replace("\r\n", "\n").replace("\r", "\n"), eol


def build_document(src: str, path: str | None = None, spec: Spec | None = None) -> Document:
    """Lex ``src`` and attach tokens to physical rows, computing bracket depth per row."""
    norm, eol = normalize(src)
    spec = spec or detect(path, norm)
    toks, err = _lex(norm, spec)
    rows_text = norm.split("\n")
    if rows_text and rows_text[-1] == "":
        rows_text.pop()
    rows = [Line(num=i + 1, raw=text, indent=_indent_of(text)) for i, text in enumerate(rows_text)]
    for t in toks:
        if 1 <= t.row <= len(rows):
            rows[t.row - 1].tokens.append(t)
        if t.end_row > t.row:
            for r in range(t.row + 1, min(t.end_row, len(rows)) + 1):
                rows[r - 1].in_trivia = True
                if t.kind == STR:
                    rows[r - 1].in_string = True
        if t.kind == PRE:
            rows[t.row - 1].directive = True
    depths, parens = _depth_scan(rows, spec)
    _mark_continuations(rows, spec)
    return Document(
        path=path or "<stdin>",
        spec=spec,
        src=norm,
        original_eol=eol,
        lines=rows_text,
        tokens=toks,
        rows=rows,
        depths=depths,
        paren_depth=parens,
        parse_error=err,
    )


def _mark_continuations(rows: list[Line], spec: Spec) -> None:
    """Flag rows that begin inside an unclosed bracket.

    In clike only ``(``/``[`` mean the row continues an expression; ``{`` opens a block whose
    indentation is real.  In python a multi-line dict literal is a continuation, so ``{`` counts.
    """
    brace_is_group = spec.family == "python"
    stack: list[str] = []
    for row in rows:
        if row.tokens:
            if stack:
                row.continuation = True
            for t in row.tokens:
                if t.kind not in (PUNCT,):
                    continue
                if t.text in ("(", "["):
                    stack.append(t.text)
                elif t.text in (")", "]"):
                    if stack:
                        stack.pop()
                elif t.text == "{":
                    if brace_is_group:
                        stack.append("{")
                elif t.text == "}":
                    if brace_is_group and stack and stack[-1] == "{":
                        stack.pop()


def _lex(norm: str, spec: Spec) -> tuple[list[Token], str | None]:
    """Python goes through stdlib ``tokenize``; unparsable input falls back to the clike scanner."""
    if spec.family == "python":
        from .py_lang import tokenize_python

        try:
            return tokenize_python(norm), None
        except Exception as exc:  # fragments, syntax errors, mixed tabs
            return tokenize_clike(norm, spec), f"{type(exc).__name__}: {exc}"
    return tokenize_clike(norm, spec), None


def _indent_of(text: str) -> str:
    i = 0
    while i < len(text) and text[i] in " \t":
        i += 1
    return text[:i]


_OPENS = {"{": "}", "(": ")", "[": "]"}
_CLOSES = {v: k for k, v in _OPENS.items()}


def _depth_scan(rows: list[Line], spec: Spec) -> tuple[list[tuple[int, int]], list[int]]:
    """Per-row brace depth (for clike indent) and open ``(`/``[`` count (for continuation rows)."""
    depths: list[tuple[int, int]] = []
    parens: list[int] = []
    stack: list[str] = []
    for row in rows:
        braces_before = sum(1 for ch in stack if ch == "}")
        parens_before = sum(1 for ch in stack if ch in (")", "]"))
        for t in row.tokens:
            if t.kind in (CMT, STR, PRE):
                continue
            if t.kind == PUNCT:
                if t.text in _OPENS:
                    stack.append(_OPENS[t.text])
                elif t.text in _CLOSES:
                    if stack and stack[-1] == t.text:
                        stack.pop()
        depths.append((braces_before, sum(1 for ch in stack if ch == "}")))
        parens.append(parens_before)
    return depths, parens


def brace_depth_before(doc: Document, row_num: int) -> int:
    return doc.depths[row_num - 1][0]




