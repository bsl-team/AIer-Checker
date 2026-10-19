import pytest

from cf.document import build_document
from cf.extract import extract
from cf.format import FORMAT_RULES, FormatOptions, Formatter, format_source, token_identity
from cf.profile import Feature, Profile
from tests.util import read_sample, sample_path

SAMPLES = ["cpp_knr.cpp", "cpp_allman.cpp", "c_tabs.c", "go_style.go", "js_single.js", "py_default.py", "mixed.cpp"]
ALL_SAMPLES = pytest.mark.parametrize("name", SAMPLES)


def profile_of(user="t", language="cpp", family="clike", **kv) -> Profile:
    """A profile whose listed rules are confidently observed (10 agreeing observations each)."""
    p = Profile(user=user, language=language, family=family)
    for key, val in kv.items():
        if isinstance(val, bool):
            text = "true" if val else "false"
        else:
            text = str(val)
        p.features[key] = Feature(key=key, counts={text: 10}, rows={text: [1]})
    return p


def fmt(src, path, profile, **opts) -> str:
    res = format_source(src, path, profile, FormatOptions(**opts))
    assert res.error is None, res.error
    return res.text


def stream(src, path=None):
    return token_identity(src, path)


@ALL_SAMPLES
def test_formatting_never_changes_the_token_stream(name):
    src = read_sample(name)
    doc = build_document(src, sample_path(name))
    profile = extract(doc)
    result = Formatter(doc, profile, FormatOptions()).run()
    assert result.error is None
    assert [t for t in stream(result.text, name)] == [t for t in stream(src, name)]


@ALL_SAMPLES
def test_formatting_a_file_with_its_own_profile_is_idempotent(name):
    src = read_sample(name)
    doc = build_document(src, sample_path(name))
    profile = extract(doc)
    once = fmt(src, name, profile)
    twice = fmt(once, name, profile)
    assert once == twice, "second pass should not move anything"


def test_knr_to_allman_moves_opening_braces():
    src = read_sample("cpp_knr.cpp")
    out = fmt(src, "cpp_knr.cpp", profile_of(**{"brace.style": "next"}))
    rows = out.split("\n")
    assert "class Solver {" not in out
    assert "int main(int argc, char **argv)" in rows
    assert any(r.strip() == "{" for r in rows)
    assert stream(out, "cpp_knr.cpp") == stream(src, "cpp_knr.cpp")


def test_allman_to_knr_attaches_braces():
    src = read_sample("cpp_allman.cpp")
    out = fmt(src, "cpp_allman.cpp", profile_of(**{"brace.style": "attach"}))
    assert "\n{\n" not in out
    assert "int distance_sq(struct Point a, struct Point b) {" in out
    assert "} else {" in out or "}\n" in out


def test_tabs_to_spaces_rewrites_indentation():
    src = read_sample("c_tabs.c")
    out = fmt(src, "c_tabs.c", profile_of(**{"indent.type": "space", "indent.size": 4}))
    body = [r for r in out.split("\n") if r.strip()]
    assert all("\t" not in r for r in body)
    assert "    if (n <= 1) return n;" in out
    assert stream(out, "c_tabs.c") == stream(src, "c_tabs.c")


def test_spaces_to_tabs():
    src = read_sample("cpp_knr.cpp")
    out = fmt(src, "cpp_knr.cpp", profile_of(**{"indent.type": "tab", "indent.size": 1, "indent.namespace_body": "none"}))
    assert "\tif (values[i] <= target) {" in out
    assert "    " not in "\n".join(r for r in out.split("\n") if r.startswith("    "))


def test_python_reindents_to_two_spaces():
    src = read_sample("py_default.py")
    prof = profile_of(language="python", family="python", **{"indent.type": "space", "indent.size": 2})
    out = fmt(src, "py_default.py", prof)
    assert "  def __init__(self):" in out
    assert "    self.handlers = {}" in out
    assert stream(out, "py_default.py") == stream(src, "py_default.py")


def test_python_quote_style_converts_when_safe():
    src = "x = 'abc'\ny = \"abc\"\nz = 'it\\'s'\nw = 'a\"b'\n"
    prof = profile_of(language="python", family="python", **{"quote.style": "double", "indent.size": 4})
    out = fmt(src, "t.py", prof)
    assert 'x = "abc"' in out
    assert "y = \"abc\"" in out
    assert "z = 'it\\'s'" in out  # escaped content is left alone
    assert "w = 'a\"b'" in out  # would need an escape: left alone


