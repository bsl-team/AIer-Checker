"""Reformat source so it matches a style profile, without ever changing the token stream.

Order of passes: brace moves (rows) -> intra-row spacing -> indentation -> blank control ->
render (with wrapping).  Each pass only moves or re-spaces existing tokens; the result is
re-lexed and compared to the input, and a mismatch returns the untouched original plus the
reason.  Rules that would need to insert or delete tokens (trailing commas, semicolons,
renaming) are reported but never applied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .document import Document, build_document, text_width
from .extract import ASSIGN_OPS, BIN_OPS, CONTROL_PAREN_KW, colon_context, is_clike_definition, is_pointer_op
from .i18n import t
from .profile import Profile
from .tokens import CHR, CMT, ID, KW, NUM, PRE, PUNCT, STR, Token

FORMAT_RULES = (
    "indent.type", "indent.size", "indent.continuation", "indent.namespace_body", "width.limit",
    "brace.style", "brace.close_same_row_branch", "brace.empty_block",
    "space.after_keyword", "space.around_operators", "space.around_assignment",
    "space.after_comma", "space.before_comma", "space.inside_parens", "space.inside_brackets",
    "space.inside_braces", "space.before_call_paren", "space.before_template_angle",
    "space.inside_template_angle", "space.before_semicolon",
    "space.after_semicolon", "space.around_colon_in_for", "space.around_pointer",
    "blank.max_in_block", "blank.between_functions", "blank.between_methods",
    "blank.after_open_brace", "blank.before_close_brace", "blank.file_start", "blank.file_end",
    "blank.final_newline", "quote.style", "layout.case_indent",
)

NOT_REWRITTEN = {
    "naming.*": "renaming identifiers changes the program, not its layout",
    "semicolon.required": "adding or removing ; changes parse boundaries",
    "layout.trailing_comma": "inserting a comma would change the token stream",
    "layout.wrap_operator": "moving an operator across rows is left to the author",
    "layout.single_statement_same_row": "moving statements across rows is not a layout-only edit",
    "comment.*": "comment text is never rewritten",
    "include.*": "reordering includes can change build behaviour",
    "constant.*": "adding, removing or moving const changes the program, not its layout",
    "statement.*": "turning , into ; (or back) rewrites the token stream, not the layout",
}

ACCESS_KW = {"public", "private", "protected"}

# Rules that rewrite string contents need near-uniform evidence: an author mixing ' and "
# usually picks per string (escapes, nested quotes), so a 60% preference is not a style rule.
HIGH_AGREEMENT = {
    "quote.style": 0.85,
    "blank.between_functions": 0.7,
    "blank.between_methods": 0.7,
}
BRANCH_KW = {"else", "catch", "finally"}
HEADER_KW = {"namespace", "extern", "template", "class", "struct", "enum", "union", "interface", "impl", "trait", "module"}


@dataclass
class FormatOptions:
    rules: set[str] | None = None      # limit the rewrite to these keys
    min_confidence: float = 0.5        # observed rules below this confidence are left alone
    use_defaults: bool = True          # apply family defaults for unobserved rules
    wrap: bool = True                  # split rows longer than width.limit
    eol: str | None = None


@dataclass
class FormatResult:
    text: str
    changed: bool
    applied: list[str]
    skipped: list[str]
    error: str | None = None

    def to_dict(self) -> dict:
        return {"changed": self.changed, "applied": self.applied, "skipped": self.skipped, "error": self.error}


@dataclass
class Cell:
    tok: Token
    pre: int | None = None
    rule: str | None = None


@dataclass
class Piece:
    kind: str                      # code | blank | trivia | directive
    cells: list[Cell] = field(default_factory=list)
    original_indent: str = ""
    raw: str = ""
    indent: str = ""
    level: int = 0
    in_group: bool = False
    open_after: int = 0          # brackets still open when this row ends
    def_rule: str | None = None
    case_row: bool = False
    comment_only: bool = False
    trivia_kind: str = ""
    row: int = 0
    skip: bool = False

    @property
    def first(self) -> Token | None:
        return self.cells[0].tok if self.cells else None

    @property
    def last(self) -> Token | None:
        return self.cells[-1].tok if self.cells else None

    def only(self, text: str) -> bool:
        return len(self.cells) == 1 and self.cells[0].tok.is_punct(text)


def format_source(src: str, path: str | None, profile: Profile, options: FormatOptions | None = None) -> FormatResult:
    """Format one source string with ``profile``; never returns damaged code."""
    return Formatter(build_document(src, path), profile, options or FormatOptions()).run()


def token_identity(src: str, path: str | None = None) -> list[tuple[str, str]]:
    """Whitespace-free identity of a source, safe for before/after equality checks.

    String tokens compare by value (quote style is layout) and comments by words (their
    indentation is layout), which is exactly the latitude the formatter is allowed to take.
    """
    return _normalise(build_document(src, path).token_stream())



class Formatter:
    def __init__(self, doc: Document, profile: Profile, options: FormatOptions):
        self.doc = doc
        self.profile = profile
        self.opts = options
        self.python = doc.spec.family == "python"
        self.indent_type = "tab" if profile.value("indent.type", "space") == "tab" else "space"
        self.unit = self._number("indent.size", 4)
        self.unit_str = "\t" * self.unit if self.indent_type == "tab" else " " * self.unit
        self.continuation = profile.value("indent.continuation", self.unit)
        self.eol = options.eol or doc.original_eol or "\n"
        self.changed_rules: set[str] = set()
        self.skipped: set[str] = set()
        self._levels: dict[int, int] | None = None
        self.reindent = any(
            self.enabled(k) for k in ("indent.size", "indent.type", "indent.continuation", "indent.namespace_body", "layout.case_indent")
        )
        self.apply_indent_type = self.enabled("indent.type")
        self.quote_style = self._quote_style()

    # -- rule gating ----------------------------------------------------
    def enabled(self, key: str) -> bool:
        if key not in FORMAT_RULES:
            return False
        if self.opts.rules is not None and key not in self.opts.rules:
            return False
        feat = self.profile.feature(key)
        if feat is not None and feat.total > 0:
            if feat.confidence < self.opts.min_confidence:
                self.skipped.add(key)
                return False
            need = HIGH_AGREEMENT.get(key)
            if need is not None and feat.agreement < need:
                self.skipped.add(key)
                return False
            return True
        if self.opts.use_defaults:
            return True
        self.skipped.add(key)
        return False

    def value(self, key: str, default):
        return self.profile.value(key, default)

    def flag(self, key: str) -> None:
        self.changed_rules.add(key)

    def _number(self, key: str, default: int) -> int:
        try:
            n = int(self.value(key, default))
        except (TypeError, ValueError):
            return default
        return n if 1 <= n <= 24 else default

    def _quote_style(self) -> str | None:
        """Quote conversion needs a real observation: rewriting literals is not pure layout.

        An empty profile would otherwise rewrite every ``'...'`` into ``"..."`` by family
        default, and the author's per-string choice (nested quotes, escapes) is not layout.
        """
        if not (self.doc.spec.single_quote_strings and self.doc.spec.double_quote_strings or self.python):
            return None
        feat = self.profile.feature("quote.style")
        if feat is None or not feat.total or not self.enabled("quote.style"):
            return None
        val = feat.value
        return val if val in ("single", "double") else None

    # -- driver ---------------------------------------------------------
    def run(self) -> FormatResult:
        original = self.doc.src.replace("\n", self.eol)
        if not self.doc.tokens:
            return FormatResult(text=original, changed=False, applied=[], skipped=sorted(self.skipped))
        pieces = self._pieces()
        pieces = self._pass_braces(pieces)
        self._pass_spacing(pieces)
        self._pass_indent(pieces)
        pieces = self._pass_blanks(pieces)
        text = self._assemble(self._render(pieces))
        before = _normalise(self.doc.token_stream())
        again = build_document(text, self.doc.path, self.doc.spec)
        if again.parse_error and not self.doc.parse_error:
            return FormatResult(
                text=original,
                changed=False,
                applied=sorted(self.changed_rules),
                skipped=sorted(self.skipped),
                error=t("f.no_lex", error=again.parse_error),
            )
        after = _normalise(again.token_stream())
        if before != after:
            return FormatResult(
                text=original,
                changed=False,
                applied=sorted(self.changed_rules),
                skipped=sorted(self.skipped),
                error=_describe_divergence(before, after),
            )
        return FormatResult(text=text, changed=text != original, applied=sorted(self.changed_rules), skipped=sorted(self.skipped))

    def _pieces(self) -> list[Piece]:
        out: list[Piece] = []
        cover = self._trivia_kinds()
        for row in self.doc.rows:
            if row.in_trivia:
                # blank-looking rows inside a docstring are string content, not layout
                out.append(Piece(kind="trivia", original_indent=row.indent, raw=row.raw, trivia_kind=cover.get(row.num, ""), row=row.num))
            elif row.blank:
                out.append(Piece(kind="blank", original_indent=row.indent, raw=row.raw, row=row.num))
            elif row.directive:
                out.append(Piece(kind="directive", cells=[Cell(t) for t in row.tokens], original_indent=row.indent, raw=row.raw, row=row.num))
            else:
                piece = Piece(kind="code", cells=[Cell(t) for t in row.tokens], original_indent=row.indent, raw=row.raw, row=row.num)
                piece.comment_only = len(piece.cells) == 1 and piece.cells[0].tok.kind == CMT
                piece.case_row = bool(piece.first) and piece.first.kind == KW and piece.first.text in ("case", "default")
                out.append(piece)
        return out

    def _trivia_kinds(self) -> dict[int, str]:
        """Row -> token kind that covers it, for rows inside a multi-line comment or string."""
        cover: dict[int, str] = {}
        for tok in self.doc.tokens:
            if tok.end_row > tok.row:
                for row in range(tok.row + 1, tok.end_row + 1):
                    cover[row] = tok.kind
        return cover

    def _neighbour(self, pieces: list[Piece], i: int, step: int) -> Piece | None:
        j = i + step
        while 0 <= j < len(pieces):
            if pieces[j].kind in ("code", "directive"):
                return pieces[j]
            if pieces[j].kind == "trivia":
                return None
            j += step
        return None

    # -- pass 1: brace placement ---------------------------------------
    def _pass_braces(self, pieces: list[Piece]) -> list[Piece]:
        if self.python:
            return pieces
        if self.enabled("brace.empty_block") and self.value("brace.empty_block", "compact") == "compact":
            pieces = self._compact_empty_blocks(pieces)
        if self.enabled("brace.close_same_row_branch"):
            pieces = self._branch_placement(pieces, bool(self.value("brace.close_same_row_branch", True)))
        if self.enabled("brace.style"):
            style = self.value("brace.style", "attach")
            if style == "attach":
                pieces = self._attach_braces(pieces)
            elif style == "next":
                pieces = self._detach_braces(pieces)
        return [p for p in pieces if p.kind != "code" or p.cells]

    def _compact_empty_blocks(self, pieces: list[Piece]) -> list[Piece]:
        out: list[Piece] = []
        i = 0
        while i < len(pieces):
            piece = pieces[i]
            nxt = self._neighbour(pieces, i, 1)
            if piece.kind == "code" and piece.only("{") and nxt is not None and nxt.only("}"):
                merged = Piece(kind="code", cells=list(piece.cells), original_indent=piece.original_indent, raw=piece.raw)
                merged.cells.append(Cell(nxt.cells[0].tok, pre=0, rule="brace.empty_block"))
                out.append(merged)
                i += 1
                while i < len(pieces) and pieces[i] is not nxt:
                    i += 1
                i += 1
                self.flag("brace.empty_block")
                continue
            out.append(piece)
            i += 1
        return out

    def _branch_placement(self, pieces: list[Piece], same_row: bool) -> list[Piece]:
        out: list[Piece] = []
        i = 0
        while i < len(pieces):
            piece = pieces[i]
            nxt = self._neighbour(pieces, i, 1)
            if same_row and piece.kind == "code" and piece.last is not None and not piece.last.kind == CMT and piece.cells and piece.cells[0].tok.is_punct("}"):
                if (
                    nxt is not None
                    and nxt.cells
                    and nxt.first.kind == KW
                    and nxt.first.text in BRANCH_KW
                    and not _is_comment_start(nxt)
                    and not _is_comment_end(piece)
                ):
                    moved = nxt.cells.pop(0)
                    moved.pre = 1
                    moved.rule = "brace.close_same_row_branch"
                    piece.cells.append(moved)
                    self.flag("brace.close_same_row_branch")
                    out.append(piece)
                    i += 1
                    while i < len(pieces) and pieces[i] is not nxt:
                        out.append(pieces[i])
                        i += 1
                    if nxt.cells:
                        out.append(nxt)
                    i += 1
                    continue
            if not same_row and piece.kind == "code" and len(piece.cells) >= 3 and piece.cells[0].tok.is_punct("}") and piece.cells[1].tok.kind == KW and piece.cells[1].tok.text in BRANCH_KW:
                head = Piece(kind="code", cells=piece.cells[:1], original_indent=piece.original_indent, raw="}")
                tail = piece.cells[1:]
                for c in tail:
                    c.rule = "brace.close_same_row_branch"
                tail[0].pre = 0
                rest = Piece(kind="code", cells=tail, original_indent=piece.original_indent, raw=piece.raw)
                rest.comment_only = False
                out.append(head)
                out.append(rest)
                self.flag("brace.close_same_row_branch")
                i += 1
                continue
            out.append(piece)
            i += 1
        return out

    def _attach_braces(self, pieces: list[Piece]) -> list[Piece]:
        out: list[Piece] = []
        i = 0
        while i < len(pieces):
            piece = pieces[i]
            if piece.kind == "code" and piece.only("{") and out and out[-1].kind == "code":
                prev = out[-1]
                if _brace_target(prev):
                    prev.cells.append(Cell(piece.cells[0].tok, pre=1, rule="brace.style"))
                    self.flag("brace.style")
                    i += 1
                    continue
            out.append(piece)
            i += 1
        return out

    def _detach_braces(self, pieces: list[Piece]) -> list[Piece]:
        out: list[Piece] = []
        for piece in pieces:
            out.append(piece)
            if piece.kind != "code" or len(piece.cells) < 2:
                continue
            if _is_comment_end(piece):
                continue
            if not piece.cells[-1].tok.is_punct("{"):
                continue
            if not _brace_target_cells(piece.cells[:-1]):
                continue
            moved = piece.cells.pop()
            moved.pre = 0
            moved.rule = "brace.style"
            out.append(Piece(kind="code", cells=[moved], original_indent=piece.original_indent, raw="{"))
            self.flag("brace.style")
        return out

    # -- pass 2: spacing -----------------------------------------------
    def _pass_spacing(self, pieces: list[Piece]) -> None:
        depths = self.doc.token_depths()
        for piece in pieces:
            if piece.kind != "code":
                continue
            depth = _Depths()
            if piece.cells:
                # seed from the global bracket state: a wrapped argument list has no opening
                # bracket on this row, and would otherwise look like a plain assignment
                depth.braces, depth.parens, depth.brackets = depths[piece.cells[0].tok.index]
            for idx, cell in enumerate(piece.cells):
                tok = cell.tok
                if idx > 0:
                    gap, rule = self._gap(piece.cells, idx, depth)
                    cell.pre = gap
                    cell.rule = rule
                    original = tok.gap if tok.gap >= 0 else None
                    if rule and (original is None or original != gap):
                        self.flag(rule)
                depth.feed(tok)

    def _gap(self, cells: list[Cell], idx: int, depth: _Depths) -> tuple[int, str | None]:
        tok = cells[idx].tok
        prev = cells[idx - 1].tok
        if tok.kind == CMT:
            return (max(1, tok.gap), None)
        if prev.kind == CMT:
            return (max(1, tok.gap), None)
        if prev.kind == KW and prev.text in CONTROL_PAREN_KW and tok.is_punct("("):
            return self._spaced("space.after_keyword", False if self.python else True)
        if tok.is_punct(","):
            return self._spaced("space.before_comma", False)
        if prev.is_punct(",") and not tok.is_punct(")", "]", "}"):
            return self._spaced("space.after_comma", True)
        if prev.is_punct("(") and not tok.is_punct(")"):
            return self._spaced("space.inside_parens", False)
        if tok.is_punct(")") and not prev.is_punct("("):
            return self._spaced("space.inside_parens", False)
        if prev.is_punct("[") and not tok.is_punct("]"):
            return self._spaced("space.inside_brackets", False)
        if tok.is_punct("]") and not prev.is_punct("["):
            return self._spaced("space.inside_brackets", False)
        if prev.is_punct("{") and depth.in_brace_body and not tok.is_punct("}"):
            return self._spaced("space.inside_braces", False)
        if tok.is_punct("}") and not prev.is_punct("{") and depth.in_brace_body:
            return self._spaced("space.inside_braces", False)
        if tok.is_punct(";") and depth.parens > 0:
            return self._spaced("space.before_semicolon", False)
        if prev.is_punct(";") and depth.parens > 0:
            return self._spaced("space.after_semicolon", True)
        if prev.kind == ID and tok.is_punct("("):
            return self._spaced("space.before_call_paren", False)
        if self.doc.is_template_angle(tok.index):
            if tok.is_punct("<"):
                return self._spaced("space.before_template_angle", False)  # vector<int> vs vector <int>
            return self._spaced("space.inside_template_angle", False)  # the closing > / >>
        if prev.is_punct("<") and self.doc.is_template_angle(prev.index):
            return self._spaced("space.inside_template_angle", False)  # token right after <
        pointer = self._pointer_gap(cells, idx)
        if pointer is not None:
            return pointer
        if tok.is_punct("**") or prev.is_punct("**"):
            return (None, None)  # exponent / **kwargs / **ptr: spacing belongs to the operands
        operator = self._operator_gap(cells, idx, depth)
        if operator is not None:
            return operator
        row_control = cells[0].tok.kind == KW and cells[0].tok.text in CONTROL_PAREN_KW
        colon_ok = colon_context(depth.braces, depth.parens, depth.brackets, row_control)
        if tok.is_punct(":") and not prev.is_punct(":", "?") and colon_ok and not self._is_label_colon(cells, idx):
            return self._colon_gap(before=True)
        if prev.is_punct(":") and not tok.is_punct(":") and colon_ok and not self._is_label_colon(cells, idx - 1):
            return self._colon_gap(before=False)
        if tok.is_punct(".", "->", "::") or prev.is_punct(".", "->", "::"):
            return (0, None)
        if tok.is_punct("...") or prev.is_punct("..."):
            return (0, None)
        if tok.gap < 0:
            return (1, None)  # rows brought together by the brace pass
        return (max(0, tok.gap), None)

    def _spaced(self, key: str, default: bool) -> tuple[int, str | None]:
        if not self.enabled(key):
            return (None, None)
        return (1 if bool(self.value(key, default)) else 0, key)

    COLON_GAPS = {
        "spaced": (1, 1),
        "after": (0, 1),
        "before": (1, 0),
        "tight": (0, 0),
        "true": (1, 1),  # profiles written before colon spacing became a four-way value
        "false": (0, 0),
    }

    def _colon_gap(self, before: bool) -> tuple[int, str | None]:
        key = "space.around_colon_in_for"
        if not self.enabled(key):
            return (None, None)
        default = "after" if self.python else "spaced"
        style = str(self.value(key, default))
        gaps = self.COLON_GAPS.get(style, (1, 1))
        return (gaps[0] if before else gaps[1], key)

    def _operator_gap(self, cells: list[Cell], idx: int, depth: _Depths) -> tuple[int, str | None] | None:
        tok = cells[idx].tok
        prev = cells[idx - 1].tok
        for candidate, is_current in ((tok, True), (prev, False)):
            if candidate.kind != PUNCT or candidate.text not in (BIN_OPS | ASSIGN_OPS):
                continue
            if candidate.text in ("<", ">") and not _is_comparison(cells, idx if is_current else idx - 1):
                continue
            key = "space.around_assignment" if candidate.text in ASSIGN_OPS else "space.around_operators"
            if candidate.text in ASSIGN_OPS and (depth.parens > 0 or depth.brackets > 0):
                continue
            j = idx if is_current else idx - 1
            if not _op_binary(cells, j):
                continue
            return self._spaced(key, True)
        return None

    def _pointer_gap(self, cells: list[Cell], idx: int) -> tuple[int, str | None] | None:
        if self.python or not self.enabled("space.around_pointer"):
            return None
        style = self.value("space.around_pointer", None)
        if style not in ("type", "name", "spaced"):
            return None
        tok, prev = cells[idx].tok, cells[idx - 1].tok
        if _is_pointer_op(cells, idx):
            if style == "type":
                return (0, "space.around_pointer")
            if style == "name":
                return (1, "space.around_pointer")
            return (1, "space.around_pointer")
        if _is_pointer_op(cells, idx - 1):
            if style == "type":
                return (1, "space.around_pointer")
            if style == "name":
                return (0, "space.around_pointer")
            return (1, "space.around_pointer")
        return None

    def _is_label_colon(self, cells: list[Cell], j: int) -> bool:
        """``case 1:`` / ``default:`` / ``goto:``, not a dict, slice or ternary colon."""
        head = cells[0].tok
        if head.kind == KW and head.text in ("case", "default"):
            return True
        return j == len(cells) - 1

    # -- pass 3: indentation -------------------------------------------
    def _pass_indent(self, pieces: list[Piece]) -> None:
        bstack: list[str] = []
        pstack: list[_Group] = []
        case_level: int | None = None
        indented_cases = self.enabled("layout.case_indent") and self.value("layout.case_indent", "flush") == "indented"
        levels = self._python_levels()
        for piece in pieces:
            if piece.kind == "blank":
                continue
            piece.in_group = bool(pstack)
            opener = pstack[-1] if pstack else None
            if piece.kind == "trivia":
                piece.indent = piece.original_indent
                continue
            ns_skip = self.enabled("indent.namespace_body") and self.value("indent.namespace_body", "indent") == "none"
            count = sum(1 for kind in bstack if not (ns_skip and kind == "ns"))
            if piece.kind == "directive" or self._is_preserved(piece):
                piece.indent = piece.original_indent
            elif piece.comment_only and opener is None:
                lvl = max(0, levels.get(piece.first.row, 0)) if self.python else count
                piece.indent = self.unit_str * lvl
                piece.level = lvl
            elif opener is not None and piece.first is not None and _closes(opener, piece.first):
                piece.indent = self._closer_indent(opener, piece)
            elif opener is not None:
                piece.indent = self._continuation_or_keep(opener, piece)
            elif self.python:
                lvl = max(0, levels.get(piece.first.row if piece.first else 1, 0))
                piece.indent = self.unit_str * lvl
                piece.level = lvl
            elif piece.first is not None and piece.first.is_punct("}"):
                lvl = sum(1 for kind in bstack[:-1] if not (ns_skip and kind == "ns")) if bstack else 0
                piece.indent = self.unit_str * lvl
                piece.level = lvl
            elif piece.case_row:
                case_level = count
                piece.indent = self.unit_str * max(0, count - (0 if indented_cases else 1))
                piece.level = count
            else:
                extra = 1 if (indented_cases and case_level == count) else 0
                piece.indent = self.unit_str * (count + extra)
                piece.level = count + extra
            self._mark_def(piece, piece.level)
            braces_before = len(bstack)
            for cidx, cell in enumerate(piece.cells):
                _feed(cell.tok, bstack, pstack, piece, cidx, _is_namespace_header(piece), self.python)
            piece.open_after = len(pstack)
            if len(bstack) < braces_before and not bstack:
                case_level = None
            if not self.reindent:
                piece.indent = piece.original_indent
            elif piece.kind == "code" and piece.indent != piece.original_indent:
                if piece.in_group:
                    self.flag("indent.continuation")
                elif self.apply_indent_type and _indent_kind(piece.indent) != _indent_kind(piece.original_indent):
                    self.flag("indent.type")
                else:
                    self.flag("indent.size")
        self._plan_trivia(pieces)

    def _is_preserved(self, piece: Piece) -> bool:
        """Rows whose own indentation carries meaning we cannot recompute: access labels and goto labels."""
        if len(piece.cells) < 2 or piece.kind != "code":
            return False
        head = piece.cells[0].tok
        second = piece.cells[1].tok
        if head.kind == KW and head.text in ACCESS_KW and second.is_punct(":"):
            return True
        if head.kind == ID and second.is_punct(":") and len(piece.cells) == 2:
            return True
        return False

    def _closer_indent(self, opener: "_Group", piece: Piece) -> str:
        """A bracket closer the author aligned under its opener stays aligned."""
        cur = text_width(piece.original_indent, 8)
        if _prefer_aligned(cur, opener.aligned, opener.indent_width):
            return " " * opener.aligned
        return opener.indent_str

    def _continuation_or_keep(self, opener: "_Group", piece: Piece) -> str:
        """Give a wrapped row whichever style it was written in: hung alignment or a fixed step.

        Re-spacing the opening row shifts the alignment target by a character or two.  Treating
        that as "no longer aligned" would flip the row to a stepped indent and never settle, so
        whichever candidate the row sits closer to wins, and an aligned row is repaired to the
        exact column.  When both candidates land on the same column the stepped form wins: it is
        built from the profile's indent type, so a tab profile keeps tabs instead of being
        rewritten to the same width in spaces (which would not be a fixed point).
        """
        if not self.enabled("indent.continuation"):
            return piece.original_indent
        wanted = self._continuation_indent(opener)
        cur = text_width(piece.original_indent, 8)
        step_w = text_width(wanted, 8)
        if opener.aligned != step_w and _prefer_aligned(cur, opener.aligned, step_w):
            return " " * opener.aligned
        return wanted

    def _continuation_indent(self, opener: "_Group") -> str:
        if not self.enabled("indent.continuation"):
            return opener.indent_str + self.unit_str
        if self.continuation == "aligned":
            return " " * opener.aligned
        try:
            delta = int(self.continuation)
        except (TypeError, ValueError):
            delta = self.unit
        delta = max(1, delta)
        if self.indent_type == "tab":
            return opener.indent_str + "\t" * delta
        return opener.indent_str + " " * delta

    def _python_levels(self) -> dict[int, int]:
        if self._levels is None:
            if not self.python:
                self._levels = {}
            else:
                from .py_lang import python_levels

                self._levels = python_levels(self.doc.src)
        return self._levels

    def _mark_def(self, piece: Piece, level: int) -> None:
        if piece.kind != "code" or not piece.cells:
            return
        first = piece.cells[0].tok
        if self.python:
            if first.kind == KW and first.text in ("def", "async", "class"):
                piece.def_rule = "blank.between_functions" if level == 0 else "blank.between_methods"
            return
        if level != 0:
            return
        if is_clike_definition([c.tok for c in piece.cells]):
            piece.def_rule = "blank.between_functions"

    def _plan_trivia(self, pieces: list[Piece]) -> None:
        """Decide how multi-line tokens are printed.

        When the spanning token is the last cell of its row, the body rows are printed from the
        piece list (comment bodies shifted to follow the new indent).  Otherwise the token's full
        text is printed inline and its body rows are suppressed, so nothing is duplicated.
        """
        by_row = {p.row: p for p in pieces}
        code_by_row = {p.row: p for p in pieces if p.kind in ("code", "directive")}
        for tok in self.doc.tokens:
            if tok.end_row <= tok.row:
                continue
            owner = code_by_row.get(tok.row)
            if owner is None:
                continue
            rows = range(tok.row + 1, tok.end_row + 1)
            last_cell = owner.cells[-1].tok if owner.cells else None
            owns_span = last_cell is not None and last_cell.index == tok.index
            delta = text_width(owner.indent, 8) - text_width(owner.original_indent, 8)
            for r in rows:
                piece = by_row.get(r)
                if piece is None or piece.kind != "trivia":
                    continue
                if owns_span:
                    if tok.kind == CMT and delta:
                        piece.raw = _shift_text(piece.raw, delta)
                else:
                    piece.skip = True

    # -- pass 4: blank lines -------------------------------------------
    def _pass_blanks(self, pieces: list[Piece]) -> list[Piece]:
        out: list[Piece] = []
        i = 0
        while i < len(pieces):
            piece = pieces[i]
            if piece.kind != "blank":
                out.append(piece)
                i += 1
                continue
            start = i
            while i < len(pieces) and pieces[i].kind == "blank":
                i += 1
            run = i - start
            prev = out[-1] if out else None
            nxt = pieces[i] if i < len(pieces) else None
            cap = self._allowed_blanks(prev, nxt)
            keep = min(run, cap)
            if prev is None:
                keep = 1 if self.enabled("blank.file_start") and bool(self.value("blank.file_start", False)) else 0
            if keep != run:
                self.flag(_blank_rule(prev, nxt))
            out.extend(Piece(kind="blank") for _ in range(keep))
        return self._definition_gaps(out)

    def _allowed_blanks(self, prev: Piece | None, nxt: Piece | None) -> int:
        if prev is None:
            return 0
        if self.enabled("blank.after_open_brace") and not bool(self.value("blank.after_open_brace", False)):
            if prev.kind == "code" and prev.last is not None and prev.last.is_punct("{"):
                return 0
        if self.enabled("blank.before_close_brace") and not bool(self.value("blank.before_close_brace", False)):
            if nxt is not None and nxt.kind == "code" and nxt.first is not None and nxt.first.is_punct("}"):
                return 0
        cap = self._number("blank.max_in_block", 1) if self.enabled("blank.max_in_block") else None
        if cap is None:
            return 10 ** 6
        inside = (nxt.level if nxt is not None else prev.level) > 0 or prev.open_after > 0
        return cap if inside else max(cap, 2)

    def _definition_gaps(self, pieces: list[Piece]) -> list[Piece]:
        out: list[Piece] = []
        for piece in pieces:
            if piece.kind == "code" and piece.def_rule and self.enabled(piece.def_rule):
                want = int(self.value(piece.def_rule, 1) or 0)
                prior = 0
                while prior < len(out) and out[len(out) - 1 - prior].kind == "blank":
                    prior += 1
                anchor_idx = len(out) - 1 - prior
                anchor = out[anchor_idx] if anchor_idx >= 0 else None
                glued_comment = anchor is not None and anchor.kind == "code" and (anchor.comment_only or _is_comment_start(piece))
                opens_block = anchor is not None and anchor.last is not None and anchor.last.is_punct("{", ":")
                decorated = anchor is not None and anchor.first is not None and anchor.first.is_punct("@")
                if (
                    anchor is not None
                    and anchor.kind == "code"
                    and anchor.open_after == 0
                    and not glued_comment
                    and not opens_block
                    and not decorated
                ):
                    if prior < want:
                        out.extend(Piece(kind="blank") for _ in range(want - prior))
                        self.flag(piece.def_rule)
                    elif prior > want:
                        del out[len(out) - (prior - want):]
                        self.flag(piece.def_rule)
            out.append(piece)
        return out

    # -- render ---------------------------------------------------------
    def _render(self, pieces: list[Piece]) -> list[str]:
        limit = self._number("width.limit", 100) if self.enabled("width.limit") else None
        lines: list[str] = []
        for piece in pieces:
            if piece.skip:
                continue
            if piece.kind == "blank":
                lines.append("")
            elif piece.kind == "trivia":
                lines.append(piece.raw)
            elif piece.kind == "directive":
                lines.append(piece.original_indent + piece.raw.lstrip(" \t"))
            else:
                if not piece.cells:
                    continue
                line = (piece.indent + self._body(piece.cells)).rstrip()
                if limit and not piece.in_group and text_width(line, 8) > limit and self.opts.wrap:
                    wrapped = self._wrap(piece, limit)
                    if wrapped:
                        lines.extend(wrapped)
                        continue
                lines.append(line)
        return lines

    def _body(self, cells: list[Cell]) -> str:
        parts: list[str] = []
        for idx, cell in enumerate(cells):
            text = self._token_text(cell.tok)
            if idx == len(cells) - 1 and cell.tok.end_row > cell.tok.row:
                text = text.split("\n", 1)[0]  # the rest is printed by the trivia pieces
            if idx == 0:
                parts.append(text)
                continue
            pre = cell.pre if cell.pre is not None else (cell.tok.gap if cell.tok.gap >= 0 else 1)
            parts.append(" " * max(0, int(pre)) + text)
        return "".join(parts)

    def _token_text(self, tok: Token) -> str:
        if tok.kind == STR and self.quote_style and not tok.multiline:
            converted = _convert_quotes(tok, self.quote_style, self.doc.spec)
            if converted is not None:
                self.flag("quote.style")
                return converted
        return tok.text

    def _wrap(self, piece: Piece, limit: int) -> list[str] | None:
        """Split an over-long row across the comma boundaries of one bracket group."""
        cells = piece.cells
        best = None
        for idx, cell in enumerate(cells):
            if not cell.tok.is_punct("(", "["):
                continue
            close = _match_index(cells, idx)
            if close is None:
                continue
            commas = _top_level_commas(cells, idx, close)
            if commas and (best is None or len(commas) > len(best[2])):
                best = (idx, close, commas)
        if best is None:
            return None
        open_i, close_i, commas = best
        head = cells[: open_i + 1]
        chunks: list[list[Cell]] = []
        start = open_i + 1
        for cut in commas:
            chunks.append(cells[start : cut + 1])  # the comma stays with its element
            start = cut + 1
        if start < close_i:
            chunks.append(cells[start:close_i])
        tail = cells[close_i:]
        cont = self._wrap_continuation(piece, open_i)
        lines = [(piece.indent + self._body(head)).rstrip()]
        for chunk in chunks:
            lines.append((cont + self._body(chunk)).rstrip())
            self.flag("width.limit")
        lines.append((piece.indent + self._body(tail)).rstrip())
        return lines

    def _wrap_continuation(self, piece: Piece, open_i: int) -> str:
        if not self.enabled("indent.continuation"):
            return piece.indent + self.unit_str
        if self.continuation == "aligned":
            prefix = self._body(piece.cells[: open_i + 1])
            return " " * text_width(piece.indent + prefix, 8)
        try:
            delta = int(self.continuation)
        except (TypeError, ValueError):
            delta = self.unit
        delta = max(1, delta)
        return piece.indent + ("\t" * delta if self.indent_type == "tab" else " " * delta)

    # -- assemble -------------------------------------------------------
    def _assemble(self, lines: list[str]) -> str:
        eol = self.eol
        body = list(lines)
        trailing = 0
        while body and body[-1] == "":
            body.pop()
            trailing += 1
        if self.enabled("blank.file_end"):
            keep = max(0, min(int(self.value("blank.file_end", 0) or 0), 2))
        else:
            keep = min(trailing, 1)
        body.extend("" for _ in range(keep))
        text = eol.join(line.replace("\n", eol) for line in body)
        if self.enabled("blank.final_newline"):
            needs = bool(self.value("blank.final_newline", True))
        else:
            needs = self.doc.src.endswith("\n")
        return text + eol if needs else text


def _is_comment_start(piece: Piece) -> bool:
    return bool(piece.cells) and piece.cells[0].tok.kind == CMT


def _is_comment_end(piece: Piece) -> bool:
    return bool(piece.cells) and piece.cells[-1].tok.kind == CMT


def _brace_target(piece: Piece) -> bool:
    return bool(piece.cells) and _brace_target_cells(piece.cells)


def _brace_target_cells(cells: list[Cell]) -> bool:
    """Is this row a block header whose ``{`` should sit with it (attach) or alone (next)?"""
    if not cells:
        return False
    last = cells[-1].tok
    if last.kind == CMT or last.is_punct("{"):
        return False
    if any(c.tok.is_punct(")") for c in cells):
        return True
    head = cells[0].tok
    if last.kind == KW and last.text in (BRANCH_KW | CONTROL_PAREN_KW | HEADER_KW):
        return True
    return head.kind == KW and head.text in (CONTROL_PAREN_KW | HEADER_KW)


def _indent_kind(text: str) -> str:
    has_tab = "\t" in text
    has_space = " " in text
    if has_tab and has_space:
        return "mixed"
    return "tab" if has_tab else "space"


def _shift_text(text: str, delta: int) -> str:
    """Move a comment body row sideways by ``delta`` columns (never past its own indent)."""
    if delta > 0:
        return " " * delta + text
    if delta < 0:
        limit = -delta
        i = 0
        while i < limit and i < len(text) and text[i] in " \t":
            i += 1
        return text[i:]
    return text


class _Depths:
    """Bracket state in front of the current token, plus the two context flags rules need."""

    def __init__(self):
        self.braces = 0
        self.parens = 0
        self.brackets = 0
        self.init_brace = False

    def feed(self, tok: Token) -> None:
        if tok.kind != PUNCT:
            return
        if tok.text == "{":
            self.braces += 1
        elif tok.text == "}":
            self.braces = max(0, self.braces - 1)
        elif tok.text == "(":
            self.parens += 1
        elif tok.text == ")":
            self.parens = max(0, self.parens - 1)
        elif tok.text == "[":
            self.brackets += 1
        elif tok.text == "]":
            self.brackets = max(0, self.brackets - 1)

    @property
    def in_brace_body(self) -> bool:
        """Inside a ``{...}`` literal, where ``{ x }`` spacing is a real style choice."""
        return self.braces > 0


class _Group:
    __slots__ = ("tok", "indent_str", "indent_width", "col", "aligned")

    def __init__(self, tok: Token, indent_str: str, col: int, aligned: int):
        self.tok = tok
        self.indent_str = indent_str
        self.indent_width = text_width(indent_str, 8)
        self.col = col
        self.aligned = aligned  # column a hanging element of this group sits at


def _is_namespace_header(piece: Piece) -> bool:
    head = piece.first
    return piece.kind == "code" and head is not None and head.kind == KW and head.text in ("namespace", "module", "extern")


def _group_for(tok: Token, piece: Piece, idx: int) -> _Group:
    """Record an opening bracket together with the column its elements hang at.

    The column is measured on the rendered row (after intra-row spacing), so a re-spaced opening
    row moves the target with it and a second pass finds the rows already where they belong.
    """
    nxt = piece.cells[idx + 1].tok if idx + 1 < len(piece.cells) else None
    if nxt is not None and nxt.row == tok.row:
        aligned = _column_after(nxt, piece)
    else:
        aligned = _column_after(tok, piece) + 1
    return _Group(tok, piece.indent, _column_after(tok, piece), aligned)


def _feed(tok: Token, bstack: list[str], pstack: list[_Group], piece: Piece, idx: int, ns: bool, brace_is_group: bool = False) -> None:
    """Track braces for block indentation and open groups for continuation rows.

    ``{`` means a block in clike but a dict/set literal in python: with ``brace_is_group`` the
    literal's rows are treated as continuations of the opening row, which is what keeps a
    multi-line dict from being re-indented as if it were a function body.
    """
    if tok.kind in (CMT, STR, CHR, PRE):
        return
    if tok.is_punct("{"):
        bstack.append("ns" if ns else "other")
        if brace_is_group:
            pstack.append(_group_for(tok, piece, idx))
    elif tok.is_punct("}"):
        if bstack:
            bstack.pop()
        if brace_is_group and pstack and pstack[-1].tok.is_punct("{"):
            pstack.pop()
    elif tok.is_punct("(", "["):
        pstack.append(_group_for(tok, piece, idx))
    elif tok.is_punct(")", "]"):
        if pstack:
            pstack.pop()


def _closes(group: "_Group", tok: Token) -> bool:
    """Does ``tok`` close the bracket that ``group`` records?"""
    return (
        tok.is_punct(")") and group.tok.is_punct("(")
        or tok.is_punct("]") and group.tok.is_punct("[")
        or tok.is_punct("}") and group.tok.is_punct("{")
    )


def _column_after(tok: Token, piece: Piece) -> int:
    """Rendered column of ``tok`` (0-based), counted the same way :meth:`Formatter._body` writes it."""
    width = text_width(piece.indent, 8)
    for idx, cell in enumerate(piece.cells):
        if cell.tok.index == tok.index:
            return width
        pre = 0 if idx == 0 else (cell.pre if cell.pre is not None else max(0, cell.tok.gap))
        width += pre + len(cell.tok.text)
    return width


def _prefer_aligned(cur: int, aligned: int, stepped: int) -> bool:
    """Does the row sit closer to the hung-alignment column than to the stepped indent?"""
    return aligned == stepped or abs(cur - aligned) < abs(cur - stepped)


def _is_pointer_op(cells: list[Cell], idx: int) -> bool:
    pr = cells[idx - 1].tok if idx >= 1 else None
    tok = cells[idx].tok if 0 <= idx < len(cells) else None
    nx = cells[idx + 1].tok if idx + 1 < len(cells) else None
    ppr = cells[idx - 2].tok if idx >= 2 else None
    if ppr is not None and tok is not None and ppr.row != tok.row:
        ppr = None
    return is_pointer_op(ppr, pr, tok, nx)


def _op_binary(cells: list[Cell], idx: int) -> bool:
    pr = cells[idx - 1].tok if idx >= 1 else None
    if pr is None:
        return False
    if pr.kind in (ID, NUM, STR, CHR):
        return True
    if pr.kind == PUNCT and pr.text in (")", "]"):
        return True
    if pr.kind == KW and pr.text in ("this", "true", "false", "nullptr", "NULL"):
        return True
    return False


def _is_comparison(cells: list[Cell], idx: int) -> bool:
    tok = cells[idx].tok
    if tok.text not in ("<", ">"):
        return False
    if tok.gap == 0 and idx + 1 < len(cells) and cells[idx + 1].tok.gap == 0:
        return False  # tight angles belong to a template
    return _op_binary(cells, idx)


def _match_index(cells: list[Cell], open_i: int) -> int | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    opener = cells[open_i].tok.text
    want = pairs.get(opener)
    if want is None:
        return None
    depth = 0
    for j in range(open_i, len(cells)):
        t = cells[j].tok
        if t.kind != PUNCT:
            continue
        if t.text == opener:
            depth += 1
        elif t.text == want:
            depth -= 1
            if depth == 0:
                return j
    return None


def _top_level_commas(cells: list[Cell], open_i: int, close_i: int) -> list[int]:
    out: list[int] = []
    depth = 0
    for j in range(open_i + 1, close_i):
        t = cells[j].tok
        if t.kind != PUNCT:
            continue
        if t.text in ("(", "[", "{"):
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
        elif t.text == "," and depth == 0:
            out.append(j)
    return out


_STRING_RE = re.compile("^([A-Za-z]*)(.)(.*)\\2$", re.S)  # group 2 is the quote character
_QUOTES = ("'", '"', "`")


def _convert_quotes(tok: Token, style: str, spec) -> str | None:
    """Swap quotes only when the content is untouched: no escapes, no target quote inside.

    Never converts away from a backtick template (``${}`` would stop interpolating) or a raw
    string, and never into a form the language has no notion of.
    """
    m = _STRING_RE.match(tok.text)
    if not m:
        return None
    prefix, quote, body = m.group(1), m.group(2), m.group(3)
    if quote not in _QUOTES:
        return None
    if "\\" in body or "${" in body:
        return None
    if quote == "`" or style == "backtick":
        return None
    lowered = prefix.lower()
    if "r" in lowered or "b" in lowered:
        return None
    target = {"single": "'", "double": '"'}.get(style)
    if target is None or quote == target or not _spec_allows(spec, target) or target in body:
        return None
    return f"{prefix}{target}{body}{target}"


def _spec_allows(spec, quote: str) -> bool:
    if quote == '"':
        return spec.double_quote_strings
    if quote == "'":
        return spec.single_quote_strings or spec.char_literals
    return spec.backtick_strings


def _blank_rule(prev: Piece | None, nxt: Piece | None) -> str:
    if prev is None:
        return "blank.file_start"
    if nxt is not None and nxt.first is not None and nxt.first.is_punct("}"):
        return "blank.before_close_brace"
    if prev.last is not None and prev.last.is_punct("{"):
        return "blank.after_open_brace"
    if nxt is not None and nxt.level > 0:
        return "blank.max_in_block"
    return "blank.between_functions"


def _normalise(stream: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Strings compare by value (quotes are layout); comments by words (their indent is layout)."""
    out = []
    for kind, text in stream:
        if kind == STR:
            out.append((kind, _string_value(text)))
        elif kind == CMT:
            out.append((kind, " ".join(text.split())))
        else:
            out.append((kind, text))
    return out


def _string_value(text: str) -> str:
    m = _STRING_RE.match(text)
    if not m:
        return text
    prefix, quote, body = m.group(1), m.group(2), m.group(3)
    if quote not in _QUOTES:
        return text
    # ' and " are one equivalence class (swapping them is layout); a backtick template is not
    marker = "`" if quote == "`" else ""
    return f"{prefix}{marker}:{body}"


def _describe_divergence(before, after) -> str:
    for i, (a, b) in enumerate(zip(before, after)):
        if a != b:
            return t("f.token_changed", index=i, before=repr(a[1][:40]), after=repr(b[1][:40]))
    if len(before) != len(after):
        return t("f.token_count", before=len(before), after=len(after))
    return t("f.changed")
