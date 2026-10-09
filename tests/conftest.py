import hashlib

import pytest

from people_app.service import ROOT

SEED_FILES = sorted((ROOT / "data").glob("*.json")) + sorted((ROOT / "policy").glob("*.json"))


def _hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in SEED_FILES}


@pytest.fixture(scope="session", autouse=True)
def seed_files_unchanged():
    before = _hashes()
    yield
    assert _hashes() == before, "Seed files in data/ or policy/ were modified"
