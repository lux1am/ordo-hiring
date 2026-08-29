import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures"

QUESTIONS = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))

# --- Заведомо проходной набор ответов -------------------------------------
#
# Один на все тесты: анкета проверяется в шести местах, и шесть своих копий
# этих словарей разошлись бы при первой же правке questions.json.

PASSING_STOPS = {
    "stop_1_sdelno": "Да, полностью готов",
    "stop_2_chasy": "15–19",
    "stop_3_svyaz": "Да",
    "stop_4_oformlenie": "ГПХ",
    "stop_5_tehnika": "Да, всё есть",
    "stop_6_zapis": "Скорее да",
}

# Часовой пояс — уточнение к третьему стоп-вопросу. В форме это отдельное
# поле, в листе оно приписано к ячейке своего вопроса.
TZ_FIELD = "stop_3_svyaz_tz"
TZ_VALUE = "МСК"

SHEET_STOPS = {
    **PASSING_STOPS,
    "stop_3_svyaz": f"Да — Ваш часовой пояс: {TZ_VALUE}",
}

# Ответы на вопросы-флаги, не дающие ни одного флага.
CLEAN_PROFILE_FIELDS = {
    "flag_motive": "Высокий доход без потолка",
    "flag_income": "80 000–120 000 ₽",
    "flag_rank_income": "1",
    "flag_rank_schedule": "2",
    "flag_rank_growth": "3",
    "flag_rank_stability": "4",
    "flag_meetings": "Достижимо при усилиях",
    "flag_goals": "РОП",
}

CLEAN_PROFILE = {
    "flag_motive": ["Высокий доход без потолка"],
    "flag_income": "80 000–120 000 ₽",
    "flag_rank": {
        "flag_rank_income": "1",
        "flag_rank_schedule": "2",
        "flag_rank_growth": "3",
        "flag_rank_stability": "4",
    },
    "flag_meetings": "Достижимо при усилиях",
    "flag_goals": "РОП",
}


def choice_fields(**over) -> dict:
    """Поля формы для стоп-вопросов и вопросов-флагов."""
    data = {**PASSING_STOPS, TZ_FIELD: TZ_VALUE, **CLEAN_PROFILE_FIELDS}
    data.update(over)
    return data


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
