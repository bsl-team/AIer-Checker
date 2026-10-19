"""Python support: stdlib ``tokenize`` feeding the shared token model, ``ast`` for semantics."""
from __future__ import annotations

import ast
import io
import token as tokmod
import tokenize
from dataclasses import dataclass, field

from .document import Document
from .tokens import CMT, ID, KW, NUM, PUNCT, SPEC_BY_NAME, STR, Token


def tokenize_python(src: str) -> list[Token]:
    """Tokenize Python source into the kernel's token model (whitespace stays implicit)."""
    kw = SPEC_BY_NAME["python"].keywords
    toks: list[Token] = []
    last_end_row = -1
    last_end_col = -1
    readline = io.StringIO(src if src.endswith("\n") else src + "\n").readline
    for tok in tokenize.generate_tokens(readline):
        kind = tok.type
        if kind in (tokmod.NEWLINE, tokmod.NL, tokmod.INDENT, tokmod.DEDENT, tokmod.ENDMARKER, tokmod.ENCODING):
            continue
        if kind == tokmod.COMMENT:
            k = CMT
        elif kind == tokmod.STRING:
            k = STR
        elif kind == tokmod.NUMBER:
            k = NUM
        elif kind == tokmod.NAME:
            k = KW if tok.string in kw else ID
        elif kind == tokmod.OP:
            k = PUNCT
        else:
            continue
        srow, scol = tok.start
        erow, ecol = tok.end
        gap = -1 if srow != last_end_row else scol - last_end_col
        toks.append(
            Token(kind=k, text=tok.string, row=srow, col=scol, end_row=erow, end_col=ecol,
                  gap=gap, index=len(toks))
        )
        last_end_row, last_end_col = erow, ecol
    return toks


@dataclass
class PythonSemantics:
    functions: list[tuple[int, str]] = field(default_factory=list)
    methods: list[tuple[int, str]] = field(default_factory=list)
    classes: list[tuple[int, str]] = field(default_factory=list)
    arguments: list[tuple[int, str]] = field(default_factory=list)
    variables: list[tuple[int, str]] = field(default_factory=list)
    constants: list[tuple[int, str]] = field(default_factory=list)
    attributes: list[tuple[int, str]] = field(default_factory=list)
    module_level_names: list[tuple[int, str]] = field(default_factory=list)
    docstring_functions: int = 0
    docstring_classes: int = 0
    typed_params: int = 0
    typed_returns: int = 0
    total_params: int = 0
    fstrings: int = 0
    str_count: int = 0
    decorators: int = 0
    async_defs: int = 0
    bare_excepts: int = 0
    type_annotations: int = 0
    parse_error: str | None = None


def _is_const_name(name: str) -> bool:
    return bool(name) and name.isupper() and all(c.isalnum() or c == "_" for c in name)


def analyze_ast(src: str) -> PythonSemantics:
    """Collect naming roles and structural facts; records a parse error instead of raising."""
    sem = PythonSemantics()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        sem.parse_error = f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"
        return sem
    except Exception as exc:
        sem.parse_error = f"{type(exc).__name__}: {exc}"
        return sem
    _Collector(sem).visit(tree)
    for key in ("constants", "variables", "attributes", "arguments", "module_level_names"):
        setattr(sem, key, _dedup(getattr(sem, key)))
    return sem


