"""Row-oriented view of a source file, shared by extraction and formatting."""
from __future__ import annotations

from dataclasses import dataclass, field

from .tokens import CMT, PRE, PUNCT, STR, Spec, Token, detect, tokenize_clike


@dataclass
class Line:
    num: int                     # 1-based
    raw: str                     # content without the line break
    indent: str                  # leading whitespace
    tokens: list[Token] = field(default_factory=list)
    in_trivia: bool = False      # part of a multi-line comment/string body
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

    @property
    def is_python(self) -> bool:
        return self.spec.family == "python"

    @property
    def row_count(self) -> int:
        return len(self.lines)

    def row(self, num: int) -> Line:
        return self.rows[num - 1]

    def code_rows(self) -> list[Line]:
        return [r for r in self.rows if not r.blank and not r.in_trivia and r.tokens]

    def token_stream(self) -> list[tuple[str, str]]:
        """Comparable identity of the file: whitespace-free, so formatters can be verified."""
        return [(t.kind, t.text) for t in self.tokens]

    def rows_with_token(self, tok: Token) -> range:
        return range(tok.row, tok.end_row + 1)

    def token_depths(self) -> list[tuple[int, int]]:
        """``(brace depth, paren depth)`` in front of each token, cached on first use."""
        if self._tdepths is None:
            out = []
            braces = 0
            parens = 0
            for t in self.tokens:
                out.append((braces, parens))
                if t.kind in (CMT, STR, PRE):
                    continue
                if t.kind == PUNCT:
                    if t.text == "{":
                        braces += 1
                    elif t.text == "}":
                        braces = max(0, braces - 1)
                    elif t.text in ("(", "["):
                        parens += 1
                    elif t.text in (")", "]"):
                        parens = max(0, parens - 1)
            self._tdepths = out
        return self._tdepths


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
        if t.kind == PRE:
            rows[t.row - 1].directive = True
    depths, parens = _depth_scan(rows, spec)
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


def brace_delta(doc: Document, row_num: int) -> int:
    d = doc.depths[row_num - 1]
    return d[1] - d[0]


def indent_level(doc: Document, row_num: int, unit: int | None = None) -> int:
    """Nesting level of a row: braces for clike, visual indent / unit for python."""
    row = doc.row(row_num)
    if doc.is_python:
        width = row.indent_width(unit or 4)
        return int(round(width / (unit or 4)))
    return brace_depth_before(doc, row_num)


def is_function_definition(doc: Document, row_num: int) -> bool:
    """Heuristic: a row that opens a function body (``f(args) {`` / ``def f(...):``)."""
    row = doc.row(row_num)
    toks = [t for t in row.tokens if t.kind != CMT]
    if not toks:
        return False
    if doc.is_python:
        return toks[0].is_kw("def", "async", "class")
    if toks[-1].is_punct("{"):
        return any(t.is_punct(")") for t in toks[:-1])
    return False
