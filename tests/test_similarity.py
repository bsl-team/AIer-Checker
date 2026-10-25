import json
import os
import subprocess
import sys

import pytest

from cf.document import build_document
from cf.similarity import (
    MatchSpan,
    Similarity,
    SimilarityError,
    Submission,
    compare_documents,
    find_duplicates,
    fingerprint,
    load_manifest,
    normalize,
)
from tests.util import read_sample, sample_path

BASE = read_sample("cpp_knr.cpp")


def renamed(text):
    return text.replace("values", "nums").replace("total", "acc").replace("Solver", "Kit")


def docs(*pairs):
    return [build_document(text, name) for name, text in pairs]


def compare(a, b, **kw):
    return compare_documents(build_document(a, "a.cpp"), build_document(b, "b.cpp"), **kw)


def test_identical_files_score_one_and_carry_evidence():
    sim = compare(BASE, BASE, min_tokens=40)
    assert sim.jaccard == 1.0 and sim.containment == 1.0
    assert sim.qualifies and sim.shared_ngrams > 3
    assert sim.evidence and sim.evidence[0].a_lines == sim.evidence[0].b_lines


def test_renaming_identifiers_does_not_change_the_score():
    sim = compare(BASE, renamed(BASE), min_tokens=40)
    assert sim.jaccard == 1.0, sim


def test_whitespace_and_comment_changes_do_not_change_the_score():
    touched = BASE.replace("    int total = 0;", "// a note\n    int   total   =   0;\n")
    sim = compare(BASE, touched, min_tokens=40)
    assert sim.jaccard > 0.9, sim.jaccard


def test_partial_copy_shows_up_in_containment_not_jaccard():
    keep = BASE[: BASE.index("    void report")]
    sim = compare(BASE, keep, min_tokens=20)
    assert sim.containment > sim.jaccard
    assert sim.containment_ba > 0.8, sim.containment_ba  # the truncated copy is contained in the original
    assert sim.qualifies


def test_unrelated_code_stays_low():
    sim = compare(BASE, read_sample("go_style.go"), min_tokens=20)
    assert sim.jaccard < 0.5 and sim.containment < 0.8
    assert not sim.qualifies


def test_boilerplate_heavy_corpus_does_not_get_flagged():
    boiler = "#include <bits/stdc++.h>\nusing namespace std;\nint main(){\n"
    tails = ["    int n; cin >> n; cout << n;\n}\n", "    int m; cin >> m; cout << m;\n}\n",
             "    int k; cin >> k; cout << k;\n}\n"]
    subs = [Submission(path=f"s{i}", id=f"s{i}", user=f"u{i}", problem="P",
                       text=boiler + tails[i % 3]) for i in range(12)]
    filtered = find_duplicates(subs, min_tokens=20)
    assert filtered.pairs == []
    assert any(w.startswith("boilerplate-filtered") for w in filtered.warnings), filtered.warnings
    kept = find_duplicates(subs, min_tokens=20, keep_common=True)
    assert kept.pairs, "without the filter the template text does match"


def test_short_submission_is_warned_about_and_not_clustered():
    subs = [Submission(path="big", id="big", user="a", text=BASE),
            Submission(path="small", id="small", user="b", text="int main(){return 0;}\n")]
    report = find_duplicates(subs, min_tokens=40)
    assert any(w.startswith("too-short:small") for w in report.warnings), report.warnings
    assert report.clusters == []


def test_empty_and_comment_only_files_do_not_crash():
    subs = [Submission(path="e1", id="e1", user="a", text=""),
            Submission(path="e2", id="e2", user="b", text="// nothing\n/* here */\n"),
            Submission(path="e3", id="e3", user="c", text="int main(){ return 0; }\n")]
    report = find_duplicates(subs, min_tokens=1)
    assert all(not p.qualifies for p in report.pairs)


def test_problem_and_language_gating(tmp_path):
    a = tmp_path / "a.cpp"; a.write_text(BASE, encoding="utf-8")
    b = tmp_path / "b.cpp"; b.write_text(renamed(BASE), encoding="utf-8")
    p = tmp_path / "p.py"; p.write_text("def solve(values):\n    return values\n", encoding="utf-8")
    subs = [Submission(str(a), "a", "u1", "P1"), Submission(str(b), "b", "u2", "P2"),
            Submission(str(p), "py", "u3", "P1")]
    strict = find_duplicates(subs, min_tokens=40)
    assert strict.pairs == []
    looser = find_duplicates(subs, min_tokens=40, same_problem=False, same_language=False)
    assert any({p.a, p.b} == {"a", "b"} for p in looser.pairs), looser.pairs


