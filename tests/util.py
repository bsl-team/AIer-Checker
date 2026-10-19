import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTDATA = os.path.join(ROOT, "testdata")


def sample_path(name: str) -> str:
    return os.path.join(TESTDATA, name)


def read_sample(name: str) -> str:
    with open(sample_path(name), encoding="utf-8") as fh:
        return fh.read()
