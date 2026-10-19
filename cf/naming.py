"""Identifier naming conventions: classify names and attribute them to roles."""
from __future__ import annotations

import re

from .document import Document
from .tokens import ID, KW, PRE, PUNCT

TYPE_KW = {"class", "struct", "enum", "union", "interface", "trait", "impl", "record"}
CONST_KW = {"const", "constexpr", "final", "readonly", "static", "volatile"}
TYPE_INTRO_KW = TYPE_KW | {
    "type", "typedef", "namespace", "module", "package", "using", "func", "fn", "def", "new"
}


def classify(name: str) -> str | None:
    """Return the convention a name evidences, or ``None`` when it proves nothing."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        return None
    core = name.strip("_")
    if len(core) <= 1:
        return None
    if core.startswith("__") and core.endswith("__") and len(core) > 4:
        return None  # python dunder: fixed by the language, says nothing about the author
    parts = [p for p in core.split("_") if p]
    if not parts:
        return None
    if len(parts) > 1:
        if all(p.isupper() for p in parts):
            return "UPPER_SNAKE_CASE"
        if all(p[:1].isupper() for p in parts):
            return "Pascal_Snake_Case"
        if all(p.islower() for p in parts):
            return "snake_case"
        return "mixed_case"
    solo = parts[0]
    if solo.isupper():
        return "UPPER_SNAKE_CASE"
    if solo[:1].isupper():
        return "PascalCase" if not solo[1:].isupper() else "mixed_case"
    if any(c.isupper() for c in solo):
        return "camelCase"
    return None  # one lowercase word fits camelCase and snake_case alike


def roles(doc: Document) -> dict[str, list[tuple[int, str]]]:
    """Map naming role -> ``(row, name)`` for one document."""
    if doc.is_python:
        return _python_roles(doc)
    return _clike_roles(doc)


def _python_roles(doc: Document) -> dict[str, list[tuple[int, str]]]:
    from .py_lang import analyze_ast

    sem = analyze_ast(doc.src)
    return {
        "function": sem.functions,
        "method": sem.methods,
        "type": sem.classes,
        "parameter": sem.arguments,
        "variable": sem.variables,
        "constant": sem.constants,
        "member": sem.attributes,
    }


def _match(toks: list, open_idx: int) -> int | None:
    """Index of the bracket matching the opener at ``open_idx``."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    want = pairs.get(toks[open_idx].text)
    if want is None:
        return None
    depth = 0
    for j in range(open_idx, len(toks)):
        t = toks[j]
        if t.kind == PUNCT:
            if t.text == toks[open_idx].text:
                depth += 1
            elif t.text == want:
                depth -= 1
                if depth == 0:
                    return j
    return None


def _prev(toks, i, back=1):
    j = i - back
    return toks[j] if 0 <= j < len(toks) else None


def _macro_names(toks) -> list[tuple[int, str]]:
    out = []
    for t in toks:
        if t.kind == PRE:
            m = re.match(r"#\s*define\s+([A-Za-z_]\w*)", t.text)
            if m:
                out.append((t.row, m.group(1)))
    return out


def _clike_roles(doc: Document) -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {
        "function": [], "method": [], "type": [], "parameter": [], "variable": [],
        "constant": [], "member": [], "macro": _macro_names(doc.tokens),
    }
    toks = doc.tokens
    seen: dict[str, set[tuple[int, str]]] = {k: set() for k in out}

    def add(role: str, row: int, name: str) -> None:
        if (row, name) not in seen[role]:
            seen[role].add((row, name))
            out[role].append((row, name))

    body_ranges = _definition_ranges(doc)
    enum_ranges = _keyword_block_ranges(doc, ("enum",))
    struct_ranges = _keyword_block_ranges(doc, ("struct", "class", "union", "interface"))
    typedef_ranges: set[int] = set()

    for i, t in enumerate(toks):
        if t.kind != ID:
            continue
        pr = _prev(toks, i)
        nx = toks[i + 1] if i + 1 < len(toks) else None
        if pr is None or nx is None:
            continue
        if pr.is_punct(".") or pr.is_punct("->"):
            add("member", t.row, t.text)
            continue
        if pr.kind == KW and pr.text in TYPE_KW:
            add("type", t.row, t.text)
            continue
        if _in_range(i, enum_ranges):
            add("constant", t.row, t.text)
            continue
        if _in_range(i, typedef_ranges):
            add("type", t.row, t.text)
            continue
        if _is_decl_name(toks, i):
            specs = _declaration_specifiers(toks, i)
            if _in_range(i, struct_ranges) and specs.isdisjoint(CONST_KW):
                add("member", t.row, t.text)
            elif specs & CONST_KW and doc.depths[t.row - 1][0] <= 1:
                add("constant", t.row, t.text)
            elif specs or nx.is_punct("="):
                add("variable", t.row, t.text)
            continue
        if _is_arg_position(doc, i, body_ranges):
            add("parameter", t.row, t.text)
    if doc.spec.name in ("go", "rust", "python"):
        _keyword_decl_roles(doc, out, add)
    return out


