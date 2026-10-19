"""A small tkinter window over the code style kernel: python -m demo.app"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

if __package__ in (None, ""):  # allow `python demo/app.py` as well as `python -m demo.app`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf.i18n import ENV_VAR, get_locale, set_locale
from cf.tokens import SPEC_BY_NAME
from demo.service import DemoError, Session

LOCALES = (("zh", "中文"), ("en", "English"))

MONO = ("Consolas", 10)
SAMPLE_START = '''// 粘在这里的样本可以直接提取风格
int messy(int n)
{
        int acc = 0;
    if(n > 0) {
        acc = n;
    }
    return acc;
}
'''


class App:
    def __init__(self, root: tk.Tk, session: Session | None = None):
        self.root = root
        self.session = session or Session()
        root.title("代码风格内核 Demo")
        root.geometry("1120x720")
        root.minsize(900, 560)
        set_locale(os.environ.get(ENV_VAR) or "zh")  # the window's own chrome is Chinese already
        self.pages: dict[str, ttk.Frame] = {}
        self.cmd = {
            "extract": self._guard(self.run_extract),
            "check": self._guard(self.run_check),
            "format": self._guard(self.run_format),
            "write": self._guard(self.run_write_back),
            "load": self._guard(self.run_load),
            "save": self._guard(self.run_save),
            "pick": self._guard(self.pick_files),
            "paste": self._guard(self.new_paste),
            "delete": self._guard(self.delete_sample),
            "clear": self._guard(self.clear_samples),
            "apply": self._guard(self.apply_editor),
            "locale": self._guard(self.switch_locale),
        }
        self._build_top()
        self._build_body()
        self._build_bottom()
        self.refresh_users()
        self.session.add_text("pasted.cpp", SAMPLE_START)
        self._reload_list()
        self._select(0)
        self.cmd["extract"]()

    # -- widgets ----------------------------------------------------------
    def _build_top(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="风格档案用户").pack(side="left")
        self.user_var = tk.StringVar()
        self.user_box = ttk.Combobox(bar, textvariable=self.user_var, width=16, values=[])
        self.user_box.pack(side="left", padx=(6, 4))
        self._button(bar, "载入", "load")
        self._button(bar, "保存为新档案", "save")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(bar, text="语言").pack(side="left")
        self.lang_var = tk.StringVar(value="auto")
        langs = ["auto"] + sorted(SPEC_BY_NAME)
        ttk.Combobox(bar, textvariable=self.lang_var, values=langs, width=11, state="readonly").pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="提示语").pack(side="left", padx=(10, 4))
        self.locale_var = tk.StringVar(value=get_locale())
        box = ttk.Combobox(bar, textvariable=self.locale_var, values=[code for code, _ in LOCALES], width=5, state="readonly")
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _e: self.cmd["locale"]())
        ttk.Label(bar, text=f"档案库: {self.session.store.path}").pack(side="right")

    def _build_body(self) -> None:
        pane = ttk.Panedwindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        left = ttk.Labelframe(pane, text="代码样本", padding=6)
        pane.add(left, weight=1)
        self.listbox = tk.Listbox(left, height=6, activestyle="dotbox", font=MONO, exportselection=False)
        self.listbox.pack(fill="x")
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._load_editor())
        row = ttk.Frame(left)
        row.pack(fill="x", pady=4)
        self._button(row, "添加文件", "pick")
        self._button(row, "新建粘贴样本", "paste")
        self._button(row, "删除所选", "delete")
        self._button(row, "清空", "clear")
        editor_bar = ttk.Frame(left)
        editor_bar.pack(fill="x")
        ttk.Label(editor_bar, text="样本内容（可编辑）").pack(side="left")
        self._button(editor_bar, "应用到样本", "apply")
        self.editor = self._text(left, wrap="none")

        right = ttk.Labelframe(pane, text="结果", padding=6)
        pane.add(right, weight=2)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.out_profile = self._output_tab("风格档案")
        self.out_check = self._output_tab("一致性检查")
        self.out_format = self._output_tab("格式化预览")

    def _build_bottom(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x")
        self._button(bar, "提取风格", "extract")
        self._button(bar, "一致性检查", "check")
        self._button(bar, "格式化预览", "format")
        self._button(bar, "写回所选文件", "write")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.baseline_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="对照已存档案", variable=self.baseline_var).pack(side="left")
        self.observed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="只用已观测规则", variable=self.observed_var).pack(side="left", padx=(6, 0))
        self.wrap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="允许长行换行", variable=self.wrap_var).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="置信度下限").pack(side="left", padx=(10, 2))
        self.min_conf = ttk.Spinbox(bar, from_=0.0, to=1.0, increment=0.05, width=5)
        self.min_conf.set(0.5)
        self.min_conf.pack(side="left")
        self.status = ttk.Label(self.root, text=self.session.status, padding=(10, 4), anchor="w")
        self.status.pack(fill="x")

    def _button(self, parent, text: str, key: str) -> ttk.Button:
        """Buttons bind to the guarded command registry, so every entry point reports errors alike."""
        btn = ttk.Button(parent, text=text, command=self.cmd[key])
        btn.pack(side="left", padx=(0, 6), pady=1)
        return btn

    def _output_tab(self, title: str) -> tk.Text:
        page = ttk.Frame(self.tabs)
        self.tabs.add(page, text=title)
        self.pages[title] = page
        return self._text(page, state="disabled")

    def _focus_tab(self, title: str) -> None:
        self.tabs.select(self.pages[title])

    def _text(self, parent, **kwargs) -> tk.Text:
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True, pady=(4, 0))
        widget = tk.Text(holder, font=MONO, undo=True, **kwargs)
        scroll = ttk.Scrollbar(holder, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        widget.pack(side="left", fill="both", expand=True)
        return widget

    # -- helpers ----------------------------------------------------------
    def _guard(self, action):
        def wrapper(*args):
            try:
                self.root.configure(cursor="watch")
                self.root.update_idletasks()
                action(*args)
            except DemoError as exc:
                messagebox.showwarning("无法执行", str(exc), parent=self.root)
                self.set_status(f"未执行: {exc}")
            finally:
                self.root.configure(cursor="")
                self.set_status(self.session.status)

        return wrapper

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _selected(self) -> int:
        picks = self.listbox.curselection()
        if not picks:
            raise DemoError("请先在左侧选中一个样本")
        return picks[0]

    def _select(self, index: int) -> None:
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.activate(index)

    def _reload_list(self) -> None:
        self.listbox.delete(0, "end")
        for sample in self.session.samples:
            tag = sample.path or f"粘贴: {sample.name}"
            self.listbox.insert("end", f"{sample.name}   [{tag}]")
        if self.session.samples:
            self._select(min(self._selected_or_zero(), len(self.session.samples) - 1))

    def _selected_or_zero(self) -> int:
        picks = self.listbox.curselection()
        return picks[0] if picks else 0

    def _load_editor(self) -> None:
        try:
            index = self._selected()
        except DemoError:
            return
        self._clear(self.editor)
        self.editor.insert("1.0", self.session.samples[index].text)

    def _clear(self, widget: tk.Text) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")

    def _show(self, widget: tk.Text, text: str) -> None:
        self._clear(widget)
        widget.insert("1.0", text.rstrip() + "\n")
        widget.configure(state="disabled")
        widget.see("1.0")

    def refresh_users(self) -> None:
        try:
            self.user_box.configure(values=self.session.users())
        except DemoError:
            self.user_box.configure(values=[])

    # -- actions ----------------------------------------------------------
    def pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root, title="选择代码文件",
            filetypes=[("源代码", "*.c *.cc *.cpp *.h *.hpp *.py *.go *.js *.ts *.java *.rs *.cs *.sh"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        self.session.add_files(list(paths))
        self._reload_list()

    def new_paste(self) -> None:
        self.session.add_text(f"pasted{len(self.session.samples) + 1}.txt", "")
        self._reload_list()
        self._select(len(self.session.samples) - 1)
        self._load_editor()
        self.editor.focus_set()

    def delete_sample(self) -> None:
        self.session.remove(self._selected())
        self._reload_list()
        self._load_editor()

    def clear_samples(self) -> None:
        self.session.clear_samples()
        self._reload_list()
        for widget in (self.out_profile, self.out_check, self.out_format):
            self._show(widget, "")

    def apply_editor(self) -> None:
        index = self._selected()
        raw = self.editor.get("1.0", "end")
        # Tk appends one implicit newline; "end-1c" would eat the file's own final newline
        text = raw[:-1] if raw.endswith("\n") else raw
        self.session.update_text(index, text)

    def switch_locale(self) -> None:
        set_locale(self.locale_var.get())
        if not self.session.samples:
            return
        self.cmd["extract"]()
        if self.session.report is not None:
            self.cmd["check"]()
        if self.session.entries:
            self.cmd["format"]()

    def run_extract(self) -> None:
        self.session.language = self.lang_var.get()
        self._show(self.out_profile, self.session.extract())

    def run_check(self) -> None:
        self.session.language = self.lang_var.get()
        user = self.user_var.get() if self.baseline_var.get() else None
        text = self.session.check(baseline_user=user)
        self._show(self.out_check, text)
        self._focus_tab("一致性检查")

    def run_load(self) -> None:
        self._show(self.out_profile, self.session.load_profile(self.user_var.get()))

    def run_save(self) -> None:
        self.session.language = self.lang_var.get()
        self._show(self.out_profile, self.session.save_profile(self.user_var.get()))
        self.refresh_users()

    def run_format(self) -> None:
        text = self.session.format_samples(
            source="user" if self.baseline_var.get() else "current",
            user=self.user_var.get(),
            observed_only=self.observed_var.get(),
            wrap=self.wrap_var.get(),
            min_confidence=float(self.min_conf.get()),
        )
        self._show(self.out_format, text)
        self._focus_tab("格式化预览")

    def run_write_back(self) -> None:
        index = self._selected()
        self._show(self.out_format, self.session.write_back(index))
        self._focus_tab("格式化预览")
        self._load_editor()


def main() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