def test_cross_user_and_same_user_marks():
    subs = [Submission(path="x1", id="x1", user="alice", text=BASE),
            Submission(path="x2", id="x2", user="alice", text=renamed(BASE)),
            Submission(path="x3", id="x3", user="bob", text=renamed(BASE))]
    report = find_duplicates(subs, min_tokens=40)
    by = {(p.a, p.b): p for p in report.pairs}
    assert by[("x1", "x2")].cross_user is False
    assert by[("x2", "x3")].cross_user is True
    only_cross = find_duplicates(subs, min_tokens=40, include_same_user=False)
    assert all(p.cross_user for p in only_cross.pairs) and only_cross.pairs


def test_python_block_structure_is_part_of_the_fingerprint():
    flat = "def f(v):\n    return v + 1\n"
    nested = "def f(v):\n    if v:\n        return v + 1\n    return 0\n"
    assert fingerprint(build_document(flat, "a.py")).hashes != fingerprint(build_document(nested, "b.py")).hashes
    assert any(nt.kind == "BLOCK" for nt in normalize(build_document(nested, "b.py")))


def test_scores_survive_line_endings_and_encoding(tmp_path):
    unix = tmp_path / "u.cpp"; unix.write_bytes(BASE.encode("utf-8"))
    dos = tmp_path / "d.cpp"; dos.write_bytes(BASE.replace("\n", "\r\n").encode("utf-8"))
    gbk = tmp_path / "g.cpp"; gbk.write_bytes(("%s\n// 中文注释\n" % BASE).encode("gbk"))
    from cf.similarity import read_document

    a = fingerprint(read_document(Submission(str(unix), "u")))
    b = fingerprint(read_document(Submission(str(dos), "d")))
    c = fingerprint(read_document(Submission(str(gbk), "g")))
    assert a.hashes == b.hashes == c.hashes, (a.tokens, b.tokens, c.tokens)


def test_fingerprints_are_stable_across_processes(tmp_path):
    a = tmp_path / "a.cpp"
    b = tmp_path / "b.cpp"
    a.write_text(BASE, encoding="utf-8")
    b.write_text(renamed(BASE), encoding="utf-8")
    script = """
import json, sys
from cf.similarity import Submission, find_duplicates

subs = [Submission(sys.argv[1], "a", "u1"), Submission(sys.argv[2], "b", "u2")]
report = find_duplicates(subs, min_tokens=40)
print(json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False))
"""
    out = []
    for seed in ("0", "1"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, "-c", script, str(a), str(b)],
            capture_output=True, text=True, encoding="utf-8", cwd=os.getcwd(), env=env,
        )
        assert result.returncode == 0, result.stderr
        out.append(json.loads(result.stdout))
    assert out[0]["pairs"], "the pair should be found"
    assert out[0] == out[1]


def test_manifest_paths_resolve_relative_and_duplicate_ids_are_rejected(tmp_path):
    (tmp_path / "s.cpp").write_text(BASE, encoding="utf-8")
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"submissions": [{"id": "x", "path": "s.cpp", "user": "u"}]}), encoding="utf-8")
    subs = load_manifest(str(m))
    assert subs[0].path.endswith("s.cpp") and os.path.isabs(subs[0].path)
    m.write_text(json.dumps({"submissions": [{"id": "x", "path": "s.cpp"}, {"id": "x", "path": "s.cpp"}]}),
                 encoding="utf-8")
    with pytest.raises(SimilarityError):
        load_manifest(str(m))
    m.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(SimilarityError):
        load_manifest(str(m))


def test_results_are_serialisable_and_json_friendly():
    sim = compare(BASE, renamed(BASE), min_tokens=40)
    payload = sim.to_dict()
    assert json.dumps(payload) and payload["qualifies"] is True
    assert isinstance(sim.evidence[0], MatchSpan)
    report = find_duplicates([Submission(path="p", id="p", text=BASE),
                              Submission(path="q", id="q", text=renamed(BASE))], min_tokens=40)
    doc = report.to_dict()
    assert doc["version"] == 1 and doc["params"]["k"] == 7
    assert {s["id"] for s in doc["submissions"]} == {"p", "q"}
