from cf.consistency import analyze, severity_for
from cf.document import build_document
from cf.extract import extract
from tests.util import read_sample, sample_path


def docs(*names):
    return [build_document(read_sample(n), sample_path(n)) for n in names]


def findings(rep, rule=None, kind=None):
    return [f for f in rep.findings if (rule is None or f.rule == rule) and (kind is None or f.kind == kind)]


def test_uniform_sample_has_no_intra_findings():
    rep = analyze(docs("cpp_knr.cpp"))
    assert rep.findings_for(sample_path("cpp_knr.cpp")) == []
    assert rep.files[0].score == 1.0
    assert rep.samples == 1


def test_mixed_sample_reports_every_axis_that_conflicts():
    rep = analyze(docs("mixed.cpp"))
    rules = {f.rule for f in findings(rep, kind="intra-file")}
    assert {"indent.type", "indent.size", "brace.style", "space.after_keyword", "blank.trailing_whitespace"} <= rules
    sev = {f.rule: f.severity for f in findings(rep)}
    assert sev["indent.type"] == "high"
    assert sev["brace.style"] == "medium"


def test_mixed_findings_carry_row_evidence():
    rep = analyze(docs("mixed.cpp"))
    tab = findings(rep, "indent.type")[0]
    assert tab.rows == [16]
    assert tab.expected == "space" and tab.actual == "tab"
    assert "tab" in tab.message
    sizes = {f.actual for f in findings(rep, "indent.size")}
    assert any(a.startswith("irregular:") for a in sizes)


def test_trailing_whitespace_is_low_severity_but_reported():
    rep = analyze(docs("mixed.cpp"))
    tw = findings(rep, "blank.trailing_whitespace")
    assert tw and tw[0].severity == "low"
    assert tw[0].rows == [17]


def test_cross_file_deviation_against_the_shared_profile():
    rep = analyze(docs("cpp_knr.cpp", "cpp_allman.cpp", "c_tabs.c"))
    assert rep.baseline.value("brace.style") == "attach"
    allman = sample_path("cpp_allman.cpp")
    brace = [f for f in findings(rep, "brace.style", "against-profile") if f.file == allman]
    assert brace and brace[0].expected == "attach" and brace[0].actual == "next"
    tabs = [f for f in findings(rep, "indent.type", "against-profile") if f.file == sample_path("c_tabs.c")]
    assert tabs and tabs[0].actual == "tab"
    knr = [f for f in rep.files if f.file == sample_path("cpp_knr.cpp")][0]
    assert knr.score > 0.95


def test_explicit_baseline_overrides_the_derived_one():
    base = extract(build_document(read_sample("cpp_allman.cpp"), sample_path("cpp_allman.cpp")))
    rep = analyze(docs("cpp_knr.cpp"), baseline=base)
    brace = findings(rep, "brace.style", "against-profile")
    assert brace and brace[0].expected == "next" and brace[0].actual == "attach"


def test_report_is_json_serialisable():
    rep = analyze(docs("mixed.cpp", "py_default.py"))
    data = rep.to_dict()
    assert data["samples"] == 2
    assert isinstance(data["score"], float)
    assert data["files"] and data["findings"]
    assert {r["rule"] for r in data["rules"]}


def test_rule_table_tracks_agreement_per_axis():
    rep = analyze(docs("cpp_knr.cpp", "cpp_allman.cpp"))
    row = [r for r in rep.rules if r["rule"] == "brace.style"][0]
    assert 0 < row["agreement"] < 1
    assert row["samples_observing"] == 2
    assert row["findings"] >= 1


def test_tab_file_and_space_profile_report_one_indent_conflict():
    rep = analyze(docs("cpp_knr.cpp", "c_tabs.c"))
    tabs = sample_path("c_tabs.c")
    rules = {f.rule for f in rep.findings if f.file == tabs and f.kind == "against-profile"}
    assert "indent.type" in rules
    assert "indent.size" not in rules  # 1 tab vs 4 spaces is a single conflict


def test_severity_helper_prefers_strict_rules():
    assert severity_for("indent.type", 0.96, "intra-file") == "high"
    assert severity_for("space.inside_parens", 0.95, "intra-file") == "low"
    assert severity_for("quote.style", 0.8, "against-profile") == "high"
