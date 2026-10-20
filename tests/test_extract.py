import json

import pytest

from cf.document import build_document
from cf.extract import extract, extract_all
from cf.naming import classify, roles
from cf.profile import Profile
from cf.tokens import Token
from tests.util import read_sample, sample_path


@pytest.fixture(scope="module")
def knr():
    return extract(build_document(read_sample("cpp_knr.cpp"), sample_path("cpp_knr.cpp")))


@pytest.fixture(scope="module")
def allman():
    return extract(build_document(read_sample("cpp_allman.cpp"), sample_path("cpp_allman.cpp")))


@pytest.fixture(scope="module")
def tabs_c():
    return extract(build_document(read_sample("c_tabs.c"), sample_path("c_tabs.c")))


@pytest.fixture(scope="module")
def py():
    return extract(build_document(read_sample("py_default.py"), sample_path("py_default.py")))


@pytest.fixture(scope="module")
def js():
    return extract(build_document(read_sample("js_single.js"), sample_path("js_single.js")))


@pytest.fixture(scope="module")
def go():
    return extract(build_document(read_sample("go_style.go"), sample_path("go_style.go")))


@pytest.fixture(scope="module")
def messy():
    return extract(build_document(read_sample("mixed.cpp"), sample_path("mixed.cpp")))


def test_classify_names():
    assert classify("calculate_total") == "snake_case"
    assert classify("calculateTotal") == "camelCase"
    assert classify("MaxParts") == "PascalCase"
    assert classify("MAX_PARTS") == "UPPER_SNAKE_CASE"
    assert classify("x") is None
    assert classify("sum") == "lowercase"  # cannot tell snake from camel, but says "not capitalised"
    assert classify("__init__") is None
    assert classify("_private_helper") == "snake_case"


def test_lowercase_names_outvote_a_couple_of_capitalised_ones():
    # a bare lowercase word used to be dropped entirely, so two ``End``/``Flag`` arrays decided the
    # whole role was PascalCase in code that names everything in lowercase
    src = (
        "int main(){\n"
        "    int n=1, ans=0, flag=0, sum=0, cnt=0;\n"
        "    int End[10];\n"
        "    bool Flag=true;\n"
        "    return n+ans+flag+sum+cnt+End[0]+Flag;\n"
        "}\n"
    )
    prof = extract(build_document(src, "t.cpp"))
    variable = prof.feature("naming.variable")
    assert variable.counts["lowercase"] == 4  # ans, flag, sum, cnt (``n`` is one letter: no evidence)
    assert prof.value("naming.variable") == "lowercase"


def test_lowercase_words_fold_into_the_convention_the_role_already_shows():
    src = (
        "int main(){\n"
        "    int someValue=1, other=2, totalSum=3;\n"
        "    return someValue+other+totalSum;\n"
        "}\n"
    )
    prof = extract(build_document(src, "c.cpp"))
    # ``other`` is one lowercase word: it joins camelCase, the convention this role shows
    counts = prof.feature("naming.variable").counts
    assert "lowercase" not in counts and counts["camelCase"] == 3


def test_knr_indent_brace_and_spacing(knr):
    assert knr.language == "cpp"
    assert knr.value("indent.type") == "space"
    assert knr.value("indent.size") == 4
    assert knr.value("brace.style") == "attach"
    assert knr.value("space.after_keyword") is True
    assert knr.value("space.before_call_paren") is False
    assert knr.value("space.inside_parens") is False
    assert knr.value("naming.type") == "PascalCase"
    assert knr.confidence("indent.size") > 0.7


def test_allman_braces_detected_as_next_line(allman):
    assert allman.value("brace.style") == "next"
    assert allman.value("indent.size") == 4
    assert allman.confidence("brace.style") > 0.5


def test_tab_indented_c_file(tabs_c):
    assert tabs_c.value("indent.type") == "tab"
    assert tabs_c.value("indent.size") == 1
    assert tabs_c.value("naming.macro") == "UPPER_SNAKE_CASE"
    assert tabs_c.value("space.after_keyword") is True


