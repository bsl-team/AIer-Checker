import json
import os

import pytest

from cf.document import build_document
from cf.extract import extract
from cf.profile import Profile
from cf.store import ProfileStore, StoreError
from tests.util import read_sample, sample_path


def doc(name):
    return build_document(read_sample(name), sample_path(name))


@pytest.fixture
def store(tmp_path):
    return ProfileStore(str(tmp_path / "profiles.json"))


def test_add_user_creates_a_profile(store):
    prof = store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    assert prof.user == "bin"
    assert prof.value("indent.size") == 4
    assert store.get("bin").language == "cpp"
    assert store.users() == ["bin"]


def test_second_batch_merges_evidence(store):
    first = store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    support_before = first.feature("indent.size").total
    merged = store.add_from_samples("bin", [doc("cpp_allman.cpp")])
    assert merged.feature("indent.size").total > support_before
    assert merged.feature("brace.style").counts["attach"] > 0
    assert merged.feature("brace.style").counts["next"] > 0
    assert len(merged.samples) == 2


def test_replace_forgets_previous_evidence(store):
    store.add_from_samples("bin", [doc("cpp_knr.cpp"), doc("cpp_allman.cpp")])
    fresh = store.add_from_samples("bin", [doc("cpp_allman.cpp")], replace=True)
    assert len(fresh.samples) == 1
    assert fresh.feature("brace.style").counts.get("attach", 0) == 0


def test_persistence_roundtrip(store, tmp_path):
    store.add_from_samples("bin", [doc("py_default.py")])
    store.add_from_samples("mia", [doc("js_single.js")])
    store.flush()
    again = ProfileStore(str(tmp_path / "profiles.json"))
    assert again.users() == ["bin", "mia"]
    assert again.get("bin").value("indent.size") == 4
    assert again.get("mia").value("quote.style") == "single"
    assert again.get("bin").stats["max_line_width"] > 0


def test_store_file_shape(store, tmp_path):
    store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    store.flush()
    raw = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert set(raw["users"]) == {"bin"}
    assert "features" in raw["users"]["bin"] and "samples" in raw["users"]["bin"]


def test_summaries_list_profiles(store):
    store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    store.add_from_samples("mia", [doc("js_single.js"), doc("go_style.go")])
    rows = {s.user: s for s in store.summaries()}
    assert rows["bin"].samples == 1 and rows["mia"].samples == 2
    assert rows["bin"].language == "cpp"
    assert rows["bin"].confident_features >= 5
    assert rows["bin"].rows > 0


def test_remove_and_rename(store):
    store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    assert store.remove("ghost") is False
    assert store.remove("bin") is True
    store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    prof = store.rename("bin", "binbin")
    assert prof.user == "binbin"
    assert store.users() == ["binbin"]
    with pytest.raises(StoreError):
        store.rename("ghost", "x")


def test_require_raises_for_unknown_user(store):
    with pytest.raises(StoreError) as exc:
        store.require("nobody")
    assert "no style profile stored" in str(exc.value)


def test_export_import_roundtrip(store, tmp_path):
    store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    text = store.export("bin")
    other = ProfileStore(str(tmp_path / "other.json"))
    imported = other.import_json(text, user="copy")
    assert imported.value("brace.style") == "attach"
    assert imported.feature("indent.size").counts == store.get("bin").feature("indent.size").counts


def test_import_accepts_a_whole_store_file(store, tmp_path):
    store.add_from_samples("bin", [doc("cpp_knr.cpp")])
    store.add_from_samples("mia", [doc("go_style.go")])
    store.flush()
    text = (tmp_path / "profiles.json").read_text(encoding="utf-8")
    other = ProfileStore(str(tmp_path / "second.json"))
    assert other.import_json(text, user="mia").user == "mia"
    assert other.users() == ["mia"]


def test_corrupt_store_file_reports_the_path(store, tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("{not json", encoding="utf-8")
    broken = ProfileStore(str(path))
    with pytest.raises(StoreError) as exc:
        broken.users()
    assert str(path) in str(exc.value)


def test_damaged_profile_inside_store(store, tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"version": 1, "users": {"bin": {"samples": "nope"}}}), encoding="utf-8")
    broken = ProfileStore(str(path))
    with pytest.raises(StoreError):
        broken.users()


def test_path_resolution_prefers_argument_then_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_STORE", str(tmp_path / "from-env.json"))
    assert ProfileStore().path == os.path.abspath(str(tmp_path / "from-env.json"))
    assert ProfileStore(str(tmp_path / "arg.json")).path == os.path.abspath(str(tmp_path / "arg.json"))
    monkeypatch.delenv("CF_STORE")
    assert ProfileStore().path.endswith(os.path.join(".code-feature", "profiles.json"))


def test_add_rejects_blank_user(store):
    prof = extract(doc("cpp_knr.cpp"))
    prof.user = "   "
    with pytest.raises(StoreError):
        store.put(prof)


def test_profile_without_samples_can_still_be_stored(store):
    prof = Profile(user="empty")
    store.put(prof)
    assert store.get("empty").features == {}
    assert store.summaries()[0].samples == 0