def test_python_fstring_is_atomic_and_survives():
    src = "def f(a, b):\n    return f'{a} of {b}'\n"
    prof = profile_of(language="python", family="python", **{"indent.size": 4, "quote.style": "double"})
    out = fmt(src, "t.py", prof)
    assert 'return f"{a} of {b}"' in out
    assert stream(out, "t.py") == stream(src, "t.py")


def test_js_template_literal_is_never_converted():
    src = "const a = `x ${y} z`;\nconst b = 'plain';\n"
    prof = profile_of(language="javascript", family="clike", **{"quote.style": "double", "indent.size": 2})
    out = fmt(src, "t.js", prof)
    assert "`x ${y} z`" in out
    assert 'const b = "plain";' in out


def test_long_rows_wrap_at_comma_boundaries():
    src = 'printf("a very long call that should be split: %d %d %d", first_argument, second_argument, third);\n'
    prof = profile_of(**{"width.limit": 60, "indent.size": 4, "indent.type": "space"})
    out = fmt("int main() {\n    " + src + "}", "t.c", prof)
    rows = out.split("\n")
    assert any(r.strip().startswith("first_argument,") for r in rows)
    assert any(r.strip() == ")" or r.strip().startswith(");") for r in rows)
    assert max(len(r) for r in rows) < 70


def test_blank_lines_are_capped_and_glue_rules_applied():
    src = "int a;\n\n\n\nint b;\n\n\nint main() {\n\n    return 0;\n\n\n}\n"
    prof = profile_of(**{"blank.max_in_block": 1, "blank.between_functions": 1, "blank.after_open_brace": False, "blank.before_close_brace": False})
    out = fmt(src, "t.c", prof)
    assert "\n\n\n\n" not in out  # at most two blank rows at top level
    assert "int main() {\n    return 0;\n}" in out


def test_definition_gap_is_inserted_when_missing():
    src = "int a(void) {\n    return 1;\n}\nint b(void) {\n    return 2;\n}\n"
    prof = profile_of(**{"blank.between_functions": 1, "indent.size": 4, "blank.max_in_block": 1})
    out = fmt(src, "t.c", prof)
    assert "}\n\nint b(void) {" in out


def test_rules_option_limits_the_rewrite():
    src = read_sample("cpp_allman.cpp")
    prof = profile_of(**{"brace.style": "attach", "indent.type": "space", "indent.size": 4})
    out = fmt(src, "cpp_allman.cpp", prof, rules={"indent.size"})
    assert "int main(void)\n{" in out  # braces left as they were
    out2 = fmt(src, "cpp_allman.cpp", prof)
    assert "int main(void) {" in out2


def test_low_confidence_rules_are_skipped():
    src = "int main() {\nreturn 0;\n}\n"
    prof = Profile(user="thin", language="cpp", family="clike")
    prof.features["indent.size"] = Feature(key="indent.size", counts={"4": 1}, rows={"4": [1]})
    result = format_source(src, "thin.c", prof, FormatOptions(use_defaults=False))
    assert "indent.size" in result.skipped
    assert result.text == src


def test_unobserved_rules_use_family_defaults_only_when_allowed():
    src = "int main()\n{\nreturn 0;\n}\n"
    prof = Profile(user="empty", language="cpp", family="clike")
    with_defaults = format_source(src, "d.c", prof, FormatOptions())
    assert with_defaults.text.startswith("int main() {")
    observed_only = format_source(src, "d.c", prof, FormatOptions(use_defaults=False))
    assert observed_only.text == src


def test_crlf_input_keeps_crlf_output():
    src = "int main() {\r\n    return 0;\r\n}\r\n"
    prof = profile_of(**{"indent.type": "space", "indent.size": 4, "brace.style": "attach"})
    out = fmt(src, "crlf.c", prof)
    assert out.endswith("\r\n")
    assert "\r\n" in out
    assert out.replace("\r\n", "").find("\n") == -1  # no bare LF left behind


def test_block_comment_bodies_move_whole():
    src = "int main() {\n/* line one\n   line two */\nreturn 0;\n}\n"
    prof = profile_of(**{"indent.type": "space", "indent.size": 4, "brace.style": "attach"})
    out = fmt(src, "cmt.c", prof)
    assert "    /* line one\n       line two */" in out


