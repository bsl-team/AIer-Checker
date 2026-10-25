import pytest

from cf.document import build_document, normalize
from cf.tokens import CMT, ID, KW, NUM, PUNCT, STR, detect, tokenize_clike, SPEC_BY_NAME


def test_clike_basic_kinds_and_gaps():
    src = "int main() {\n    if (x) return 0;\n}\n"
    doc = build_document(src, "a.c")
    assert doc.spec.name == "c"
    toks = doc.tokens
    assert toks[0].kind == KW and toks[0].text == "int"
    second = toks[1]
    assert second.kind == ID and second.text == "main" and second.gap == 1
    brace_line = doc.row(2)
    assert brace_line.indent == "    " and brace_line.tokens[0].text == "if"


def test_multiline_block_comment_marks_body_rows():
    doc = build_document("/* a\n * b\n */\nint x;\n", "a.c", SPEC_BY_NAME["c"])
    assert doc.row(1).tokens[0].kind == CMT
    assert doc.row(2).in_trivia and doc.row(3).in_trivia
    assert doc.row(4).tokens[0].text == "int"


def test_preprocessor_directive_is_one_token_and_row_flag():
    doc = build_document("#include <stdio.h>\n#define MAX(a,b) ((a)>(b)?(a):(b))\nint main(void){return 0;}\n", "m.c")
    assert doc.row(1).directive and doc.row(2).directive
    assert doc.row(1).tokens[0].kind == "pre"
    assert doc.row(1).tokens[0].text.startswith("#include")


def test_strings_and_chars_not_treated_as_code():
    doc = build_document('char* s = "a{b}c"; char c = \'/\';\n', "a.c", SPEC_BY_NAME["c"])
    kinds = [t.kind for t in doc.tokens]
    assert STR in kinds and "char" in kinds
    strings = [t for t in doc.tokens if t.kind == STR]
    assert strings[0].text == '"a{b}c"'
    chars = [t for t in doc.tokens if t.kind == "char"]
    assert [t.text for t in chars] == ["'/'"]
    # braces inside the string must not affect depth tracking
    assert doc.depths[0] == (0, 0)


def test_python_tokens_via_stdlib_tokenize():
    src = 'def f(a, b=1):\n    """doc"""\n    return a + b  # add\n'
    doc = build_document(src, "x.py")
    assert doc.spec.family == "python"
    assert doc.parse_error is None
    row2 = doc.row(2)
    assert row2.tokens[0].kind == STR and row2.tokens[0].text == '"""doc"""'
    row3 = doc.row(3)
    assert row3.tokens[-1].kind == CMT


def test_python_fallback_on_broken_source():
    doc = build_document("def f(:\n  x = ", "broken.py")
    assert doc.parse_error is not None
    assert doc.tokens  # still tokenized by the clike scanner


def test_line_continuation_in_python_string():
    src = 'x = ("a"\n     "b")\n'
    doc = build_document(src, "y.py")
    assert doc.row(2).in_trivia is False
    assert doc.row(1).tokens[-1].text == '"a"'
    # depths only tracks braces; the open paren is visible through paren_depth
    assert doc.depths[0][1] == 0 and doc.depths[1][0] == 0
    assert doc.paren_depth[0] == 0 and doc.paren_depth[1] == 1


def test_python_multiline_string_body_rows():
    doc = build_document('s = """\nline\n"""\n', "z.py")
    assert doc.row(1).tokens[0].text == "s"
    str_tok = [t for t in doc.row(1).tokens if t.kind == STR][0]
    assert str_tok.multiline and str_tok.end_row == 3
    assert doc.row(2).in_trivia and doc.row(3).in_trivia


@pytest.mark.parametrize(
    "path,expected",
    [
        ("a.cpp", "cpp"),
        ("a.H", "c"),
        ("main.go", "go"),
        ("x.tsx", "typescript"),
        ("Y.java", "java"),
        ("z.py", "python"),
        ("lib.rs", "rust"),
    ],
)
def test_detect_by_extension(path, expected):
    assert detect(path, "").name == expected


