import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixtures() -> list[dict]:
    out = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["_file"] = path.name
        out.append(data)
    return out


ALL_FIXTURES = load_fixtures()


def pytest_generate_tests(metafunc):
    if "fixture" in metafunc.fixturenames:
        metafunc.parametrize(
            "fixture", ALL_FIXTURES, ids=[f["_file"].replace(".json", "") for f in ALL_FIXTURES]
        )


@pytest.fixture(scope="session")
def fixtures():
    return ALL_FIXTURES