def test_python_profile_uses_ast_and_tokenize(py):
    assert py.family == "python"
    assert py.language == "python"
    assert py.value("indent.type") == "space"
    assert py.value("indent.size") == 4
    assert py.value("quote.style") == "double"
    assert py.value("blank.between_functions") == 2
    assert py.value("naming.function") == "snake_case"
    assert py.value("naming.type") == "PascalCase"
    assert py.value("comment.doc_style") == "docstring"
    # python has no block braces, so the brace axis must stay unobserved
    assert py.feature("brace.style") is None or py.feature("brace.style").total == 0


def test_js_profile(js):
    assert js.value("indent.size") == 2
    assert js.value("quote.style") == "single"
    assert js.value("semicolon.required") is True
    # the file declares DEFAULT_NAME in UPPER_SNAKE but also two lowercase consts, so the
    # majority reading is "lowercase" with UPPER_SNAKE_CASE kept visible as the deviation
    constant = js.feature("naming.constant")
    assert constant.counts["UPPER_SNAKE_CASE"] == 1
    assert js.value("naming.constant") == "lowercase"
    assert js.confidence("naming.constant") < 0.5
    assert js.value("layout.trailing_comma") is True


def test_go_profile(go):
    assert go.value("indent.type") == "tab"
    assert go.value("naming.function") == "camelCase"
    assert go.value("naming.type") == "PascalCase"
    assert go.value("semicolon.required") is None or go.confidence("semicolon.required") == 0.0


def test_inconsistent_sample_keeps_evidence_of_deviations(messy):
    brace = messy.feature("brace.style")
    assert brace.value == "attach"
    assert brace.counts.get("next") == 3
    assert messy.feature("indent.type").counts.get("tab") == 1
    assert messy.feature("indent.size").counts.get("4") == 25
    kw = messy.feature("space.after_keyword")
    assert kw.value == "true" and kw.counts.get("false") == 1
    assert messy.stats["max_line_width"] > messy.value("width.limit") or messy.stats["over_limit_rows"] == 0


def test_outlier_rows_point_at_the_offending_lines(messy):
    rows = dict(messy.feature("brace.style").outlier_rows())
    assert "next" in rows and rows["next"]
    line = rows["next"][0]
    assert messy.samples[0].path
    text = build_document(read_sample("mixed.cpp"), sample_path("mixed.cpp")).row(line).raw
    assert text.strip().startswith("{")


def test_extract_all_merges_conflicting_samples():
    docs = [
        build_document(read_sample("cpp_knr.cpp"), sample_path("cpp_knr.cpp")),
        build_document(read_sample("cpp_allman.cpp"), sample_path("cpp_allman.cpp")),
    ]
    merged = extract_all(docs)
    brace = merged.feature("brace.style")
    assert brace.counts["attach"] > 0 and brace.counts["next"] > 0
    assert brace.confidence < 1.0
    assert len(merged.samples) == 2
    assert merged.stats["samples"] == 2


def test_profile_json_roundtrip():
    docs = [build_document(read_sample("cpp_knr.cpp"), sample_path("cpp_knr.cpp"))]
    prof = extract_all(docs)
    prof.user = "tester"
    data = json.loads(json.dumps(prof.to_dict()))
    back = Profile.from_dict(data)
    assert back.user == "tester"
    assert back.value("indent.size") == 4
    assert back.feature("brace.style").counts == prof.feature("brace.style").counts
    assert back.stats["max_line_width"] == prof.stats["max_line_width"]


def test_empty_input_still_produces_defaults():
    prof = extract_all([])
    eff = prof.effective()
    assert prof.value("indent.size") is None
    assert eff["indent.size"][0] == 4
    assert eff["space.after_keyword"][0] is True


def test_fragment_without_balanced_braces_is_analysable():
    src = "int main() {\n    printf(\"%d\", x);\n"
    prof = extract(build_document(src, "fragment.c"))
    assert prof.value("indent.size") == 4
    assert prof.feature("brace.style").value == "attach"


def test_indent_unit_prefers_the_level_fitted_step():
    from cf.extract import _indent_unit

    assert _indent_unit([(4, 1), (8, 2), (12, 3), (8, 2), (4, 1)]) == 4
    assert _indent_unit([(4, 2), (8, 3), (12, 4)]) == 4  # namespace bodies not indented
    assert _indent_unit([(2, 1), (4, 2), (6, 3), (4, 2)]) == 2
    assert _indent_unit([(1, 1), (2, 2), (3, 3), (1, 1)]) == 1  # tabs
    assert _indent_unit([(8, 1), (16, 2)]) == 8
    assert _indent_unit([]) == 4
    assert _indent_unit([(3, 1), (3, 2), (3, 3)]) in (3, 6)