def test_final_newline_rule_is_respected():
    src = "int x;\n"
    prof = profile_of(**{"blank.final_newline": False})
    out = fmt(src, "nf.c", prof)
    assert not out.endswith("\n")
    prof2 = profile_of(**{"blank.final_newline": True})
    assert fmt(src, "nf.c", prof2).endswith("\n")


def test_fragment_with_unbalanced_braces_is_still_safe():
    src = "void f() {\n  int x = 1;\n"
    prof = profile_of(**{"indent.size": 4})
    out = fmt(src, "frag.c", prof)
    assert "        int x = 1;" not in out
    assert "    int x = 1;" in out
    assert stream(out, "frag.c") == stream(src, "frag.c")


def test_applied_lists_only_rules_that_moved_something():
    src = "int main()\n{\n\treturn 0;\n}\n"
    prof = profile_of(**{"brace.style": "attach", "indent.type": "space", "indent.size": 4, "quote.style": "double"})
    res = format_source(src, "t.c", prof, FormatOptions())
    assert "brace.style" in res.applied
    assert "indent.type" in res.applied
    assert "quote.style" not in res.applied  # nothing to convert
    assert set(res.applied) <= set(FORMAT_RULES)


def test_nested_switch_case_indent_modes():
    src = "void f(int x) {\n    switch (x) {\n    case 1:\n        break;\n    }\n}\n"
    prof_flush = profile_of(**{"layout.case_indent": "flush", "indent.size": 4, "brace.style": "attach"})
    out = fmt(src, "sw.c", prof_flush)
    assert "    case 1:\n        break;" in out
    prof_indented = profile_of(**{"layout.case_indent": "indented", "indent.size": 4, "brace.style": "attach"})
    out2 = fmt(src, "sw.c", prof_indented)
    assert "        case 1:\n            break;" in out2


def test_every_profile_on_every_sample_preserves_tokens():
    """Cross-product stress: no sample/profile combination may change code identity."""
    docs = {name: build_document(read_sample(name), sample_path(name)) for name in SAMPLES}
    profiles = {name: extract(doc) for name, doc in docs.items()}
    for target, doc in docs.items():
        for owner, profile in profiles.items():
            if doc.spec.family != profile.family:
                continue  # mixing python and clike profiles is not a supported request
            result = Formatter(doc, profile, FormatOptions()).run()
            assert result.error is None, f"{target} with {owner} profile: {result.error}"
            assert token_identity(result.text, target) == token_identity(doc.src, target), (target, owner)


def test_files_without_code_tokens_are_left_alone():
    prof = profile_of(**{"indent.size": 4})
    assert format_source("", "e.c", prof).text == ""
    assert format_source("\n\n", "e.c", prof).changed is False


def test_comments_never_merge_with_code():
    src = "int main() {\n    int x = 1; // note\n    return x;\n}\n"
    prof = profile_of(**{"indent.size": 2, "space.around_assignment": True})
    out = fmt(src, "t.c", prof)
    assert "int x = 1; // note" in out
    assert "// notint" not in out
    assert stream(out, "t.c") == stream(src, "t.c")


def test_multiline_comment_body_is_not_duplicated():
    src = "int main() {\n    /* a\n       b */\n    return 0;\n}\n"
    prof = profile_of(**{"indent.size": 2, "brace.style": "attach"})
    out = fmt(src, "t.c", prof)
    assert out.count("/* a") == 1
    assert out.count("b */") == 1
    assert stream(out, "t.c") == stream(src, "t.c")


def test_preprocessor_lines_are_left_verbatim():
    src = "#if defined(X)\n  #define Y 1\n#endif\nint main(void) { return 0; }\n"
    prof = profile_of(**{"indent.size": 4, "brace.style": "attach"})
    out = fmt(src, "t.c", prof)
    assert "#if defined(X)" in out
    assert "  #define Y 1" in out
    assert stream(out, "t.c") == stream(src, "t.c")


def test_python_dedent_to_module_level_is_exact():

    src = "class A:\n    def m(self):\n        return 1\n\n\ndef top():\n    return 2\n"
    prof = profile_of(language="python", family="python", **{"indent.size": 4, "blank.between_functions": 2, "blank.between_methods": 1})
    out = fmt(src, "d.py", prof)
    assert out == src
    assert stream(out, "d.py") == stream(src, "d.py")
