import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    """A fresh copy of the supplied snapshot (plus the two added tables)."""
    p = tmp_path / "test.db"
    init_db(p)
    return p


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


T = lambda hhmm: f"2026-08-04T{hhmm}:00+05:30"   # noqa: E731
P = lambda n: f"+91-90000100{n:02d}"              # noqa: E731
