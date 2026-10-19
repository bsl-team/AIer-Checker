"""Logic behind the demo window: no tkinter here, so it stays testable."""
from __future__ import annotations

import difflib
import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from cf.cli import InputError, read_source, write_source
from cf.consistency import analyze
from cf.document import build_document
from cf.extract import extract_all
from cf.format import FormatOptions, FormatResult, Formatter
from cf.profile import Profile
from cf.report import render_consistency, render_profile
from cf.store import ProfileStore, StoreError
from cf.tokens import SPEC_BY_NAME

MAX_PRINTED_FINDINGS = 200


class DemoError(Exception):
    """Something the user can fix: pick files, add a sample, choose a user."""


@contextmanager
def friendly():
    """Turn kernel errors into one message type the window can show directly."""
    try:
        yield
    except DemoError:
        raise
    except (StoreError, InputError, OSError) as exc:
        raise DemoError(str(exc)) from exc


@dataclass
class Sample:
    name: str
    text: str
    path: str | None = None
    encoding: str = "utf-8"

    @property
    def from_file(self) -> bool:
        return bool(self.path)


@dataclass
class FormatEntry:
    sample: Sample
    result: FormatResult

    @property
    def diff(self) -> str:
        if not self.result.changed or self.result.error:
            return ""
        before = self.sample.text.replace("\r\n", "\n").replace("\r", "\n")
        after = self.result.text.replace("\r\n", "\n").replace("\r", "\n")
        lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{self.sample.name} (原文)",
            tofile=f"{self.sample.name} (格式化)",
            n=2,
        )
        return "".join(line.rstrip("\r\n") + "\n" for line in lines)


