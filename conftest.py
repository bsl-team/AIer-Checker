import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def default_locale():
    """cf.i18n holds one process-wide locale; pin it to English between tests."""
    from cf.i18n import set_locale

    set_locale("en")
    yield
    set_locale("en")