def test_detect_by_content():
    assert detect(None, '#include <stdio.h>\nint main(){return 0;}\n').name == "c"
    assert detect(None, "func main() {\n\tfmt.Println()\n}\n").name == "go"
    assert detect(None, "def f(x):\n    return x\n").name == "python"
    assert detect(None, "import { a } from 'b';\nexport const x = 1;\n").name in ("javascript", "typescript")


def test_normalize_keeps_eol_style():
    norm, eol = normalize("a\r\nb\r\n")
    assert norm == "a\nb\n" and eol == "\r\n"


def test_number_and_operator_scanning():
    toks = tokenize_clike("0x1F 1e-3 3.14f a >>= b ++c", SPEC_BY_NAME["c"])
    assert [t.text for t in toks if t.kind == NUM] == ["0x1F", "1e-3", "3.14f"]
    assert ">>=" in [t.text for t in toks if t.kind == PUNCT]
    assert "++" in [t.text for t in toks if t.kind == PUNCT]


def test_jsx_free_js_template_literal_with_braces():
    src = "const t = `a ${ b } c`;\n"
    doc = build_document(src, "a.js")
    strings = [t for t in doc.tokens if t.kind == STR]
    assert len(strings) == 1 and strings[0].text == "`a ${ b } c`"
    assert doc.depths[0] == (0, 0)  # ${ } inside the template must not change depth


def test_token_stream_roundtrips_multiline_tokens():
    doc = build_document("/* x\ny */ int z;\n", "a.c", SPEC_BY_NAME["c"])
    assert doc.token_stream()[0][1] == "/* x\ny */"


def test_cpp_class_header_is_not_mistaken_for_python_without_an_extension():
    # `class Solver {` used to satisfy the loose python pattern and win before the #include test
    cxx = "#include <cstdio>\n\nclass Solver {\npublic:\n    int go() { return 1; }\n};\n"
    assert detect(None, cxx).name in ("cpp", "c")
    assert detect(None, "class Solver {\n};\n").name != "python"


def test_python_class_needs_a_colon_or_base_list():
    assert detect(None, "class Config:\n    pass\n").name == "python"
    assert detect(None, "class Config(Base, Mixin):\n    pass\n").name == "python"


def test_python_shebang_is_enough_without_an_extension():
    assert detect(None, "#!/usr/bin/env python3\nimport sys\n").name == "python"
    assert detect(None, "#!/usr/bin/python\nprint(1)\n").name == "python"


def test_long_license_header_does_not_push_python_past_the_head_window():
    header = "".join(f"# copyright line {i}\n" for i in range(120))
    assert detect(None, header + "\ndef main():\n    pass\n").name == "python"


def test_k02_cpp_and_csharp_inheritance_is_not_python():
    assert detect(None, "class Foo : public Bar {\npublic:\n    int x;\n};\n").name != "python"
    assert detect(None, "class Foo : Bar {\n    int x;\n}\n").name != "python"
    assert detect(None, "class Foo : Bar {\n    int x;\n}\n").name == "clike"


def test_k02_python_class_headers_still_detect():
    assert detect(None, "class Foo:\n    pass\n").name == "python"
    assert detect(None, "class Foo(Base, Mixin):\n    pass\n").name == "python"
    assert detect(None, "class Foo(object):  # note\n    pass\n").name == "python"


def test_k02_oi_cpp_without_an_extension_is_cpp_not_c():
    src = ("#include <bits/stdc++.h>\nusing namespace std;\n"
           "int main(){ vector<int> a; cout << 1; return 0; }\n")
    assert detect(None, src).name == "cpp"
    assert detect(None, "#include <stdio.h>\nint main(){ printf(\"x\"); return 0; }\n").name == "c"
    assert detect("t.cpp", src).name == "cpp"          # the extension still wins
