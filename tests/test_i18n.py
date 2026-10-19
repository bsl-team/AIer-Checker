import json
import pathlib
import re

import pytest

from cf.cli import main, split_msg_lang
from cf.consistency import analyze
from cf.document import build_document
from cf.extract import extract
from cf.format import FORMAT_RULES, Formatter
from cf.i18n import CATALOG, FEATURE_SPECS_KEYS, get_locale, rule_label, set_locale, t
from cf.report import render_profile
from cf.store import ProfileStore, StoreError
from tests.util import read_sample, sample_path

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("cf", "demo")
# keys built from a variable or prefix at runtime, not a literal t("...")
DYNAMIC_KEYS = {"c.created", "c.updated"}  # chosen by profile add/update verb


def _literal_call_keys():
    """Every t("<literal>") key written directly in the source, including prefix-built w. keys."""
    found = set()
    for module_dir in SOURCE_DIRS:
        for path in (REPO / module_dir).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for key in re.findall(r"""\bt\(\s*["']([^"']+)["']\s*[,)]""", text):
                found.add(key)
            # t("w." + pattern) resolves against NOT_REWRITTEN keys at runtime
            if 't("w." +' in text or "t('w.' +" in text:
                from cf.format import NOT_REWRITTEN
                found.update(f"w.{pattern}" for pattern in NOT_REWRITTEN)
    return found


@pytest.fixture(autouse=True)
def english():
    set_locale("en")
    yield
    set_locale("en")


def run(capsys, argv):
    code = main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_every_message_has_both_languages():
    for key, entry in CATALOG.items():
        assert entry.get("en"), f"{key} has no english text"
        assert entry.get("zh"), f"{key} has no chinese text"
        literal = re.sub(r"\{[^}]*\}", "", entry["en"])  # column layouts need no translation
        if any(ch.isalpha() for ch in literal):
            assert entry["en"] != entry["zh"], f"{key} was not translated"


def test_every_referenced_key_exists():
    """A t("...") call that misses the catalog silently prints the raw key."""
    missing = sorted(_literal_call_keys() - set(CATALOG) - DYNAMIC_KEYS)
    assert not missing, f"t() called with unknown catalog keys: {missing}"


def test_every_rule_key_has_a_label():
    for key in FEATURE_SPECS_KEYS:
        assert rule_label(key) != key, f"no label for {key}"


def test_rule_labels_cover_what_the_formatter_can_apply():
    for key in FORMAT_RULES:
        assert key in FEATURE_SPECS_KEYS


def test_t_falls_back_to_english_then_key():
    set_locale("fr")
    assert t("r.no_findings") == "no style conflicts found"
    assert t("nope.not-a-key") == "nope.not-a-key"
    assert get_locale() == "en"


def test_profile_report_switches_language():
    profile = extract(build_document(read_sample("cpp_knr.cpp"), sample_path("cpp_knr.cpp")))
    set_locale("en")
    english_text = render_profile(profile)
    set_locale("zh")
    chinese = render_profile(profile)
    assert "confidence" in english_text and "置信度" not in english_text
    assert "置信度" in chinese
    assert "缩进单位" in chinese and "indent.size" in chinese  # the key stays visible


def test_consistency_messages_are_localised():
    docs = [build_document(read_sample("mixed.cpp"), sample_path("mixed.cpp"))]
    set_locale("en")
    en = analyze(docs)
    set_locale("zh")
    zh = analyze([build_document(read_sample("mixed.cpp"), sample_path("mixed.cpp"))])
    assert any("varies" in f.message or "is not a multiple" in f.message for f in en.findings)
    assert any(("混用" in f.message or "整数倍" in f.message) for f in zh.findings)
    # rule keys are language independent, so downstream filters keep working
    assert {f.rule for f in en.findings} == {f.rule for f in zh.findings}


def test_store_errors_are_localised(tmp_path):
    store = ProfileStore(str(tmp_path / "missing.json"))
    set_locale("en")
    with pytest.raises(StoreError) as exc:
        store.require("ghost")
    assert "no style profile stored" in str(exc.value)
    set_locale("zh")
    with pytest.raises(StoreError) as exc:
        store.require("ghost")
    assert "没有用户" in str(exc.value)


def test_cli_messages_use_the_flag(capsys, tmp_path):
    store = str(tmp_path / "profiles.json")
    _code, out, _err = run(capsys, ["-L", "zh", "extract", sample_path("py_default.py"), "--store", store])
    assert "档案：" in out and "置信度" in out
    _code, out, _err = run(capsys, ["extract", sample_path("py_default.py"), "--store", store])
    assert "profile:" in out


def test_msg_lang_flag_is_accepted_anywhere(capsys):
    code_a, out_a, _ = run(capsys, ["-L", "zh", "rules"])
    code_b, out_b, _ = run(capsys, ["rules", "-L", "zh"])
    assert code_a == code_b == 0
    assert out_a == out_b
    assert "会改写" in out_a


def test_split_msg_lang_forms():
    assert split_msg_lang(["-L", "zh", "rules"]) == (["rules"], "zh")
    assert split_msg_lang(["rules", "--msg-lang=zh"]) == (["rules"], "zh")
    assert split_msg_lang(["rules"]) == (["rules"], None)


def test_env_var_selects_locale(capsys, monkeypatch):
    monkeypatch.setenv("CF_LANG", "zh")
    code, out, err = run(capsys, ["languages"])
    assert code == 0, err
    assert "说明" in out


def test_help_text_follows_the_locale(capsys):
    with pytest.raises(SystemExit):
        main(["-L", "zh", "--help"])
    out = capsys.readouterr().out
    assert "代码风格内核" in out and "示例" in out
    with pytest.raises(SystemExit):
        main(["--help"])
    english = capsys.readouterr().out
    assert "Code style kernel" in english and "examples:" in english


def test_format_error_output_is_localised(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(capsys, ["-L", "zh", "profile", "add", "bin", sample_path("cpp_knr.cpp"), "--store", str(tmp_path / "p.json")])
    code, out, err = run(capsys, ["-L", "zh", "format", sample_path("cpp_allman.cpp"), "--user", "ghost", "--store", str(tmp_path / "p.json")])
    assert code == 2
    assert "没有用户" in err


def test_json_output_stays_language_free(capsys, tmp_path):
    _code, out, _err = run(capsys, ["-L", "zh", "check", sample_path("mixed.cpp"), "--json"])
    data = json.loads(out)
    assert {"score", "findings", "files", "rules"} <= set(data)
    assert all(isinstance(f["rule"], str) and f["rule"] == f["rule"].encode("ascii").decode() for f in data["findings"])


def test_public_api_exposes_locale_helpers():
    import cf

    assert callable(cf.set_locale) and callable(cf.t)
    assert cf.get_locale() in ("en", "zh")
