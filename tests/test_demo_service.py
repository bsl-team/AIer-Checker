import os

import pytest

from demo.service import DemoError, Session
from tests.util import read_sample, sample_path


def test_session_starts_empty_and_asks_for_samples():
    s = Session()
    assert s.samples == []
    with pytest.raises(DemoError):
        s.extract()


def test_add_pasted_sample_and_extract_profile():
    s = Session()
    s.add_text("pasted.py", "def build(a, b):\n    return a + b\n\n\nx = 1\n")
    text = s.extract()
    assert s.profile.language == "python"
    assert "indent.size" in text and "width.limit" in text
    assert "quote.style" not in text.split("unset")[0]  # no string literals seen: stays a default
    assert "unset (family default used when formatting)" in text
    assert "提取" in s.status


def test_add_files_from_disk_keeps_path_and_encoding(tmp_path):
    target = tmp_path / "gbk.c"
    target.write_bytes("// 中文注释\nint main(void) { return 0; }\n".encode("gbk"))
    s = Session()
    assert s.add_files([str(target)]) == 1
    sample = s.samples[0]
    assert sample.from_file and sample.encoding == "gbk"
    assert "中文注释" in sample.text
    s.extract()
    assert s.profile.language == "c"


def test_add_files_ignores_missing_paths(tmp_path):
    s = Session()
    assert s.add_files([str(tmp_path / "nope.c")]) == 0
    assert "没有可读文件" in s.status


def test_update_text_and_remove(tmp_path):
    s = Session()
    s.add_text("a.c", "int x;\n")
    s.add_text("b.c", "int y;\n")
    s.update_text(0, "int x = 1;\n")
    assert s.samples[0].text == "int x = 1;\n"
    s.remove(1)
    assert len(s.samples) == 1
    with pytest.raises(DemoError):
        s.update_text(5, "x")


def test_check_reports_mixed_styles_inside_one_sample(tmp_path):
    store = str(tmp_path / "profiles.json")
    s = Session(store_path=store)
    s.add_text("mixed.cpp", read_sample("mixed.cpp"))
    text = s.check()
    assert "indent.type" in text and "mixed in file" in text
    assert s.report.findings


def test_check_again_a_stored_user_profile(tmp_path):
    store = str(tmp_path / "profiles.json")
    s = Session(store_path=store)
    s.add_file(sample_path("cpp_knr.cpp"))
    s.save_profile("bin")
    other = Session(store_path=store)
    other.add_file(sample_path("cpp_allman.cpp"))
    text = other.check(baseline_user="bin")
    assert "对照已存档案: bin" in text
    assert "differs from profile" in text


def test_save_and_load_profile_roundtrip(tmp_path):
    store = str(tmp_path / "profiles.json")
    s = Session(store_path=store)
    s.add_file(sample_path("go_style.go"))
    text = s.save_profile("gopher")
    assert store in text
    assert os.path.exists(store)
    loaded = Session(store_path=store)
    shown = loaded.load_profile("gopher")
    assert "tab" in shown
    assert loaded.profile.value("indent.type") == "tab"
    with pytest.raises(DemoError):
        loaded.load_profile("ghost")
    with pytest.raises(DemoError):
        loaded.save_profile("   ")


def test_users_lists_stored_profiles(tmp_path):
    store = str(tmp_path / "profiles.json")
    s = Session(store_path=store)
    s.add_file(sample_path("js_single.js"))
    s.save_profile("web")
    assert s.users() == ["web"]


def test_format_preview_uses_extracted_profile_and_produces_diff(tmp_path):
    s = Session(store_path=str(tmp_path / "p.json"))
    s.add_file(sample_path("cpp_knr.cpp"))   # the profile comes from the clean sample
    s.extract()
    s.add_text("messy.c", "int main()\n{\n\tint x=1;\n\treturn x;\n}\n")
    out = s.format_samples()
    assert "已符合档案，无需改动" in out          # the clean sample has nothing to fix
    assert "应用规则" in out and "---" in out and "+++" in out
    preview = s.preview_text(1)
    assert "int main() {" in preview
    assert "\t" not in preview
    assert "1/2" in s.status


def test_format_with_user_profile_when_current_is_missing(tmp_path):
    store = str(tmp_path / "profiles.json")
    seed = Session(store_path=store)
    seed.add_file(sample_path("cpp_knr.cpp"))
    seed.save_profile("bin")

    fresh = Session(store_path=store)
    fresh.add_text("t.cpp", "int main()\n{\n    return 0;\n}\n")
    with pytest.raises(DemoError):
        fresh.format_samples()  # no profile yet
    out = fresh.format_samples(source="user", user="bin")
    assert "int main() {" in fresh.preview_text(0)
    assert "brace.style" in out
    assert "1/1" in fresh.status


def test_unchanged_sample_says_so(tmp_path):
    s = Session(store_path=str(tmp_path / "p.json"))
    s.add_file(sample_path("cpp_knr.cpp"))
    s.extract()
    out = s.format_samples()
    assert "已符合档案，无需改动" in out
    assert s.entries[0].diff == ""


def test_write_back_only_works_for_file_samples(tmp_path):
    store = str(tmp_path / "p.json")
    s = Session(store_path=store)
    s.add_file(sample_path("cpp_knr.cpp"))
    s.extract()
    target = tmp_path / "messy.c"
    target.write_text("int main()\n{\n\treturn 0;\n}\n", encoding="utf-8")
    s.add_file(str(target))
    s.format_samples()
    with pytest.raises(DemoError):
        s.write_back(99)
    s.write_back(1)
    assert "int main() {" in target.read_text(encoding="utf-8")
    assert s.samples[1].text == s.entries[1].result.text


def test_write_back_refuses_pasted_samples(tmp_path):
    s = Session(store_path=str(tmp_path / "p.json"))
    s.add_text("t.c", "int main()\n{\n    return 0;\n}\n")
    s.extract()
    s.format_samples()
    with pytest.raises(DemoError):
        s.write_back(0)


def test_language_override_is_applied(tmp_path):
    s = Session(store_path=str(tmp_path / "p.json"))
    s.add_text("noext", "int main() {\n    return 0;\n}\n")
    s.language = "cpp"
    s.extract()
    assert s.profile.language == "cpp"
    s.language = "klingon"
    with pytest.raises(DemoError):
        s.extract()


def test_clear_samples_resets_results(tmp_path):
    s = Session(store_path=str(tmp_path / "p.json"))
    s.add_text("a.c", "int x;\n")
    s.extract()
    s.format_samples()
    s.clear_samples()
    assert s.samples == [] and s.entries == []
    assert "已清空样本" in s.status