def test_colon_style_is_measured_as_four_values():
    from cf.extract import colon_style

    def colon(before_gap):
        return Token(kind="punct", text=":", row=1, col=3, end_row=1, end_col=4, gap=before_gap)

    def following(after_gap):
        return Token(kind="id", text="b", row=1, col=5, end_row=1, end_col=6, gap=after_gap)

    assert colon_style(colon(1), following(1)) == "spaced"
    assert colon_style(colon(0), following(1)) == "after"
    assert colon_style(colon(1), following(0)) == "before"
    assert colon_style(colon(0), following(0)) == "tight"


def test_dict_and_range_for_colons_are_measured_but_slices_are_not():
    from cf.extract import colon_context

    assert colon_context(braces=1, parens=0, brackets=0, row_is_control=False)  # dict literal
    assert colon_context(braces=0, parens=1, brackets=0, row_is_control=True)  # range-for
    assert not colon_context(braces=0, parens=0, brackets=1, row_is_control=False)  # slice
    assert not colon_context(braces=0, parens=1, brackets=0, row_is_control=False)  # annotation


def test_namespace_body_indentation_is_its_own_axis():
    indented = 'namespace n {\n    void f() {\n        int x = 1;\n    }\n}\n'
    flush = 'namespace n {\nvoid f() {\n    int x = 1;\n}\n}\n'
    assert extract(build_document(indented, "i.cpp")).value("indent.namespace_body") == "indent"
    assert extract(build_document(flush, "f.cpp")).value("indent.namespace_body") == "none"


def test_low_count_structural_rules_reach_a_usable_confidence():
    src = "namespace n {\nclass C {\npublic:\n    void f() {}\n};\n}\n"
    prof = extract(build_document(src, "n.cpp"))
    assert prof.confidence("indent.namespace_body") >= 0.5
    assert prof.confidence("brace.empty_block") >= 0.5


def test_single_observation_of_a_common_rule_stays_weak():
    prof = extract(build_document("int main() {\n    return 1 ;\n}\n", "s.c"))
    assert prof.confidence("space.before_semicolon") < 0.5


def test_defaults_fill_unobserved_features():
    src = "x = 1\n"
    prof = extract(build_document(src, "a.py"))
    eff = prof.effective()
    assert prof.confidence("indent.size") == 0.0
    assert eff["indent.size"][0] == 4
    assert eff["indent.size"][2] is False  # not observed, family default


def test_template_angle_spacing_is_measured_separately_from_comparisons():
    src = (
        "int main() {\n"
        "    vector<int> tight;\n"
        "    vector <int> spaced;\n"
        "    for (int i = 0; i < n; i++) if (a < b) tight = a;\n"
        "}\n"
    )
    prof = extract(build_document(src, "t.cpp"))
    before = prof.feature("space.before_template_angle")
    assert before is not None and before.total == 2  # the two declarations only
    assert prof.value("space.inside_template_angle") is False
    # the `i < n` and `a < b` comparisons are operator spacing, not a template
    assert prof.feature("space.around_operators") is not None


def test_template_angle_recognition_rejects_comparison_shapes():
    src = "int main(){ if(a<b&&c<=d) x = n >> 2; vector<int> v; vector<vector<int>> w; }\n"
    doc = build_document(src, "r.cpp")
    angles = doc.template_angles()
    texts = {doc.tokens[i].text for i in angles}
    assert texts == {"<"}  # only template openers, never the comparison or shift operators
    assert len(angles) == 3  # vector<, plus the outer and inner of vector<vector<int>>


def test_knr_sample_uses_tight_template_angles(knr):
    assert knr.value("space.before_template_angle") is False
    assert knr.value("space.inside_template_angle") is False