@dataclass
class Session:
    """The window's whole state, plus the actions the buttons trigger."""

    store_path: str | None = None
    samples: list[Sample] = field(default_factory=list)
    language: str = "auto"
    profile: Profile | None = None
    report: object | None = None
    entries: list[FormatEntry] = field(default_factory=list)
    status: str = "就绪"

    def __post_init__(self):
        self.store = ProfileStore(self.store_path)

    # -- sample management --------------------------------------------------
    def add_file(self, path: str) -> Sample:
        with friendly():
            text, encoding = read_source(path)
        sample = Sample(name=os.path.basename(path), text=text, path=path, encoding=encoding)
        self.samples.append(sample)
        return sample

    def add_files(self, paths: list[str]) -> int:
        added = 0
        for path in paths:
            if not os.path.isfile(path):
                continue
            self.add_file(path)
            added += 1
        if added:
            self.status = f"已加入 {added} 个文件"
        else:
            self.status = "没有可读文件"
        return added

    def add_text(self, name: str, text: str) -> Sample:
        sample = Sample(name=name, text=text)
        self.samples.append(sample)
        return sample

    def update_text(self, index: int, text: str) -> None:
        self._check_index(index)
        self.samples[index].text = text

    def remove(self, index: int) -> None:
        self._check_index(index)
        del self.samples[index]

    def clear_samples(self) -> None:
        self.samples.clear()
        self.entries.clear()
        self.report = None
        self.status = "已清空样本"

    def _check_index(self, index: int) -> None:
        if not 0 <= index < len(self.samples):
            raise DemoError("请先在左侧选中一个样本")

    # -- analysis -----------------------------------------------------------
    def _documents(self):
        if not self.samples:
            raise DemoError("还没有代码样本：添加文件，或把代码粘贴进来")
        spec = None if self.language == "auto" else SPEC_BY_NAME.get(self.language)
        if self.language != "auto" and spec is None:
            raise DemoError(f"未知语言 {self.language}")
        return [build_document(s.text, s.path or s.name, spec) for s in self.samples]

    def extract(self) -> str:
        self.profile = extract_all(self._documents())
        self.status = f"已从 {len(self.samples)} 个样本提取风格（{self.profile.language}）"
        return render_profile(self.profile, min_confidence=0.0, show_rows=True)

    def check(self, baseline_user: str | None = None) -> str:
        docs = self._documents()
        baseline = None
        if baseline_user and baseline_user.strip():
            with friendly():
                baseline = self.store.require(baseline_user.strip())
        self.report = analyze(docs, baseline=baseline)
        text = render_consistency(self.report, max_findings=MAX_PRINTED_FINDINGS, show_all=True)
        if baseline is not None:
            text = f"对照已存档案: {baseline.user}\n\n{text}"
        worst = max((f.severity for f in self.report.findings), key=_severity_rank, default="none")
        self.status = f"一致性 {self.report.score * 100:.1f}%，最高严重度 {worst}"
        return text

    def users(self) -> list[str]:
        with friendly():
            return self.store.users()

    def save_profile(self, user: str) -> str:
        user = user.strip()
        if not user:
            raise DemoError("请先填写用户名")
        docs = self._documents()
        with friendly():
            merged = self.store.add_from_samples(user, docs, replace=True)
            self.store.flush()
        self.profile = merged
        self.status = f"已保存档案 {user}（{len(docs)} 个样本）"
        return render_profile(merged, show_rows=True) + f"\n\n档案库: {self.store.path}"

    def load_profile(self, user: str) -> str:
        user = (user or "").strip()
        if not user:
            raise DemoError("请先在下拉框选择或输入一个用户名")
        with friendly():
            self.profile = self.store.require(user)
        self.status = f"已载入档案 {user}"
        return render_profile(self.profile, show_rows=True) + f"\n\n档案库: {self.store.path}"

    def format_samples(self, source: str = "current", user: str | None = None,
                       observed_only: bool = False, wrap: bool = True,
                       min_confidence: float = 0.5) -> str:
        profile = self.profile
        if source == "user":
            with friendly():
                profile = self.store.require((user or "").strip())
        if profile is None:
            raise DemoError("还没有可用风格档案：先“提取风格”或载入一个已存档案")
        options = FormatOptions(
            min_confidence=min_confidence,
            use_defaults=not observed_only,
            wrap=wrap,
        )
        docs = self._documents()
        self.entries = [FormatEntry(sample, Formatter(doc, profile, options).run())
                        for sample, doc in zip(self.samples, docs)]
        changed = sum(1 for e in self.entries if e.result.changed)
        refused = [e.sample.name for e in self.entries if e.result.error]
        self.status = f"{changed}/{len(self.entries)} 个样本会被改写" + (f"，拒绝: {', '.join(refused)}" if refused else "")
        return self.format_report()

    def format_report(self) -> str:
        blocks: list[str] = []
        for entry in self.entries:
            res = entry.result
            head = entry.sample.name
            if res.error:
                blocks.append(f"=== {head}\n    拒绝执行: {res.error}\n")
                continue
            if not res.changed:
                blocks.append(f"=== {head}\n    已符合档案，无需改动\n")
                continue
            applied = ", ".join(res.applied) or "-"
            skipped = ", ".join(res.skipped) or "-"
            blocks.append(
                f"=== {head}  [内核已校验 token 序列不变]\n"
                f"    应用规则: {applied}\n"
                f"    未应用(证据不足): {skipped}\n\n{entry.diff}"
            )
        if not blocks:
            return "还没有格式化结果：点“格式化预览”。"
        return "\n".join(blocks)

    def write_back(self, index: int) -> str:
        self._check_index(index)
        entry = self.entries[index] if index < len(self.entries) else None
        if entry is None:
            raise DemoError("请先做一次格式化预览")
        sample = entry.sample
        if not sample.from_file:
            raise DemoError("粘贴的样本没有对应文件，请复制预览文本")
        if entry.result.error:
            raise DemoError(f"内核拒绝格式化该文件: {entry.result.error}")
        with friendly():
            write_source(sample.path, entry.result.text, sample.encoding)
        sample.text = entry.result.text
        self.status = f"已写回 {os.path.basename(sample.path or '')}"
        return self.format_report()

    def preview_text(self, index: int) -> str:
        self._check_index(index)
        if index >= len(self.entries):
            raise DemoError("请先做一次格式化预览")
        return self.entries[index].result.text


def _severity_rank(sev: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(sev, -1)
