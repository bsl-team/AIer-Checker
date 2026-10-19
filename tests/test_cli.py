import io
import json
import os
import sys

import pytest

from cf.cli import main
from cf.document import build_document
from cf.format import token_identity
from tests.util import read_sample, sample_path


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "profiles.json")


def run(capsys, argv):
    code = main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_extract_reports_the_observed_rules(capsys, tmp_path):
    code, out, err = run(capsys, ["extract", sample_path("cpp_knr.cpp"), "--store", str(tmp_path / "s.json")])
    assert code == 0, err
    assert "indent.size" in out and "4" in out
    assert "brace.style" in out
    assert "measurements:" in out


def test_extract_json_is_the_profile_document(capsys, tmp_path):
    code, out, _err = run(capsys, ["extract", sample_path("py_default.py"), "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["language"] == "python"
    assert data["features"]["indent.size"]["value"] == "4"


def test_extract_out_writes_a_reusable_profile_file(capsys, tmp_path):
    target = tmp_path / "prof.json"
    code, _out, err = run(capsys, ["extract", sample_path("cpp_allman.cpp"), "--out", str(target)])
    assert code == 0, err
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["features"]["brace.style"]["value"] == "next"


def test_extract_with_user_saves_the_profile(capsys, store, tmp_path):
    code, out, err = run(capsys, ["extract", sample_path("cpp_knr.cpp"), "--user", "bin", "--store", store])
    assert code == 0, err
    assert "saved profile for bin" in out
    assert os.path.exists(store)


def test_profile_add_list_show_delete_rename(capsys, store):
    code, out, err = run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", store])
    assert code == 0, err
    assert "created profile 'bin'" in out
    code, out, _ = run(capsys, ["profile", "list", "--store", store])
    assert "bin" in out and "cpp" in out
    code, out, _ = run(capsys, ["profile", "show", "bin", "--store", store, "--json"])
    assert json.loads(out)["user"] == "bin"
    code, out, _ = run(capsys, ["profile", "rename", "bin", "binbin", "--store", store])
    assert "renamed" in out
    code, out, _ = run(capsys, ["profile", "delete", "binbin", "--store", store])
    assert "deleted" in out
    code, _out, err = run(capsys, ["profile", "delete", "ghost", "--store", store])
    assert code == 2 and "no profile named" in err


def test_profile_add_needs_samples(capsys, store):
    code, _out, err = run(capsys, ["profile", "add", "nobody", "--store", store])
    assert code == 2
    assert "needs source files" in err


def test_profile_update_merges_further_samples(capsys, store):
    run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", store])
    _code, out, _ = run(capsys, ["profile", "show", "bin", "--store", store, "--json"])
    first = json.loads(out)["features"]["brace.style"]["total"]
    code, out, err = run(capsys, ["profile", "update", "bin", sample_path("cpp_allman.cpp"), "--store", store])
    assert code == 0, err
    _code, out, _ = run(capsys, ["profile", "show", "bin", "--store", store, "--json"])
    merged = json.loads(out)
    assert merged["features"]["brace.style"]["total"] > first
    assert len(merged["samples"]) == 2
    assert merged["stats"]["rows"] == sum(s["rows"] for s in merged["samples"])


def test_profile_export_and_import_into_another_store(capsys, store, tmp_path):
    run(capsys, ["profile", "add", "bin", sample_path("js_single.js"), "--store", store])
    exported = tmp_path / "bin.json"
    code, _out, err = run(capsys, ["profile", "export", "bin", "--out", str(exported), "--store", store])
    assert code == 0, err
    other = str(tmp_path / "other.json")
    code, out, err = run(capsys, ["profile", "import", str(exported), "--store", other])
    assert code == 0, err
    assert "imported profile 'bin'" in out
    code, out, _ = run(capsys, ["profile", "list", "--store", other])
    assert "bin" in out


def test_check_reports_conflicts_and_can_fail_the_build(capsys, store):
    code, out, _err = run(capsys, ["check", sample_path("mixed.cpp")])
    assert code == 1  # a high severity finding exists
    assert "indent.type" in out and "mixed in file" in out
    code, out, _ = run(capsys, ["check", sample_path("mixed.cpp"), "--fail-on", "none"])
    assert code == 0
    code, out, _ = run(capsys, ["check", sample_path("cpp_knr.cpp")])
    assert code == 0
    assert "no style conflicts found" in out


def test_check_json_carries_files_rules_and_findings(capsys):
    code, out, _ = run(capsys, ["check", sample_path("mixed.cpp"), sample_path("cpp_allman.cpp"), "--json"])
    assert code == 1
    data = json.loads(out)
    assert data["samples"] == 2
    assert 0 <= data["score"] <= 1
    assert {f["rule"] for f in data["findings"]}
    assert all({"file", "severity", "rows", "message"} <= set(f) for f in data["findings"])
    assert [row["rule"] for row in data["rules"]]


def test_check_against_a_stored_profile(capsys, store):
    run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", store])
    code, out, _ = run(capsys, ["check", sample_path("cpp_allman.cpp"), "--user", "bin", "--store", store])
    assert code == 1
    assert "differs from profile" in out
    assert "brace.style" in out


def test_check_requires_a_known_user(capsys, store):
    code, _out, err = run(capsys, ["check", sample_path("cpp_knr.cpp"), "--user", "ghost", "--store", store])
    assert code == 2
    assert "no style profile stored" in err


def test_format_check_mode_does_not_touch_files(capsys, tmp_path):
    target = tmp_path / "messy.cpp"
    target.write_text(read_sample("mixed.cpp").replace("\n", os.linesep), encoding="utf-8")
    before = target.read_bytes()
    code, out, err = run(capsys, ["format", str(target), "--user", "nobody", "--check"])
    assert code == 2 and "no style profile" in err
    run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", str(tmp_path / "s.json")])
    code, out, _ = run(capsys, ["format", str(target), "--user", "bin", "--store", str(tmp_path / "s.json"), "--check"])
    assert code == 1
    assert "would change" in out
    assert target.read_bytes() == before


def test_format_write_rewrites_in_place_and_is_safe(capsys, tmp_path):
    target = tmp_path / "messy.cpp"
    target.write_text(read_sample("mixed.cpp"), encoding="utf-8")
    store = str(tmp_path / "s.json")
    run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", store])
    code, out, err = run(capsys, ["format", str(target), "--user", "bin", "--store", store, "--write", "--diff"])
    assert code == 0, err
    assert "reformatted" in out
    assert "---" in out and "+++" in out  # diff shown
    after = target.read_text(encoding="utf-8")
    assert "\t" not in after
    assert token_identity(after, str(target)) == token_identity(read_sample("mixed.cpp"), "mixed.cpp")
    code, out, _ = run(capsys, ["format", str(target), "--user", "bin", "--store", store, "--check"])
    assert code == 0
    assert "already matches the profile" in out


def test_format_reads_stdin_and_writes_stdout(capsys, monkeypatch, store):
    run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", store])
    src = "int main()\n{\n\treturn 0;\n}\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(src.encode())))
    code, out, err = run(capsys, ["format", "--user", "bin", "--store", store])
    assert code == 0, err
    assert out.startswith("int main() {")
    assert "\t" not in out
    assert token_identity(out, "x.c") == token_identity(src, "x.c")


def test_format_profile_file_source(capsys, tmp_path, monkeypatch):
    _code, out, _ = run(capsys, ["extract", sample_path("cpp_allman.cpp"), "--json"])
    prof = tmp_path / "p.json"
    prof.write_text(out, encoding="utf-8")
    src = "int main() {\n    return 0;\n}\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(src.encode())))
    code, text, err = run(capsys, ["format", "--profile", str(prof)])
    assert code == 0, err
    assert "int main()\n{" in text  # the allman profile puts the brace on its own row
    assert token_identity(text, "x.c") == token_identity(src, "x.c")


def test_format_rules_option_limits_changes(capsys, store, tmp_path):
    target = tmp_path / "a.c"
    target.write_text("int main()\n{\n\tint x=1;\n\treturn x;\n}\n", encoding="utf-8")
    run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", store])
    code, out, _ = run(capsys, ["format", str(target), "--user", "bin", "--store", store, "--rules", "indent.size", "--check"])
    assert code == 1
    assert "indent.size" in out
    assert "brace.style" not in out


def test_format_unknown_rule_is_reported(capsys, store):
    run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", store])
    code, _out, err = run(capsys, ["format", sample_path("cpp_knr.cpp"), "--user", "bin", "--store", store, "--rules", "not.a.rule"])
    assert code == 2
    assert "unknown rule" in err


def test_format_requires_a_profile(capsys):
    code, _out, err = run(capsys, ["format", sample_path("cpp_knr.cpp")])
    assert code == 2
    assert "--user NAME or --profile FILE" in err


def test_unknown_language_is_reported(capsys):
    code, _out, err = run(capsys, ["extract", sample_path("cpp_knr.cpp"), "--lang", "pascal"])
    assert code == 2
    assert "unknown language" in err


def test_missing_input_file_is_reported(capsys):
    code, _out, err = run(capsys, ["extract", "no/such/file.c"])
    assert code == 2
    assert "cannot read" in err


def test_gbk_encoded_source_is_read_and_written_back(capsys, tmp_path, store):
    text = "// 中文注释：求最大值\nint main() {\n\tint max = 0;\n\treturn max;\n}\n"
    target = tmp_path / "gbk.c"
    target.write_bytes(text.encode("gbk"))
    run(capsys, ["profile", "add", "bin", sample_path("c_tabs.c"), "--store", store])
    code, out, err = run(capsys, ["format", str(target), "--user", "bin", "--store", store, "--write"])
    assert code == 0, err
    assert target.read_bytes().decode("gbk").startswith("// 中文注释")
    assert "\t" in target.read_bytes().decode("gbk")  # tabs kept: the profile is tab-indented


def test_rules_and_languages_lists(capsys):
    code, out, _ = run(capsys, ["rules"])
    assert code == 0
    assert "applies" in out and "reports only" in out
    assert "never rewritten" in out
    code, out, _ = run(capsys, ["languages"])
    assert code == 0
    assert "python" in out and ".cpp" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert "0.1.0" in str(exc.value.code) or exc.value.code == 0


def test_store_is_created_on_demand(capsys, tmp_path):
    nested = tmp_path / "deep" / "profiles.json"
    code, out, err = run(capsys, ["profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", str(nested)])
    assert code == 0, err
    assert os.path.exists(nested)


def test_glob_patterns_are_expanded_by_the_kernel(capsys, tmp_path):
    for name in ("a.c", "b.c"):
        (tmp_path / name).write_text("int main(void) {\n    return 0;\n}\n", encoding="utf-8")
    pattern = str(tmp_path / "*.c").replace("\\", "/")
    code, out, err = run(capsys, ["extract", pattern, "--json"])
    assert code == 0, err
    data = json.loads(out)
    assert len(data["samples"]) == 2
    code, _out, err = run(capsys, ["extract", str(tmp_path / "*.nope")])
    assert code == 2 and "no files match" in err


def test_check_can_use_a_profile_file(capsys, tmp_path):
    _code, out, _ = run(capsys, ["extract", sample_path("cpp_allman.cpp"), "--json"])
    prof = tmp_path / "allman.json"
    prof.write_text(out, encoding="utf-8")
    code, out, _ = run(capsys, ["check", sample_path("cpp_knr.cpp"), "--profile", str(prof)])
    assert code == 1
    assert "differs from profile" in out
