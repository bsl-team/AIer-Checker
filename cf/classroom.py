"""Teacher-facing classroom analysis: a contest folder in, review data out.

This module is deliberately UI-free.  It scans a folder such as::

    Contest/
      User1/T1.cpp  T2.cpp  T3.cpp  T4.cpp
      User2/T1.cpp  T2.cpp  T3.cpp  T4.cpp

and answers three questions separately:

* did one student mix clearly different styles in this contest?
* do submissions share too much structure with another student?
* how does this contest compare with a student's saved historical profile?

The "multiple styles" check is evidence, not an AI verdict.  It compares the
distributions of the 57 style axes between one student's files and only reports
a split when both the within-group and between-group evidence are strong.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .consistency import ConsistencyReport, Finding, analyze
from .document import Document, build_document
from .extract import extract, extract_all
from .i18n import rule_label, translate_value
from .profile import FEATURE_SPECS, Profile
from .similarity import Similarity, SimilarityError, SimilarityReport, Submission, find_duplicates
from .sources import InputError, read_source
from .store import ProfileStore, StoreError
from .tokens import SPEC_BY_NAME

# Source files the review desk understands.  Kept in the web layer so the
# kernel's language table stays untouched and upstream updates remain clean.
SOURCE_EXTENSIONS = frozenset({
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".ino",
    ".go", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".java", ".rs", ".cs", ".py", ".pyw", ".pyi",
    ".sh", ".bash", ".zsh",
})

MAX_FILE_BYTES = 2_000_000
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "venv", "env",
    "dist", "build", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
}
SENSITIVITIES: dict[str, dict[str, float | int]] = {
    "strict": {"min_jaccard": 0.60, "min_containment": 0.85, "min_shared": 4, "min_tokens": 100},
    "standard": {"min_jaccard": 0.50, "min_containment": 0.80, "min_shared": 3, "min_tokens": 80},
    "loose": {"min_jaccard": 0.40, "min_containment": 0.70, "min_shared": 2, "min_tokens": 50},
}
STYLE_MIN_SHARED = 3
STYLE_MIN_CONFIDENCE = 0.15
STYLE_SPLIT_SEPARATION = 0.12
STYLE_SPLIT_WITHIN = 0.62
STYLE_SPLIT_CROSS = 0.80
STYLE_WEAK_SEPARATION = 0.08
STYLE_WEAK_WITHIN = 0.55
STYLE_TWO_FILES_SPLIT = 0.55
STYLE_TWO_FILES_WEAK = 0.68


class ClassroomError(Exception):
    """A readable problem with the contest folder, store, or requested action."""


@dataclass
class ClassroomFile:
    id: str
    user: str
    problem: str
    path: str
    relative_path: str
    language: str
    family: str
    rows: int
    tokens: int
    chars: int
    encoding: str
    parse_error: str | None
    profile: Profile = field(repr=False, compare=False)

    @property
    def file_name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class StyleGroups:
    groups: list[list[str]]
    separation: float = 0.0
    within: float | None = None
    cross: float | None = None
    multi_style: bool = False
    weak: bool = False
    coverage: float = 0.0
    matrix: list[dict] = field(default_factory=list)
    note: str = ""


@dataclass
class UserAnalysis:
    user: str
    files: list[ClassroomFile]
    profile: Profile
    self_report: ConsistencyReport
    saved_profile: Profile | None
    saved_report: ConsistencyReport | None
    saved_overlap: int
    saved_score: float | None
    saved_findings: list[Finding]
    groups: StyleGroups
    pair_sims: list[dict]
    duplicate_pairs: list[Similarity]
    cross_pairs: list[Similarity]
    same_pairs: list[Similarity]
    attention: str = "low"
    reasons: list[str] = field(default_factory=list)


@dataclass
class ClassroomScan:
    root: str
    store_path: str
    language: str
    sensitivity: str
    same_problem: bool
    include_same_user: bool
    scanned_at: float
    elapsed_ms: int
    docs: dict[str, Document]
    files: dict[str, ClassroomFile]
    users: dict[str, UserAnalysis]
    duplicates: SimilarityReport
    warnings: list[str]
    skipped: list[dict]
    profile_summaries: list[dict]

    # -- lookups -------------------------------------------------------------
    def source(self, submission_id: str) -> dict:
        f = self.files.get(submission_id)
        doc = self.docs.get(submission_id)
        if f is None or doc is None:
            raise ClassroomError("当前扫描结果中没有这个文件")
        return {
            "id": f.id,
            "path": f.path,
            "language": f.language,
            "family": f.family,
            "encoding": f.encoding,
            "rows": f.rows,
            "tokens": f.tokens,
            "parse_error": f.parse_error,
            "text": doc.src,
        }

    # -- saving --------------------------------------------------------------
    def save_profiles(self, users: list[str] | None = None, mode: str = "merge") -> list[dict]:
        """Save the current contest files into the profile store.

        ``merge`` is the normal path: it appends only files whose path is not
        already in the stored profile.  ``replace`` rebuilds the profile from
        this contest only.  Re-saving the same contest is therefore a no-op for
        counts rather than double-counting the same file.
        """
        if mode not in ("merge", "replace"):
            raise ClassroomError("保存方式必须是 merge 或 replace")
        names = sorted(set(users if users is not None else self.users))
        unknown = [name for name in names if name not in self.users]
        if unknown:
            raise ClassroomError("扫描结果中没有这些学生：" + "、".join(unknown))
        if not names:
            raise ClassroomError("没有可保存的学生")

        store = ProfileStore(self.store_path)
        saved: list[dict] = []
        added_by_user: dict[str, int] = {}
        try:
            for user in names:
                ua = self.users[user]
                docs = [self.docs[f.id] for f in ua.files]
                existing = store.get(user)
                added = 0
                if mode == "replace" or existing is None:
                    fresh = extract_all(docs)
                    fresh.user = user
                    store.put(fresh)
                    added = len(docs)
                else:
                    profile = Profile.from_dict(existing.to_dict())
                    known = {_path_key(s.path) for s in profile.samples}
                    new_docs = [doc for doc in docs if _path_key(doc.path) not in known]
                    for doc in new_docs:
                        profile.merge(extract(doc))
                    if new_docs:
                        profile.user = user
                        store.put(profile)
                    added = len(new_docs)
                added_by_user[user] = added
            store.flush()
        except StoreError as exc:
            raise ClassroomError(str(exc)) from exc

        # Read back from disk so the UI gets the merged result, not our cache.
        self._reload_saved()
        for user in names:
            profile = store.get(user)
            saved.append({
                "user": user,
                "mode": mode,
                "added": added_by_user.get(user, 0),
                "samples": len(profile.samples) if profile else 0,
            })
        return saved

    def _reload_saved(self) -> None:
        try:
            store = ProfileStore(self.store_path)
            self.profile_summaries = [asdict(s) for s in store.summaries()]
            for user, ua in self.users.items():
                saved = store.get(user)
                ua.saved_profile = saved
                ua.saved_overlap = _saved_overlap(saved, ua.files)
                if saved is None:
                    ua.saved_report = None
                    ua.saved_score = None
                    ua.saved_findings = []
                else:
                    docs = [self.docs[f.id] for f in ua.files]
                    ua.saved_report = analyze(docs, baseline=saved)
                    ua.saved_score = ua.saved_report.score
                    ua.saved_findings = [
                        f for f in ua.saved_report.findings if f.kind == "against-profile"
                    ]
                ua.attention, ua.reasons = _assess(ua)
        except StoreError as exc:
            raise ClassroomError(str(exc)) from exc

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        users = [_user_payload(ua) for ua in sorted(self.users.values(), key=lambda u: u.user)]
        dup = _duplicates_payload(self.duplicates)
        cross = sum(1 for p in self.duplicates.pairs if p.cross_user)
        same = len(self.duplicates.pairs) - cross
        summary = {
            "users": len(self.users),
            "submissions": len(self.files),
            "duplicate_pairs": len(self.duplicates.pairs),
            "cross_user_pairs": cross,
            "same_user_pairs": same,
            "clusters": len(self.duplicates.clusters),
            "multi_style_users": sum(1 for ua in self.users.values() if ua.groups.multi_style),
            "attention_high": sum(1 for ua in self.users.values() if ua.attention == "high"),
            "attention_medium": sum(1 for ua in self.users.values() if ua.attention == "medium"),
            "saved_profiles": len(self.profile_summaries),
            "skipped": len(self.skipped),
            "warnings": len(self.warnings),
            "scan_ms": self.elapsed_ms,
        }
        return {
            "root": self.root,
            "root_name": os.path.basename(os.path.normpath(self.root)),
            "store": self.store_path,
            "language": self.language,
            "sensitivity": self.sensitivity,
            "same_problem": self.same_problem,
            "include_same_user": self.include_same_user,
            "scanned_at": self.scanned_at,
            "elapsed_ms": self.elapsed_ms,
            "summary": summary,
            "users": users,
            "files": [
                {
                    "id": f.id, "user": f.user, "problem": f.problem, "path": f.path,
                    "language": f.language, "rows": f.rows, "tokens": f.tokens,
                    "parse_error": f.parse_error,
                }
                for f in sorted(self.files.values(), key=lambda f: f.id)
            ],
            "duplicates": dup,
            "profiles": list(self.profile_summaries),
            "warnings": list(self.warnings),
            "skipped": list(self.skipped),
        }


# -- public entry point -------------------------------------------------------


def scan_contest(
    root: str,
    *,
    store_path: str | None = None,
    language: str | None = None,
    sensitivity: str = "standard",
    same_problem: bool = True,
    include_same_user: bool = True,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> ClassroomScan:
    """Scan a contest folder and build the review model used by the web UI."""
    started = time.time()
    if not root or not str(root).strip():
        raise ClassroomError("请填写比赛文件夹路径")
    root_abs = os.path.abspath(os.path.expanduser(str(root).strip()))
    if not os.path.isdir(root_abs):
        raise ClassroomError(f"比赛文件夹不存在：{root_abs}")
    if sensitivity not in SENSITIVITIES:
        raise ClassroomError("灵敏度必须是 strict、standard 或 loose")
    lang = None if (not language or language == "auto") else str(language)
    if lang is not None and lang not in SPEC_BY_NAME:
        raise ClassroomError(f"不支持的语言：{lang}")
    spec = SPEC_BY_NAME.get(lang) if lang else None

    store = ProfileStore(store_path)
    try:
        profile_summaries = [asdict(s) for s in store.summaries()]
    except StoreError as exc:
        raise ClassroomError(str(exc)) from exc

    groups, root_is_user = _discover_users(root_abs)
    warnings: list[str] = []
    skipped: list[dict] = []
    docs_by_user: dict[str, list[Document]] = {}
    files_by_user: dict[str, list[ClassroomFile]] = {}
    docs: dict[str, Document] = {}
    files: dict[str, ClassroomFile] = {}
    submissions: list[Submission] = []
    used_ids: set[str] = set()

    if not root_is_user:
        root_sources = list(_direct_sources(root_abs))
        if root_sources:
            warnings.append("root-files-ignored: 根目录下的源码没有归入任何学生，已跳过")

    for user, folder in groups:
        for path in _iter_sources(folder):
            if not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                skipped.append({"path": path, "reason": f"无法读取文件大小：{exc}"})
                continue
            if size > max_file_bytes:
                skipped.append({"path": path, "reason": f"文件超过 {max_file_bytes // 1024} KB"})
                continue
            try:
                text, encoding = read_source(path)
            except (InputError, OSError) as exc:
                skipped.append({"path": path, "reason": str(exc)})
                continue
            doc = build_document(text, path, spec)
            rel = os.path.relpath(path, root_abs).replace(os.sep, "/")
            rel_key = rel.casefold()
            if rel_key in used_ids:
                skipped.append({"path": path, "reason": f"重复的相对路径：{rel}"})
                continue
            used_ids.add(rel_key)
            user_dir = folder
            problem = os.path.splitext(os.path.relpath(path, user_dir))[0].replace(os.sep, "/")
            file_id = rel
            entry = ClassroomFile(
                id=file_id,
                user=user,
                problem=problem,
                path=os.path.abspath(path),
                relative_path=rel,
                language=doc.spec.name,
                family=doc.spec.family,
                rows=len(doc.lines),
                tokens=len(doc.tokens),
                chars=len(doc.src),
                encoding=encoding,
                parse_error=doc.parse_error,
                profile=extract(doc),
            )
            docs[file_id] = doc
            files[file_id] = entry
            docs_by_user.setdefault(user, []).append(doc)
            files_by_user.setdefault(user, []).append(entry)
            submissions.append(Submission(
                path=entry.path, id=file_id, user=user, problem=problem,
                language=doc.spec.name, text=text,
            ))

    if not files:
        raise ClassroomError(
            "没有找到可分析的源码文件。请确认文件夹结构类似 Contest/User1/T1.cpp，"
            "并检查文件后缀是否受支持。"
        )

    params = SENSITIVITIES[sensitivity]
    try:
        duplicates = find_duplicates(
            submissions,
            same_problem=same_problem,
            include_same_user=include_same_user,
            **params,
        )
    except SimilarityError as exc:
        raise ClassroomError(str(exc)) from exc

    users: dict[str, UserAnalysis] = {}
    for user in sorted(docs_by_user):
        ua = _build_user(
            user,
            sorted(files_by_user[user], key=lambda f: (f.problem, f.id)),
            docs_by_user[user],
            store.get(user),
            duplicates,
        )
        users[user] = ua

    for f in files.values():
        if f.parse_error:
            warnings.append(f"parse-error:{f.id}")

    all_warnings = list(dict.fromkeys(duplicates.warnings + warnings))
    elapsed = int((time.time() - started) * 1000)
    return ClassroomScan(
        root=root_abs,
        store_path=store.path,
        language=lang or "auto",
        sensitivity=sensitivity,
        same_problem=same_problem,
        include_same_user=include_same_user,
        scanned_at=time.time(),
        elapsed_ms=elapsed,
        docs=docs,
        files=files,
        users=users,
        duplicates=duplicates,
        warnings=all_warnings,
        skipped=skipped,
        profile_summaries=profile_summaries,
    )


# -- discovery ----------------------------------------------------------------


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS


def _iter_sources(folder: str):
    try:
        walker = os.walk(folder)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
            for name in sorted(filenames):
                if os.path.splitext(name)[1].lower() in SOURCE_EXTENSIONS:
                    yield os.path.join(dirpath, name)
    except OSError:
        return


def _direct_sources(folder: str):
    """Source files directly in ``folder``, not in its subfolders."""
    try:
        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in SOURCE_EXTENSIONS:
                yield entry.path
    except OSError:
        return


def _discover_users(root: str) -> tuple[list[tuple[str, str]], bool]:
    """Return (user, folder) pairs.  A flat root is treated as one user called root."""
    try:
        entries = sorted(
            (e.name, e.path) for e in os.scandir(root)
            if e.is_dir() and not _skip_dir(e.name)
        )
    except OSError as exc:
        raise ClassroomError(f"无法读取比赛文件夹：{exc}") from exc
    if not entries:
        return [("root", root)], True
    return entries, False


# -- per-user analysis --------------------------------------------------------


def _build_user(
    user: str,
    files: list[ClassroomFile],
    docs: list[Document],
    saved: Profile | None,
    duplicates: SimilarityReport,
) -> UserAnalysis:
    profile = extract_all(docs)
    profile.user = user
    self_report = analyze(docs, baseline=profile)
    saved_report = analyze(docs, baseline=saved) if saved is not None else None
    saved_findings = (
        [f for f in saved_report.findings if f.kind == "against-profile"]
        if saved_report is not None else []
    )
    groups = _style_groups(files)
    pairs = [p for p in duplicates.pairs if p.user_a == user or p.user_b == user]
    cross = [p for p in pairs if p.cross_user]
    same = [p for p in pairs if not p.cross_user]
    ua = UserAnalysis(
        user=user,
        files=files,
        profile=profile,
        self_report=self_report,
        saved_profile=saved,
        saved_report=saved_report,
        saved_overlap=_saved_overlap(saved, files),
        saved_score=saved_report.score if saved_report is not None else None,
        saved_findings=saved_findings,
        groups=groups,
        pair_sims=groups.matrix,
        duplicate_pairs=pairs,
        cross_pairs=cross,
        same_pairs=same,
    )
    ua.attention, ua.reasons = _assess(ua)
    return ua


def style_pair_similarity(a: Profile, b: Profile) -> dict | None:
    """Compare two one-file profiles as distributions, not as single values.

    The score is a confidence-weighted mean of per-axis distribution similarity
    (1 - total variation).  Features observed in only one profile are ignored.
    """
    score = 0.0
    weight = 0.0
    shared = 0
    for key in sorted(set(a.features) & set(b.features)):
        fa, fb = a.features[key], b.features[key]
        if not fa.total or not fb.total or not fa.value or not fb.value:
            continue
        conf = min(fa.confidence, fb.confidence)
        if conf < STYLE_MIN_CONFIDENCE:
            continue
        values = set(fa.counts) | set(fb.counts)
        tvd = 0.5 * sum(
            abs(fa.counts.get(v, 0) / fa.total - fb.counts.get(v, 0) / fb.total)
            for v in values
        )
        sim = max(0.0, min(1.0, 1.0 - tvd))
        evidence = min(fa.total, fb.total)
        w = (0.35 + 0.65 * conf) * (1.0 + min(evidence, 20) / 20.0 * 0.25)
        score += sim * w
        weight += w
        shared += 1
    if shared < STYLE_MIN_SHARED or weight <= 0:
        return None
    return {"score": round(score / weight, 4), "shared": shared, "weight": round(weight, 2)}


def _style_groups(files: list[ClassroomFile]) -> StyleGroups:
    ids = [f.id for f in files]
    n = len(ids)
    if n <= 1:
        return StyleGroups(
            groups=[ids] if ids else [],
            note="只有一个文件，无法判断多套风格" if ids else "没有文件",
        )

    sims: dict[tuple[str, str], float] = {}
    matrix: list[dict] = []
    shared_counts: list[int] = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = ids[i], ids[j]
            result = style_pair_similarity(files[i].profile, files[j].profile)
            if result is None:
                continue
            key = (a, b) if a < b else (b, a)
            sims[key] = result["score"]
            shared_counts.append(result["shared"])
            matrix.append({
                "a": a, "b": b, "score": result["score"], "shared": result["shared"],
            })
    matrix.sort(key=lambda item: (item["a"], item["b"]))
    coverage = round(
        (sum(shared_counts) / len(shared_counts) / len(FEATURE_SPECS)) if shared_counts else 0.0,
        3,
    )

    def pair(a: str, b: str) -> float | None:
        return sims.get((a, b) if a < b else (b, a))

    if n == 2:
        s = pair(ids[0], ids[1])
        if s is None:
            return StyleGroups(
                groups=[ids], coverage=coverage, matrix=matrix,
                note="共同风格特征不足，无法比较两个文件",
            )
        split = s < STYLE_TWO_FILES_SPLIT
        weak = s < STYLE_TWO_FILES_WEAK
        return StyleGroups(
            groups=[[ids[0]], [ids[1]]] if weak else [ids],
            separation=round(1.0 - s, 3),
            within=None,
            cross=round(s, 3),
            multi_style=split,
            weak=weak,
            coverage=coverage,
            matrix=matrix,
            note=(
                "两个文件的风格差异较大，但样本仅 2 个，只能作为人工复核线索"
                if split else
                "两个文件存在一定风格差异，样本较少，证据有限"
                if weak else
                "两个文件风格接近；样本仅 2 个，证据有限"
            ),
        )

    best = _best_partition(ids, sims)
    if best is None:
        return StyleGroups(
            groups=[ids], coverage=coverage, matrix=matrix,
            note="共同风格特征不足，无法可靠分组",
        )
    group_a, group_b, within, cross, separation = best
    multi = separation >= STYLE_SPLIT_SEPARATION and within >= STYLE_SPLIT_WITHIN and cross <= STYLE_SPLIT_CROSS
    weak = separation >= STYLE_WEAK_SEPARATION and within >= STYLE_WEAK_WITHIN
    groups = [group_a, group_b] if (multi or weak) else [ids]
    groups.sort(key=lambda g: (-len(g), g[0] if g else ""))
    if multi:
        note = (
            f"检测到 {len(groups)} 组可区分风格：组内平均 {within:.0%}，"
            f"组间平均 {cross:.0%}"
        )
    elif weak:
        note = (
            f"存在一定风格差异：组内平均 {within:.0%}，组间平均 {cross:.0%}，"
            "但未达到稳定分组条件"
        )
    else:
        note = "未发现明显的多套风格"
    if coverage < 0.25:
        note += "；共同特征较少，建议结合源码人工复核"
    return StyleGroups(
        groups=groups,
        separation=round(separation, 3),
        within=round(within, 3),
        cross=round(cross, 3),
        multi_style=multi,
        weak=weak,
        coverage=coverage,
        matrix=matrix,
        note=note,
    )


def _best_partition(
    ids: list[str], sims: dict[tuple[str, str], float]
) -> tuple[list[str], list[str], float, float, float] | None:
    """Enumerate two-way splits, keeping the first item in group A to break symmetry."""
    n = len(ids)
    best: tuple[float, float, list[str], list[str], float, float] | None = None
    for mask in range(1, 1 << (n - 1)):
        a = [ids[0]] + [ids[i] for i in range(1, n) if mask & (1 << (i - 1))]
        b = [ids[i] for i in range(1, n) if not (mask & (1 << (i - 1)))]
        if not b:
            continue
        within: list[float] = []
        cross: list[float] = []
        for x in range(n):
            for y in range(x + 1, n):
                a_has = ids[x] in a
                b_has = ids[y] in a
                s = sims.get((ids[x], ids[y]) if ids[x] < ids[y] else (ids[y], ids[x]))
                if s is None:
                    continue
                if a_has == b_has:
                    within.append(s)
                else:
                    cross.append(s)
        if not within or not cross:
            continue
        w = sum(within) / len(within)
        c = sum(cross) / len(cross)
        sep = w - c
        balance = min(len(a), len(b)) / n
        key = (sep, w, balance)
        if best is None or key > best[0:2] + (min(len(best[2]), len(best[3])) / n,):
            best = (sep, w, a, b, w, c)
    if best is None:
        return None
    sep, _w, a, b, w, c = best
    return a, b, w, c, sep


def _assess(ua: UserAnalysis) -> tuple[str, list[str]]:
    reasons: list[str] = []
    high = False
    medium = False
    g = ua.groups
    if g.multi_style:
        reasons.append(
            f"同一场比赛出现 {len(g.groups)} 组可区分风格（组间差异 {g.separation:.0%}）"
        )
        medium = True
        if len(ua.files) >= 3 and g.separation >= 0.18:
            high = True
    elif g.weak:
        reasons.append("文件之间存在风格差异，但证据强度不足以稳定分组")
        medium = True

    saved = ua.saved_profile
    if saved is not None:
        if ua.saved_overlap:
            reasons.append(
                f"已存档案包含本次 {ua.saved_overlap} 个文件，档案一致性会因自证而虚高，仅作参考"
            )
        elif saved.family != ua.profile.family:
            reasons.append("已存档案与本次提交的语族不同，档案一致性仅供参考")
        elif ua.saved_score is not None:
            if ua.saved_score < 0.62:
                reasons.append(f"与历史档案一致性仅 {ua.saved_score:.0%}")
                high = True
            elif ua.saved_score < 0.75:
                reasons.append(f"与历史档案一致性偏低（{ua.saved_score:.0%}）")
                medium = True
            elif ua.saved_score >= 0.85:
                reasons.append(f"与历史档案风格接近（{ua.saved_score:.0%}）")
        strong_saved = sum(1 for f in ua.saved_findings if f.severity == "high")
        if strong_saved >= 2 and not ua.saved_overlap and saved.family == ua.profile.family:
            reasons.append(f"历史档案中有 {strong_saved} 条高严重度风格偏离")
            high = True

    strong_cross = [
        p for p in ua.cross_pairs
        if p.jaccard >= 0.72 or p.containment >= 0.90
    ]
    if ua.cross_pairs:
        reasons.append(f"涉及 {len(ua.cross_pairs)} 对跨用户重复")
        medium = True
    if strong_cross:
        reasons.append(f"其中 {len(strong_cross)} 对跨用户重复的相似度很高")
        high = True
    if ua.same_pairs:
        reasons.append(f"本人文件之间有 {len(ua.same_pairs)} 对结构重复")
        medium = True

    if not reasons:
        reasons.append("未发现需要优先复核的风格或重复信号")
    attention = "high" if high else "medium" if medium else "low"
    return attention, list(dict.fromkeys(reasons))


# -- payload helpers ----------------------------------------------------------


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _saved_overlap(saved: Profile | None, files: list[ClassroomFile]) -> int:
    if saved is None:
        return 0
    current = {_path_key(f.path) for f in files}
    return sum(1 for sample in saved.samples if _path_key(sample.path) in current)


def _finding_payload(f: Finding, source: str = "self") -> dict:
    data = f.to_dict()
    data.update({
        "source": source,
        "rule_label": rule_label(f.rule),
        "expected_label": translate_value(f.expected),
        "actual_label": translate_value(f.actual),
        "severity_label": translate_value(f.severity),
        "kind_label": "文件内混用" if f.kind == "intra-file" else "与档案不一致",
    })
    return data


def _feature_rows(profile: Profile, saved: Profile | None) -> list[dict]:
    current = profile.effective()
    previous = saved.effective() if saved is not None else {}
    rows: list[dict] = []
    for key, spec in FEATURE_SPECS.items():
        value, confidence, observed = current[key]
        saved_value, saved_conf, saved_observed = previous.get(key, (None, 0.0, False))
        f = profile.features.get(key)
        rows.append({
            "key": key,
            "label": rule_label(key),
            "kind": spec[0],
            "value": value,
            "value_label": translate_value(value),
            "confidence": confidence,
            "support": f.support if f and observed else 0,
            "total": f.total if f and observed else 0,
            "agreement": f.agreement if f and observed else 0.0,
            "thin": f.thin if f and observed else True,
            "observed": observed,
            "saved_value": saved_value if saved_observed else None,
            "saved_value_label": translate_value(saved_value) if saved_observed else None,
            "saved_observed": saved_observed,
            "saved_confidence": saved_conf if saved_observed else 0.0,
        })
    rows.sort(key=lambda row: (not row["observed"], -row["confidence"], row["key"]))
    return rows


def _pair_sort_key(p: Similarity) -> tuple:
    return (-p.jaccard, -p.containment, p.a, p.b)


def _duplicates_payload(report: SimilarityReport) -> dict:
    data = report.to_dict()
    cluster_of: dict[tuple[str, str], str] = {}
    for cluster in report.clusters:
        for pair in cluster.pair_ids:
            cluster_of[tuple(sorted(pair))] = cluster.id
    for pair in data["pairs"]:
        pair["cluster_id"] = cluster_of.get(tuple(sorted((pair["a"], pair["b"]))))
    return data


def _user_payload(ua: UserAnalysis) -> dict:
    self_scores = {f.file: f.score for f in ua.self_report.files}
    saved_scores = {f.file: f.score for f in ua.saved_report.files} if ua.saved_report else {}
    cluster_of: dict[str, int] = {}
    for index, group in enumerate(ua.groups.groups):
        for file_id in group:
            cluster_of[file_id] = index
    group_label = (
        {index: f"组 {chr(65 + index)}" for index in range(len(ua.groups.groups))}
        if len(ua.groups.groups) > 1 else {0: "同一组"}
    )

    files: list[dict] = []
    for f in ua.files:
        self_findings = [x for x in ua.self_report.findings if x.file == f.path]
        saved_file_findings = [x for x in ua.saved_findings if x.file == f.path]
        pairs_for_file = [p for p in ua.duplicate_pairs if f.id in (p.a, p.b)]
        index = cluster_of.get(f.id)
        files.append({
            "id": f.id,
            "user": f.user,
            "problem": f.problem,
            "path": f.path,
            "relative_path": f.relative_path,
            "file_name": f.file_name,
            "language": f.language,
            "family": f.family,
            "rows": f.rows,
            "tokens": f.tokens,
            "chars": f.chars,
            "encoding": f.encoding,
            "parse_error": f.parse_error,
            "self_score": round(self_scores.get(f.path, 1.0), 3),
            "saved_score": round(saved_scores[f.path], 3) if f.path in saved_scores else None,
            "cluster": index,
            "cluster_label": group_label.get(index),
            "group_size": len(ua.groups.groups[index]) if index is not None else None,
            "findings": len(self_findings),
            "saved_findings": len(saved_file_findings),
            "duplicate_pairs": len(pairs_for_file),
            "cross_user_pairs": sum(1 for p in pairs_for_file if p.cross_user),
        })

    findings = [_finding_payload(f, "self") for f in ua.self_report.findings]
    saved_findings = [_finding_payload(f, "saved") for f in ua.saved_findings]
    summary = ua.profile.summary()
    saved_summary = ua.saved_profile.summary() if ua.saved_profile else None
    saved_comparable = (
        ua.saved_profile is not None
        and ua.saved_overlap == 0
        and ua.saved_profile.family == ua.profile.family
    )
    return {
        "user": ua.user,
        "language": ua.profile.language,
        "family": ua.profile.family,
        "profile": {
            "user": ua.user,
            "language": ua.profile.language,
            "family": ua.profile.family,
            "samples": len(ua.profile.samples),
            "rows": sum(s.rows for s in ua.profile.samples),
            "confident_features": summary.get("confident_features", 0),
            "updated": ua.profile.updated,
        },
        "attention": ua.attention,
        "attention_label": {"high": "高关注", "medium": "建议复核", "low": "常规"}[ua.attention],
        "reasons": ua.reasons,
        "self_score": round(ua.self_report.score, 3),
        "saved_score": round(ua.saved_score, 3) if ua.saved_score is not None else None,
        "saved_overlap": ua.saved_overlap,
        "saved_comparable": saved_comparable,
        "saved_family_mismatch": bool(
            ua.saved_profile and ua.saved_profile.family != ua.profile.family
        ),
        "saved_profile": (
            {
                "exists": True,
                "language": ua.saved_profile.language,
                "family": ua.saved_profile.family,
                "samples": len(ua.saved_profile.samples),
                "confident_features": (saved_summary or {}).get("confident_features", 0),
                "updated": ua.saved_profile.updated,
            }
            if ua.saved_profile else {"exists": False}
        ),
        "files": files,
        "groups": [
            {
                "index": index,
                "label": group_label[index],
                "members": group,
                "size": len(group),
            }
            for index, group in enumerate(ua.groups.groups)
        ],
        "style": {
            "multi_style": ua.groups.multi_style,
            "weak": ua.groups.weak,
            "separation": ua.groups.separation,
            "within": ua.groups.within,
            "cross": ua.groups.cross,
            "coverage": ua.groups.coverage,
            "note": ua.groups.note,
            "matrix": ua.groups.matrix,
        },
        "findings": findings,
        "saved_findings": saved_findings,
        "style_features": _feature_rows(ua.profile, ua.saved_profile),
        "duplicate_pairs": len(ua.duplicate_pairs),
        "cross_user_pairs": len(ua.cross_pairs),
        "same_user_pairs": len(ua.same_pairs),
        "duplicate_pair_ids": [[p.a, p.b] for p in sorted(ua.duplicate_pairs, key=_pair_sort_key)],
    }