class _Collector(ast.NodeVisitor):
    """Walks the tree keeping class/function scope depth so names land in the right role."""

    def __init__(self, sem: PythonSemantics):
        self.sem = sem
        self.class_depth = 0
        self.func_depth = 0

    def _visit_def(self, node, is_async: bool) -> None:
        if is_async:
            self.sem.async_defs += 1
        self.sem.decorators += len(node.decorator_list)
        if ast.get_docstring(node):
            self.sem.docstring_functions += 1
        if node.returns is not None:
            self.sem.typed_returns += 1
        bucket = self.sem.methods if self.class_depth else self.sem.functions
        bucket.append((node.lineno, node.name))
        a = node.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            if arg.arg in ("self", "cls"):
                continue
            self.sem.total_params += 1
            if arg.annotation is not None:
                self.sem.typed_params += 1
            self.sem.arguments.append((arg.lineno or node.lineno, arg.arg))
        if a.vararg:
            self.sem.arguments.append((node.lineno, a.vararg.arg))
        if a.kwarg:
            self.sem.arguments.append((node.lineno, a.kwarg.arg))
        self.func_depth += 1
        self.generic_visit(node)
        self.func_depth -= 1

    def visit_FunctionDef(self, node) -> None:
        self._visit_def(node, False)

    def visit_AsyncFunctionDef(self, node) -> None:
        self._visit_def(node, True)

    def visit_ClassDef(self, node) -> None:
        self.sem.classes.append((node.lineno, node.name))
        self.sem.decorators += len(node.decorator_list)
        if ast.get_docstring(node):
            self.sem.docstring_classes += 1
        self.class_depth += 1
        prev_func, self.func_depth = self.func_depth, 0
        self.generic_visit(node)
        self.class_depth -= 1
        self.func_depth = prev_func

    def _record_targets(self, node, targets) -> None:
        for tgt in targets:
            names: list[str] = []
            if isinstance(tgt, ast.Name):
                names = [tgt.id]
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                names = [e.id for e in tgt.elts if isinstance(e, ast.Name)]
            elif isinstance(tgt, ast.Starred) and isinstance(tgt.value, ast.Name):
                names = [tgt.value.id]
            for nm in names:
                top_level = self.func_depth == 0
                if _is_const_name(nm):
                    if top_level or self.class_depth:
                        self.sem.constants.append((node.lineno, nm))
                    else:
                        self.sem.variables.append((node.lineno, nm))
                else:
                    self.sem.variables.append((node.lineno, nm))
                if top_level or self.class_depth:
                    self.sem.module_level_names.append((node.lineno, nm))

    def visit_Assign(self, node) -> None:
        self._record_targets(node, node.targets)
        self.generic_visit(node)

    def visit_AnnAssign(self, node) -> None:
        if node.annotation is not None:
            self.sem.type_annotations += 1
        self._record_targets(node, [node.target] if node.target else [])
        self.generic_visit(node)

    def visit_AugAssign(self, node) -> None:
        if isinstance(node.target, ast.Name):
            self.sem.variables.append((node.lineno, node.target.id))
        self.generic_visit(node)

    def visit_NamedExpr(self, node) -> None:
        if isinstance(node.target, ast.Name):
            self.sem.variables.append((node.lineno, node.target.id))
        self.generic_visit(node)

    def visit_For(self, node) -> None:
        if isinstance(node.target, ast.Name):
            self.sem.variables.append((node.lineno, node.target.id))
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_withitem(self, node) -> None:
        if isinstance(node.optional_vars, ast.Name):
            self.sem.variables.append((node.lineno, node.optional_vars.id))
        self.generic_visit(node)

    def visit_ExceptHandler(self, node) -> None:
        if node.name:
            self.sem.variables.append((node.lineno, node.name))
        if node.type is None:
            self.sem.bare_excepts += 1
        self.generic_visit(node)

    def visit_Attribute(self, node) -> None:
        if not node.attr.startswith("__"):
            self.sem.attributes.append((node.lineno, node.attr))
        self.generic_visit(node)

    def visit_JoinedStr(self, node) -> None:
        self.sem.fstrings += 1
        self.generic_visit(node)

    def visit_Constant(self, node) -> None:
        if isinstance(node.value, str):
            self.sem.str_count += 1
        self.generic_visit(node)

    def visit_Import(self, node) -> None:
        for alias in node.names:
            self.sem.module_level_names.append((node.lineno, alias.asname or alias.name.split(".")[0]))
        self.generic_visit(node)

    def visit_ImportFrom(self, node) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.sem.module_level_names.append((node.lineno, alias.asname or alias.name))
        self.generic_visit(node)

    def visit_Global(self, node) -> None:
        for nm in node.names:
            self.sem.module_level_names.append((node.lineno, nm))
        self.generic_visit(node)


def _dedup(items: list[tuple[int, str]]) -> list[tuple[int, str]]:
    seen: set[str] = set()
    out = []
    for row, name in sorted(items):
        if name in seen or name.startswith("_") and name.strip("_") == "":
            continue
        seen.add(name)
        out.append((row, name))
    return out


def blank_line_pattern(doc: Document, sem: PythonSemantics) -> dict[str, float]:
    """Blank rows before top-level defs/classes, and the largest blank run inside bodies."""
    starts = {row for row, _ in sem.functions} | {row for row, _ in sem.classes}
    before: dict[int, int] = {}
    for row in sorted(starts):
        if row - 1 < 1 or doc.row(row).indent.strip():
            continue
        n = 0
        r = row - 1
        while r >= 1 and doc.row(r).blank:
            n += 1
            r -= 1
        before.setdefault(n, 0)
        before[n] += 1
    total = sum(before.values()) or 1
    mode = max(before, key=lambda k: (before[k], -k))
    return {"mode": float(mode), "coverage": before.get(mode, 0) / total, "observed": float(total)}