def test_constants_are_marked_by_keyword_macro_or_enum():
    src = (
        "#define N 5\n"
        "#define SQ(x) ((x)*(x))\n"
        "enum { A = 1, B = 2 };\n"
        "const int a = 1;\n"
        "constexpr int b = 2;\n"
        "int main(){ return a + b + N + A + B + SQ(2); }\n"
    )
    prof = extract(build_document(src, "t.cpp"))
    marker = prof.feature("constant.marker")
    assert marker.counts["const"] == 1
    assert marker.counts["constexpr"] == 1
    assert marker.counts["define"] == 1  # only the object-like #define, not SQ(x)
    assert marker.counts["enum"] == 2  # A and B
    assert "SQ" not in marker.rows.get("define", [])


def test_plain_variables_are_not_constant_evidence():
    prof = extract(build_document("int a = 5;\nint b = 6;\nint main(){return a+b;}\n", "t.cpp"))
    assert prof.feature("constant.marker") is None  # no const / #define / enum at all


def test_const_placement_before_and_after_type():
    before = extract(build_document("const int a=1;\nconst char* s;\nint main(){return 0;}\n", "b.cpp"))
    assert before.value("constant.placement") == "before-type"
    after = extract(build_document("int const a=1;\nMyClass const& m;\nint main(){return 0;}\n", "a.cpp"))
    assert after.value("constant.placement") == "after-type"


def test_trailing_member_const_is_not_a_placement_signal():
    prof = extract(build_document("struct S { int f() const; void g() const; };\n", "s.cpp"))
    assert prof.feature("constant.placement") is None


def test_comma_join_vs_semicolon_between_calls():
    joined = extract(build_document("int main(){ cin.tie(0),cout.tie(0); }\n", "j.cpp"))
    assert joined.value("statement.comma_join") is True
    separate = extract(build_document("int main(){ cin.tie(0); cout.tie(0); }\n", "s.cpp"))
    assert separate.value("statement.comma_join") is False


def test_comma_join_ignores_declarations_arguments_and_assignments():
    for src in (
        "int main(){ int a=1, b=2; }\n",        # declarator list
        "int main(){ printf(\"%d %d\", a, b); }\n",  # arguments inside a call
        "int main(){ x = f(); y = g(); }\n",    # assignments, not lone calls
        "int main(){ v[0], v[1]; }\n",          # subscript expressions
    ):
        assert extract(build_document(src, "n.cpp")).feature("statement.comma_join") is None, src


def test_comma_join_chained_operands_each_count():
    prof = extract(build_document("int main(){ a(),b(),c(); }\n", "c.cpp"))
    feat = prof.feature("statement.comma_join")
    assert feat.counts["true"] == 2  # a-b and b-c both joined


def test_clike_function_and_method_names_are_detected():
    src = (
        "int solve_case(int n) { return n; }\n"
        "void build_graph(int e) { }\n"
        "struct Point {\n"
        "    int dist_here() { return 0; }\n"
        "};\n"
        "int main() { return solve_case(1); }\n"
    )
    doc = build_document(src, "t.cpp")
    r = roles(doc)
    assert [n for _row, n in r["function"]] == ["solve_case", "build_graph"]
    assert [n for _row, n in r["method"]] == ["dist_here"]
    prof = extract(doc)
    assert prof.value("naming.function") == "snake_case"
    assert prof.value("naming.method") == "snake_case"


def test_main_and_control_bodies_are_not_function_name_evidence():
    src = "int main(){\n\tif(x){ y=1; }\n\twhile(a){ b=2; }\n\treturn 0;\n}\n"
    r = roles(build_document(src, "m.cpp"))
    assert r["function"] == []          # main is fixed by the language; if/while are not names
    assert extract(build_document(src, "m.cpp")).feature("naming.function") is None


def test_prototypes_without_a_body_are_not_counted():
    src = "int compute_thing(int x);\nint main(){ return 0; }\n"
    assert roles(build_document(src, "p.cpp"))["function"] == []


def test_java_methods_and_js_functions_get_names():
    java = build_document("class A {\n void doThing(){}\n int other(){ return 1; }\n}\n", "A.java")
    assert [n for _r, n in roles(java)["method"]] == ["doThing", "other"]
    assert roles(java)["function"] == []
    js = build_document("function doThing(a){ return a; }\nfunction other_fn(){ return 1; }\n", "a.js")
    assert [n for _r, n in roles(js)["function"]] == ["doThing", "other_fn"]
