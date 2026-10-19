import tkinter as tk

import pytest

from demo.service import Session
from tests.util import sample_path

tkinter = pytest.importorskip("tkinter")


@pytest.fixture(scope="module")
def have_display():
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no X server / display on this machine
        pytest.skip(f"no display available for tkinter: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(have_display, tmp_path):
    from demo.app import App

    built = App(have_display, session=Session(store_path=str(tmp_path / "profiles.json")))
    yield built
    have_display.update()


def test_window_builds_and_autoruns_extract(app):
    assert "indent" in app.out_profile.get("1.0", "end")
    assert "提取" in app.status.cget("text")
    assert app.listbox.size() == 1


def test_check_and_format_tabs_fill(app):
    app.run_check()
    assert "consistency" in app.out_check.get("1.0", "end") or "一致性" in app.status.cget("text")
    app.run_format()
    text = app.out_format.get("1.0", "end")
    assert "应用规则" in text or "已符合档案" in text


def test_editor_roundtrip_applies_changes(app):
    app.editor.focus_set()
    app._clear(app.editor)
    app.editor.insert("1.0", "int x = 1;\n")
    app.apply_editor()
    assert app.session.samples[0].text == "int x = 1;\n"


def test_load_sample_from_disk_and_delete(app, tmp_path):
    target = tmp_path / "s.c"
    target.write_text("int main(void) {\n    return 0;\n}\n", encoding="utf-8")
    before = app.listbox.size()
    app.session.add_files([str(target)])
    app._reload_list()
    assert app.listbox.size() == before + 1
    app._select(app.listbox.size() - 1)
    app._load_editor()
    assert "int main" in app.editor.get("1.0", "end")
    app.delete_sample()
    assert app.listbox.size() == before


def test_save_and_reload_user_profile(app):
    app.user_box.set("demo-user")
    app.run_save()
    assert "demo-user" in app.user_box.cget("values")
    app.user_box.set("demo-user")
    app.run_load()
    assert "档案库" in app.out_profile.get("1.0", "end")


def test_clear_resets_everything(app):
    app.run_extract()
    app.clear_samples()
    assert app.listbox.size() == 0
    assert app.out_profile.get("1.0", "end").strip() == ""


def test_real_sample_end_to_end_through_the_window(app):
    app.session.add_files([sample_path("mixed.cpp")])
    app._reload_list()
    app.lang_var.set("auto")
    app.run_extract()
    app.run_check()
    app.run_format()
    body = app.out_format.get("1.0", "end")
    assert "token 序列不变" in body