def _declaration_specifiers(toks, i) -> set[str]:
    """Keywords/types immediately in front of a declared name, on the same row."""
    found: set[str] = set()
    j = i - 1
    row = toks[i].row
    while j >= 0 and toks[j].row == row:
        t = toks[j]
        if t.kind == KW:
            found.add(t.text)
            j -= 1
        elif t.kind == ID and (toks[j].col + len(t.text) <= toks[j + 1].col):
            found.add(t.text)
            j -= 1
        elif t.kind == PUNCT and t.text in ("*", "&", "<", ">", "::", ",", ">", ">>"):
            j -= 1
        else:
            break
    return found


def _is_decl_name(toks, i) -> bool:
    """A name that declares something: preceded by a type-ish token, followed by = , ; [ or another name."""
    t = toks[i]
    pr = _prev(toks, i)
    if pr is None or pr.kind not in (ID, KW, PUNCT):
        return False
    if pr.kind == KW and pr.text in TYPE_INTRO_KW:
        return False  # covered by the type / language-declaration passes
    if pr.kind == PUNCT and pr.text not in ("*", "&", ">", ")", ","):
        return False
    if pr.kind == KW and pr.text in ("return", "if", "for", "while", "switch", "case", "else", "new", "sizeof", "delete"):
        return False
    nx = toks[i + 1] if i + 1 < len(toks) else None
    if nx is None:
        return False
    if nx.is_punct(")") and _prev(toks, i) is not None and toks[i - 1].is_punct("("):
        return True  # void f(int x) style
    if nx.kind in (ID, KW) and toks[i + 1].row == t.row:
        return True  # `Type name`
    if nx.is_punct("=") or nx.is_punct(";") or nx.is_punct(",") or nx.is_punct("["):
        return True
    if nx.is_punct(":") and toks[i + 1].kind == PUNCT and toks[i + 1].text == ":":
        return False
    return False


def _definition_ranges(doc: Document) -> list[tuple[int, int]]:
    """Token index ranges of parameter lists that belong to a function definition."""
    ranges: list[tuple[int, int]] = []
    toks = doc.tokens
    for i, t in enumerate(toks):
        if not t.is_punct("(") or t.gap != 0:
            continue
        pr = _prev(toks, i)
        if pr is None:
            continue
        if pr.kind == KW and pr.text not in ("func", "fn", "def"):
            continue
        if pr.kind not in (ID, KW):
            continue
        close = _match(toks, i)
        if close is not None and _after_brace(toks, close):
            ranges.append((i, close))
    return ranges


def _after_brace(toks, close_idx: int) -> bool:
    """The paren group is followed by a block opener, directly or after const/noexcept."""
    for j in range(close_idx + 1, min(close_idx + 8, len(toks))):
        t = toks[j]
        if t.is_punct("{"):
            return True
        if t.is_punct(";") or t.is_punct(":"):
            return False
        if t.kind in (ID, KW, PUNCT) and t.text in ("const", "noexcept", "override", "final", "->", "throws"):
            continue
        return False
    return False



def _keyword_block_ranges(doc: Document, keywords) -> list[tuple[int, int]]:
    """Ranges of ``{ ... }`` bodies introduced by one of ``keywords``."""
    ranges = []
    toks = doc.tokens
    for i, t in enumerate(toks):
        if t.kind == KW and t.text in keywords:
            for j in range(i + 1, min(i + 12, len(toks))):
                if toks[j].is_punct("{"):
                    close = _match(toks, j)
                    if close is not None:
                        ranges.append((j, close))
                    break
                if toks[j].kind == PUNCT and toks[j].text in ("(", ";"):
                    break
    return ranges


def _in_range(i: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo < i < hi for lo, hi in ranges)


def _is_arg_position(doc, i, body_ranges) -> bool:
    toks = doc.tokens
    for lo, hi in body_ranges:
        if lo < i < hi:
            t = toks[i]
            if t.kind != ID:
                return False
            pr = _prev(toks, i)
            if pr is not None and pr.kind == PUNCT and pr.text in (".", "::", "<", ">"):
                return False
            nx = toks[i + 1] if i + 1 < len(toks) else None
            if nx is not None and nx.is_punct("("):
                return False
            return True
    return False


_LANG_DECL_KW = {
    "go": {"func": "function", "type": "type", "const": "constant", "var": "variable"},
    "rust": {"fn": "function", "struct": "type", "enum": "type", "trait": "type", "const": "constant", "static": "constant", "type": "type", "impl": "type", "mod": "type", "use": None, "let": "variable", "loop": None},
    "python": {"def": "function", "class": "type", "import": None, "from": None},
}


def _keyword_decl_roles(doc: Document, out, add) -> None:
    """Keywords that name a declaration directly, per language."""
    mapping = _LANG_DECL_KW.get(doc.spec.name)
    if not mapping:
        return
    toks = doc.tokens
    for i, t in enumerate(toks):
        if t.kind != KW:
            continue
        role = mapping.get(t.text)
        if not role:
            continue
        j = i + 1
        if t.text == "func" and _prev(toks, i) is not None:
            pass
        if role == "function" and doc.spec.name in ("go", "rust"):
            if toks[j].is_punct("("):  # anonymous / closure
                continue
            while j < len(toks) and toks[j].is_punct(")"):
                j += 1
        nm = toks[j] if j < len(toks) else None
        if nm is None or nm.kind != ID:
            continue
        if (nm.row, nm.text) not in {x for x in out[role]}:
            add(role, nm.row, nm.text)
