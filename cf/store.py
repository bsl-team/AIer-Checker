"""Persistent store of per-user style profiles (one JSON file, atomic writes)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass

from .document import Document
from .extract import extract, extract_all
from .profile import Profile

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".code-feature")
DEFAULT_FILE = os.path.join(DEFAULT_DIR, "profiles.json")
ENV_VAR = "CF_STORE"
STORE_VERSION = 1


class StoreError(Exception):
    pass


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
    def _read(self) -> dict[str, Profile]:
        if self._users is not None:
            return self._users
        if not os.path.exists(self.path):
            self._users = {}
            return self._users
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StoreError(f"profile store {self.path} is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise StoreError(f"cannot read profile store {self.path}: {exc}") from exc
        users = {}
        for name, data in (raw.get("users") or {}).items():
            try:
                users[name] = Profile.from_dict(data)
            except Exception as exc:
                raise StoreError(f"profile {name!r} in {self.path} is damaged: {exc}") from exc
        self._users = users
        return users

    def flush(self) -> None:
        users = self._read()
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
            raise StoreError(f"cannot write profile store {self.path}: {exc}") from exc

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
            raise StoreError(f"no style profile stored for user {user!r} (in {self.path})")
        return prof

    def put(self, profile: Profile) -> Profile:
        if not profile.user or not profile.user.strip():
            raise StoreError("profile needs a non-empty user name")
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
            raise StoreError(f"cannot rename {old!r} to {new!r}: name taken or empty")
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
            raise StoreError(f"profile text is not valid JSON: {exc}") from exc
        if "users" in data:  # a whole store file
            names = list(data["users"])
            if not names:
                raise StoreError("profile file contains no users")
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
