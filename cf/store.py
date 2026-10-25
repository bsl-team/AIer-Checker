"""Persistent store of per-user style profiles (one JSON file, atomic writes)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .document import Document
from .i18n import t
from .extract import extract, extract_all
from .profile import Profile

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".code-feature")
DEFAULT_FILE = os.path.join(DEFAULT_DIR, "profiles.json")
ENV_VAR = "CF_STORE"
STORE_VERSION = 1
LOCK_TIMEOUT = 10.0
LOCK_RETRY = 0.05


class StoreError(Exception):
    pass


def _lock_acquire(handle) -> bool:
    """Take an exclusive advisory lock without blocking; False if someone else holds it."""
    try:
        import msvcrt  # Windows
    except ImportError:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _lock_release(handle) -> None:
    try:
        import msvcrt
    except ImportError:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _store_lock(path: str):
    """Serialise read-modify-write on the store across processes.

    A sidecar lock file is used rather than the store itself, because ``os.replace`` swaps the
    data file's inode and a lock on it would not protect the next writer.  Inside the lock
    :meth:`ProfileStore.flush` re-reads the file and keeps users it does not know about, so two
    processes adding different users both survive.
    """
    lock_path = path + ".lock"
    folder = os.path.dirname(lock_path) or "."
    os.makedirs(folder, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT
    handle = open(lock_path, "a+b")
    try:
        while not _lock_acquire(handle):
            if time.monotonic() >= deadline:
                raise StoreError(t("e.store_locked", path=path))
            time.sleep(LOCK_RETRY)
        yield
    finally:
        try:
            _lock_release(handle)
        except OSError:
            pass
        handle.close()


@dataclass
class UserSummary:
    user: str
    language: str
    family: str
    samples: int
    updated: float
    confident_features: int
    rows: int


def store_path(explicit: str | None = None) -> str:
    """Resolution order: --store argument, CF_STORE env var, ~/.code-feature/profiles.json."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get(ENV_VAR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return DEFAULT_FILE


class ProfileStore:
    """A user -> profile map on disk. Missing users are ``None``, never an error."""

    def __init__(self, path: str | None = None):
        self.path = store_path(path)
        self._users: dict[str, Profile] | None = None

    # -- file access ------------------------------------------------------
    def _read_from_file(self) -> dict[str, Profile]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StoreError(t("e.store_json", path=self.path, error=exc)) from exc
        except OSError as exc:
            raise StoreError(t("e.store_read", path=self.path, error=exc)) from exc
        users = {}
        for name, data in (raw.get("users") or {}).items():
            try:
                users[name] = Profile.from_dict(data)
            except Exception as exc:
                raise StoreError(t("e.store_damaged", name=repr(name), path=self.path, error=exc)) from exc
        return users

    def _read(self) -> dict[str, Profile]:
        if self._users is None:
            self._users = self._read_from_file()
        return self._users

    def flush(self) -> None:
        users = self._read()
        with _store_lock(self.path):
            on_disk = self._read_from_file()
            # keep users another process wrote while we held our own copy; our entries win for
            # the same user, because we are the one that just changed them
            for name, profile in on_disk.items():
                users.setdefault(name, profile)
            self._users = users
            payload = {
                "version": STORE_VERSION,
                "updated": time.time(),
                "users": {name: p.to_dict() for name, p in sorted(users.items())},
            }
            folder = os.path.dirname(self.path) or "."
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".profiles-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(payload, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp, self.path)
            except OSError as exc:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise StoreError(t("e.store_write", path=self.path, error=exc)) from exc

    # -- users ------------------------------------------------------------
    def users(self) -> list[str]:
        return sorted(self._read())

    def summaries(self) -> list[UserSummary]:
        out = []
        for name, prof in sorted(self._read().items()):
            summary = prof.summary()
            out.append(
                UserSummary(
                    user=name,
                    language=prof.language,
                    family=prof.family,
                    samples=len(prof.samples),
                    updated=prof.updated,
                    confident_features=summary["confident_features"],
                    rows=sum(s.rows for s in prof.samples),
                )
            )
        return out

    def get(self, user: str) -> Profile | None:
        return self._read().get(user)

    def require(self, user: str) -> Profile:
        prof = self.get(user)
        if prof is None:
            raise StoreError(t("e.no_profile", name=repr(user), path=self.path))
        return prof

    def put(self, profile: Profile) -> Profile:
        if not profile.user or not profile.user.strip():
            raise StoreError(t("e.user_required"))
        profile.user = profile.user.strip()
        self._read()
        existing = self._users.get(profile.user)
        if existing is not None:
            profile.created = existing.created  # re-learning keeps the original first-seen date
        self._users[profile.user] = profile
        return profile

    def add_from_samples(self, user: str, docs: list[Document], replace: bool = False) -> Profile:
        """Create (or extend) a user's profile from a batch of source files."""
        fresh = extract_all(docs)
        fresh.user = user.strip() if user and user.strip() else "default"
        current = self.get(fresh.user)
        if current is None or replace:
            self.put(fresh)
            return fresh
        current.merge(fresh)
        current.language = fresh.language if fresh.samples else current.language
        self.put(current)
        return current

    def remove(self, user: str) -> bool:
        if user in self._read():
            del self._users[user]
            return True
        return False

    def rename(self, old: str, new: str) -> Profile:
        prof = self.require(old)
        if not new.strip() or new in self._read():
            raise StoreError(t("e.rename_taken", old=repr(old), new=repr(new)))
        del self._users[old]
        prof.user = new.strip()
        self.put(prof)
        return prof

    # -- import/export -----------------------------------------------------
    def export(self, user: str) -> str:
        return json.dumps(self.require(user).to_dict(), indent=2, ensure_ascii=False) + "\n"

    def import_json(self, text: str, user: str | None = None, replace: bool = False) -> Profile:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StoreError(t("e.import_json", error=exc)) from exc
        if "users" in data:  # a whole store file
            names = list(data["users"])
            if not names:
                raise StoreError(t("e.import_no_users"))
            chosen = user if user in data["users"] else names[0]
            prof = Profile.from_dict(data["users"][chosen])
        else:
            prof = Profile.from_dict(data)
        prof.user = (user or prof.user or "default").strip()
        current = self.get(prof.user)
        if current is not None and not replace:
            current.merge(prof)
            prof = current
        self.put(prof)
        return prof
