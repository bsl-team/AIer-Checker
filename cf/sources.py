"""Reading and writing source files, faithfully to their encoding.

Shared by the CLI and by :mod:`cf.similarity`, so the byte-level contract - a file is written
back in the encoding it was read as, and a BOM is never invented - lives in exactly one place.
"""
from __future__ import annotations

import codecs

from .i18n import t

ENCODINGS = ("utf-8", "gbk", "cp936", "big5", "cp1252", "latin-1")


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


def read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise InputError(t("e.read_file", path=path, error=exc.strerror or exc)) from exc


def read_source(path: str) -> tuple[str, str]:
    return decode_bytes(read_bytes(path))


def write_source(path: str, text: str, encoding: str) -> None:
    try:
        with open(path, "wb") as fh:
            fh.write(text.encode(encoding, errors="replace"))
    except OSError as exc:
        raise InputError(t("e.write_file", path=path, error=exc.strerror or exc)) from exc
