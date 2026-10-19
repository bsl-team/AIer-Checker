"""Command line entry point: extract, check, profile, format."""
from __future__ import annotations

import argparse
import codecs
import json
import os
import sys

from . import __version__
from .consistency import analyze
from .document import build_document
from .extract import extract_all
from .format import FormatOptions, Formatter
from .profile import FEATURE_SPECS, Profile
from .report import (
    render_consistency,
    render_format,
    render_languages,
    render_profile,
    render_rules,
    render_store,
    rule_summary,
)
from .store import ProfileStore, StoreError
from .tokens import SPEC_BY_NAME

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2
ENCODINGS = ("utf-8", "gbk", "cp936", "big5", "cp1252", "latin-1")
SEVERITY_RANK = {"none": -1, "low": 0, "medium": 1, "high": 2}


class InputError(Exception):
    pass


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decode source text and remember the encoding so an in-place rewrite is faithful.

    BOMs are detected explicitly: decoding a BOM-less file with utf-8-sig and writing it back
    would add a BOM that was never there.
    """
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def read_source(path: str) -> tuple[str, str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return decode_bytes(raw)


def write_source(path: str, text: str, encoding: str) -> None:
    try:
        with open(path, "wb") as fh:
            fh.write(text.encode(encoding, errors="replace"))
    except OSError as exc:
        raise InputError(f"cannot write {path}: {exc.strerror or exc}") from exc


def load_inputs(paths: list[str], lang: str | None = None) -> tuple[list, list[tuple[str, str]]]:
    """Return (documents, [(path, encoding)]).  No paths, or ``-``, reads stdin."""
    spec = None
    if lang:
        if lang not in SPEC_BY_NAME:
            raise InputError(f"unknown language {lang!r}; try --lang with one of {', '.join(sorted(SPEC_BY_NAME))}")
        spec = SPEC_BY_NAME[lang]
    if not paths or paths == ["-"]:
        raw = sys.stdin.buffer.read()
        text, enc = decode_bytes(raw)
        return [build_document(text, "<stdin>", spec)], [("<stdin>", enc)]
    docs, meta = [], []
    for path in paths:
        text, enc = read_source(path)
        docs.append(build_document(text, path, spec))
        meta.append((path, enc))
    return docs, meta


def profile_from_args(args, store: ProfileStore) -> Profile:
    user = getattr(args, "user", None)
    path = getattr(args, "profile", None)
    if user and path:
        raise InputError("use either --user or --profile, not both")
    if user:
        return store.require(user)
    if path:
        try:
            text, _enc = read_source(path)
            data = json.loads(text)
        except OSError as exc:
            raise InputError(f"cannot read profile {path}: {exc.strerror or exc}") from exc
        except json.JSONDecodeError as exc:
            raise InputError(f"profile {path} is not valid JSON: {exc}") from exc
        if "users" in data:
            names = list(data["users"])
            if not names:
                raise InputError(f"profile file {path} contains no users")
            return Profile.from_dict(data["users"][names[0]])
        return Profile.from_dict(data)
    raise InputError("a style profile is required: pass --user NAME or --profile FILE")


def options_from_args(args) -> FormatOptions:
    rules = None
    if getattr(args, "rules", None):
        rules = {r.strip() for r in args.rules.split(",") if r.strip()}
        unknown = sorted(rules - set(FEATURE_SPECS))
        if unknown:
            raise InputError(f"unknown rule(s): {', '.join(unknown)} (see 'cf rules')")
    return FormatOptions(
        rules=rules,
        min_confidence=args.min_confidence,
        use_defaults=not args.observed_only,
        wrap=not args.no_wrap,
        eol={"lf": "\n", "crlf": "\r\n"}.get(getattr(args, "eol", "keep"), None),
    )


# -- commands ---------------------------------------------------------------


def cmd_extract(args) -> int:
    docs, _meta = load_inputs(args.files, args.lang)
    store = ProfileStore(args.store)
    if args.user:
        profile = store.add_from_samples(args.user, docs, replace=args.replace)
        store.flush()
    else:
        profile = extract_all(docs)
    if args.out:
        write_source(args.out, json.dumps(profile.to_dict(), indent=2, ensure_ascii=False) + "\n", "utf-8")
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_profile(profile, min_confidence=args.min_confidence, show_rows=args.show_rows))
        if args.user:
            print(f"\nsaved profile for {profile.user} in {store.path}")
    return EXIT_OK


def cmd_check(args) -> int:
    docs, _meta = load_inputs(args.files, args.lang)
    store = ProfileStore(args.store)
    baseline = profile_from_args(args, store) if (args.user or args.profile) else None
    report = analyze(docs, baseline=baseline, min_evidence=args.min_evidence)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_consistency(report, max_findings=args.max_findings, show_all=args.all))
        if args.show_rules:
            rows = rule_summary(report)
            if rows:
                print("per-rule agreement:")
                print("\n".join(f"  {line}" for line in rows))
    threshold = SEVERITY_RANK.get(args.fail_on, 2)
    worst = max((SEVERITY_RANK.get(f.severity, 0) for f in report.findings), default=-1)
    return EXIT_FINDINGS if threshold >= 0 and worst >= threshold else EXIT_OK


def cmd_profile(args) -> int:
    store = ProfileStore(args.store)
    action = args.action
    if action == "list":
        if args.json:
            print(json.dumps([s.__dict__ for s in store.summaries()], indent=2, ensure_ascii=False))
        else:
            print(render_store(store.summaries(), store.path))
        return EXIT_OK
    if action in ("add", "update"):
        if not args.files:
            raise InputError(f"'profile {action}' needs source files to learn from")
        docs, _meta = load_inputs(args.files, args.lang)
        replace = action == "add" and args.replace
        profile = store.add_from_samples(args.name, docs, replace=replace)
        store.flush()
        if args.json:
            print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
        else:
            verb = "created" if replace or action == "add" else "updated"
            print(f"{verb} profile '{profile.user}' from {len(docs)} sample(s), language {profile.language}")
            print(f"store: {store.path}")
            print(render_profile(profile, min_confidence=args.min_confidence))
        return EXIT_OK
    if action == "show":
        profile = store.require(args.name)
        if args.json:
            print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(render_profile(profile, min_confidence=args.min_confidence, show_rows=args.show_rows))
        return EXIT_OK
    if action == "delete":
        if not store.remove(args.name):
            raise InputError(f"no profile named {args.name!r}")
        store.flush()
        print(f"deleted profile '{args.name}'")
        return EXIT_OK
    if action == "rename":
        profile = store.rename(args.name, args.new_name)
        store.flush()
        print(f"renamed '{args.name}' to '{profile.user}'")
        return EXIT_OK
    if action == "export":
        text = store.export(args.name)
        if args.out:
            write_source(args.out, text, "utf-8")
            print(f"wrote {args.name} to {args.out}")
        else:
            print(text, end="")
        return EXIT_OK
    if action == "import":
        text, _enc = read_source(args.file)
        profile = store.import_json(text, user=args.name, replace=args.replace)
        store.flush()
        print(f"imported profile '{profile.user}' ({len(profile.samples)} sample(s)) into {store.path}")
        return EXIT_OK
    raise InputError(f"unknown profile action {action!r}")


def cmd_format(args) -> int:
    docs, meta = load_inputs(args.files, args.lang)
    store = ProfileStore(args.store)
    profile = profile_from_args(args, store)
    options = options_from_args(args)
    results = []
    refused = []
    for (path, enc), doc in zip(meta, docs):
        res = Formatter(doc, profile, options).run()
        results.append((path, res, enc, doc.src))
        if res.error:
            refused.append(path)
    from_stdin = len(meta) == 1 and meta[0][0] == "<stdin>"
    if from_stdin and not args.check:
        sys.stdout.write(results[0][1].text)
    elif args.write:
        for path, res, enc, _before in results:
            if res.changed and not res.error:
                write_source(path, res.text, enc)
    if args.json:
        payload = {
            "profile": profile.user,
            "mode": "write" if args.write else "check" if args.check else "preview",
            "files": [
                {
                    "file": path,
                    "changed": res.changed,
                    "applied": res.applied,
                    "skipped": res.skipped,
                    "error": res.error,
                }
                for path, res, _enc, _before in results
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif not from_stdin:
        print(render_format([(p, r, before) for p, r, _enc, before in results], diffs=args.diff))
    if refused:
        print(f"could not format {len(refused)} file(s) safely: {', '.join(refused)}", file=sys.stderr)
        return EXIT_ERROR
    changed_any = any(res.changed for _p, res, _e, _b in results)
    if args.check and changed_any:
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_rules(args) -> int:
    print(render_rules())
    return EXIT_OK


def cmd_languages(args) -> int:
    print(render_languages(sorted(SPEC_BY_NAME.items())))
    return EXIT_OK


# -- parser -----------------------------------------------------------------


def add_common(parser: argparse.ArgumentParser, files_help: str) -> None:
    parser.add_argument("files", nargs="*", help=files_help)
    parser.add_argument("--lang", help="force the language instead of detecting it")
    parser.add_argument("--store", help=f"profile store file (default {os.path.join('~', '.code-feature', 'profiles.json')}, env CF_STORE)")
    parser.add_argument("--json", action="store_true", help="machine readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cf",
        description="Code style kernel: extract a developer's style, audit consistency, store profiles, reformat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cf profile add bin --lang cpp src/*.cpp              learn a profile from samples\n"
            "  cf extract src/*.py                                  show what the samples imply\n"
            "  cf check src/*.cpp --user bin --fail-on medium        audit against a stored profile\n"
            "  cf format --user bin --check src/*.cpp                CI gate: would anything move?\n"
            "  cf format --user bin --write src/*.cpp               reformat in place\n"
            "  cf format --profile bin.json < main.c                 read stdin, write stdout\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"cf {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="extract a style profile from code samples")
    add_common(p, "source files (default: stdin)")
    p.add_argument("--user", help="also save the profile under this user")
    p.add_argument("--replace", action="store_true", help="replace the user's stored profile")
    p.add_argument("--out", help="write the profile JSON to this file")
    p.add_argument("--min-confidence", type=float, default=0.0, help="hide weakly evidenced rules")
    p.add_argument("--show-rows", action="store_true", help="list the rows where a rule deviates")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("check", help="find style inconsistencies inside and across samples")
    add_common(p, "source files (default: stdin)")
    p.add_argument("--user", help="audit against a stored profile instead of the samples' own")
    p.add_argument("--profile", help="audit against a profile JSON file")
    p.add_argument("--min-evidence", type=int, default=3, help="ignore rules seen fewer than N times")
    p.add_argument("--max-findings", type=int, default=40, help="cap printed findings (0 = no cap)")
    p.add_argument("--all", action="store_true", help="print every finding")
    p.add_argument("--show-rules", action="store_true", help="also print per-rule agreement")
    p.add_argument("--fail-on", choices=("none", "low", "medium", "high"), default="high",
                   help="exit 1 when a finding of at least this severity exists")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("profile", help="manage stored style profiles")
    subp = p.add_subparsers(dest="action", required=True)

    def profile_flags(sp):
        sp.add_argument("--store", help="profile store file")
        sp.add_argument("--json", action="store_true", help="machine readable output")
        sp.add_argument("--min-confidence", type=float, default=0.0)
        sp.add_argument("--show-rows", action="store_true")
        return sp

    a = profile_flags(subp.add_parser("add", help="create a user's profile from samples"))
    a.add_argument("name")
    a.add_argument("files", nargs="*")
    a.add_argument("--lang")
    a.add_argument("--replace", action="store_true", help="start over instead of merging")
    s = profile_flags(subp.add_parser("show", help="print a stored profile"))
    s.add_argument("name")
    u = profile_flags(subp.add_parser("update", help="merge more samples into a stored profile"))
    u.add_argument("name")
    u.add_argument("files", nargs="*")
    u.add_argument("--lang")
    profile_flags(subp.add_parser("list", help="list stored profiles"))
    d = profile_flags(subp.add_parser("delete", help="remove a profile"))
    d.add_argument("name")
    r = profile_flags(subp.add_parser("rename", help="rename a profile"))
    r.add_argument("name")
    r.add_argument("new_name")
    e = profile_flags(subp.add_parser("export", help="print (or write) a profile as JSON"))
    e.add_argument("name")
    e.add_argument("--out")
    i = profile_flags(subp.add_parser("import", help="load a profile JSON into the store"))
    i.add_argument("file")
    i.add_argument("name", nargs="?")
    i.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("format", help="reformat code to match a style profile")
    add_common(p, "source files (default: stdin)")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--user", help="use this stored profile")
    target.add_argument("--profile", help="use a profile JSON file")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--write", "-i", action="store_true", help="rewrite files in place")
    mode.add_argument("--check", action="store_true", help="exit 1 if anything would change")
    p.add_argument("--diff", action="store_true", help="show a unified diff for each change")
    p.add_argument("--rules", help="comma separated rule keys to apply (see 'cf rules')")
    p.add_argument("--min-confidence", type=float, default=0.5, help="skip observed rules below this confidence")
    p.add_argument("--observed-only", action="store_true", help="never fall back to family defaults")
    p.add_argument("--no-wrap", action="store_true", help="do not split rows longer than width.limit")
    p.add_argument("--eol", choices=("keep", "lf", "crlf"), default="keep")
    p.set_defaults(func=cmd_format)

    p = sub.add_parser("rules", help="list the style rules the kernel understands")
    p.set_defaults(func=cmd_rules)
    p = sub.add_parser("languages", help="list supported languages")
    p.set_defaults(func=cmd_languages)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "max_findings", None) == 0:
        args.all = True
    try:
        return args.func(args)
    except (InputError, StoreError) as exc:
        print(f"cf: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
