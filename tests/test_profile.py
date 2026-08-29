"""Разбор стоп-вопросов и вопросов-флагов из полей формы.

Ошибка обязана называть своё поле: форма раскладывает тексты под вопросами
по этому имени, и общая полоса вместо подписи под вопросом означала бы, что
кандидат ищет ошибку глазами по всей странице.
"""
import pytest

from app import profile, ru
from tests.conftest import CLEAN_PROFILE, CLEAN_PROFILE_FIELDS, TZ_FIELD, choice_fields


# ------------------------- стоп-вопросы -----------------------------------

def test_valid_stops():
    stops, errors = profile.validate_stops(choice_fields())
    assert errors == {}
    assert stops["stop_1_sdelno"] == "Да, полностью готов"
    assert stops["stop_3_svyaz"] == "Да — Ваш часовой пояс: МСК"


def test_missing_stop_is_pinpointed():
    _, errors = profile.validate_stops(choice_fields(stop_2_chasy=""))
    assert errors == {"stop_2_chasy": profile.EMPTY_CHOICE_RU}


def test_unknown_option_is_rejected():
    """Значение мимо списка приходит только в обход формы."""
    _, errors = profile.validate_stops(choice_fields(stop_4_oformlenie="ИП"))
    assert errors == {"stop_4_oformlenie": profile.UNKNOWN_CHOICE_RU}


def test_timezone_is_required():
    fields = choice_fields()
    fields[TZ_FIELD] = "   "
    _, errors = profile.validate_stops(fields)
    assert errors == {TZ_FIELD: profile.EMPTY_TEXT_RU}


def test_timezone_length_limit():
    fields = choice_fields()
    fields[TZ_FIELD] = "ю" * 61
    _, errors = profile.validate_stops(fields)
    assert errors == {TZ_FIELD: profile.TOO_LONG_RU}


def test_stop_option_reads_the_cell_back():
    """Ячейку читает обработчик, и уточнение не должно ему мешать."""
    stops, _ = profile.validate_stops(choice_fields())
    assert profile.stop_option(stops["stop_3_svyaz"]) == "Да"
    assert profile.stop_option(stops["stop_1_sdelno"]) == "Да, полностью готов"
    assert profile.stop_option("") == ""


def test_option_with_dash_survives_round_trip():
    """Внутри вариантов есть короткое тире — разделитель на него не похож."""
    stops, _ = profile.validate_stops(choice_fields(stop_2_chasy="20–24"))
    assert profile.stop_option(stops["stop_2_chasy"]) == "20–24"


# ------------------------ вопросы-флаги -----------------------------------

def test_valid_profile():
    parsed, errors = profile.validate_profile(choice_fields())
    assert errors == {}
    assert parsed == CLEAN_PROFILE


def test_multi_accepts_two_choices():
    fields = choice_fields()
    fields["flag_motive"] = ["Высокий доход без потолка", "Сменить сферу"]
    parsed, errors = profile.validate_profile(fields)
    assert errors == {}
    assert parsed["flag_motive"] == ["Высокий доход без потолка", "Сменить сферу"]


def test_multi_requires_at_least_one():
    fields = choice_fields()
    fields["flag_motive"] = []
    _, errors = profile.validate_profile(fields)
    assert errors == {"flag_motive": profile.FEW_CHOICES_RU}


def test_multi_rejects_three():
    fields = choice_fields()
    fields["flag_motive"] = ["Высокий доход без потолка", "Сменить сферу", "Другое"]
    _, errors = profile.validate_profile(fields)
    assert errors == {"flag_motive": profile.MANY_CHOICES_RU}


def test_other_requires_text():
    fields = choice_fields()
    fields["flag_motive"] = ["Другое"]
    _, errors = profile.validate_profile(fields)
    assert errors == {"flag_motive_other": profile.OTHER_REQUIRED_RU}


def test_other_with_text_passes():
    fields = choice_fields()
    fields["flag_motive"] = ["Другое"]
    fields["flag_motive_other"] = "Хочу работать голосом"
    parsed, errors = profile.validate_profile(fields)
    assert errors == {}
    assert parsed["flag_motive"] == ["Другое"]


def test_other_text_length_limit():
    fields = choice_fields()
    fields["flag_motive"] = ["Другое"]
    fields["flag_motive_other"] = "я" * 121
    _, errors = profile.validate_profile(fields)
    assert errors == {"flag_motive_other": profile.TOO_LONG_RU}


def test_single_choice_required():
    _, errors = profile.validate_profile(choice_fields(flag_goals=""))
    assert errors == {"flag_goals": profile.EMPTY_CHOICE_RU}


def test_rank_requires_every_place():
    _, errors = profile.validate_profile(choice_fields(flag_rank_growth=""))
    assert errors == {"flag_rank": profile.PLACE_REQUIRED_RU}


def test_rank_rejects_duplicates():
    _, errors = profile.validate_profile(choice_fields(flag_rank_growth="1"))
    assert errors == {"flag_rank": profile.PLACE_UNIQUE_RU}


@pytest.mark.parametrize("place", ["0", "5", "первое"])
def test_rank_rejects_values_outside_the_list(place):
    _, errors = profile.validate_profile(choice_fields(flag_rank_income=place))
    assert errors == {"flag_rank": profile.PLACE_REQUIRED_RU}


def test_single_value_multi_is_accepted():
    """Без JavaScript один выбранный чекбокс приходит строкой, а не списком."""
    fields = dict(CLEAN_PROFILE_FIELDS)
    assert isinstance(fields["flag_motive"], str)
    parsed, errors = profile.validate_profile(fields)
    assert errors == {}
    assert parsed["flag_motive"] == ["Высокий доход без потолка"]


# ------------------------- ячейка ai_markers ------------------------------

def test_pack_round_trip():
    cell = profile.pack_markers(CLEAN_PROFILE, ["ровные абзацы"])
    markers, parsed = profile.unpack_markers(cell)
    assert markers == ["ровные абзацы"]
    assert parsed == CLEAN_PROFILE


def test_pack_is_readable_json():
    """Ячейку читают глазами в таблице — экранированной кириллицы там быть не должно."""
    cell = profile.pack_markers({"flag_goals": "РОП"}, [])
    assert cell == '{"markers": [], "profile": {"flag_goals": "РОП"}}'


def test_pack_of_nothing_is_an_empty_cell():
    assert profile.pack_markers({}, []) == ""
    assert profile.pack_markers(None, None) == ""


def test_pack_keeps_markers_without_profile():
    """Строка, оценённая до появления вопросов о мотивации, тоже валидна."""
    markers, parsed = profile.unpack_markers(profile.pack_markers({}, ["нет опечаток"]))
    assert markers == ["нет опечаток"] and parsed == {}


def test_old_cell_reads_as_markers():
    """До перехода на JSON в колонке лежали признаки через запятую."""
    markers, parsed = profile.unpack_markers("ровные абзацы, нет опечаток")
    assert markers == ["ровные абзацы", "нет опечаток"]
    assert parsed == {}


@pytest.mark.parametrize("cell", ["", "   ", None, "{битый", "[1, 2]", "null"])
def test_broken_cell_does_not_crash(cell):
    """Ячейку правят руками — разбор не должен ронять обработку."""
    markers, parsed = profile.unpack_markers(cell)
    assert isinstance(markers, list) and isinstance(parsed, dict)


def test_other_text_is_kept_in_the_profile():
    """Свой вариант ответа доезжает до таблицы, а не теряется по дороге."""
    fields = choice_fields()
    fields["flag_motive"] = ["Другое"]
    fields["flag_motive_other"] = "Хочу работать голосом"
    parsed, _ = profile.validate_profile(fields)
    assert parsed["flag_motive_other"] == "Хочу работать голосом"

    _, restored = profile.unpack_markers(profile.pack_markers(parsed, []))
    assert restored["flag_motive_other"] == "Хочу работать голосом"


def test_other_text_absent_when_not_chosen():
    """Пустой ключ в каждой анкете был бы шумом в и без того плотной ячейке."""
    parsed, _ = profile.validate_profile(choice_fields())
    assert "flag_motive_other" not in parsed


# --------------------------- витрина --------------------------------------

def test_profile_ru_reads_as_a_sentence():
    assert ru.profile_ru(CLEAN_PROFILE) == (
        "мотивация: высокий доход без потолка; доход 80–120к; "
        "приоритет: доход, график, рост, стабильность; "
        "встречи — достижимо при усилиях; цель — РОП"
    )


def test_profile_ru_orders_priorities_by_place():
    rank = {
        "flag_rank_stability": "1",
        "flag_rank_growth": "2",
        "flag_rank_schedule": "3",
        "flag_rank_income": "4",
    }
    text = ru.profile_ru({**CLEAN_PROFILE, "flag_rank": rank})
    assert "приоритет: стабильность, рост, график, доход" in text


@pytest.mark.parametrize(
    "option,short",
    [
        ("до 50 000 ₽", "до 50к"),
        ("50 000–80 000 ₽", "50–80к"),
        ("80 000–120 000 ₽", "80–120к"),
        ("120 000–150 000 ₽", "120–150к"),
        ("более 150 000 ₽", "более 150к"),
    ],
)
def test_money_is_shortened(option, short):
    """Витрина узкая: «80 000–120 000 ₽» в строке замечаний занимает полстроки."""
    assert ru.short_money(option) == short


def test_unknown_money_format_survives():
    assert ru.short_money("договорная") == "договорная"


def test_abbreviations_keep_their_case():
    """«роп» вместо «РОП» читалось бы как опечатка."""
    assert ru.profile_ru({**CLEAN_PROFILE, "flag_goals": "РОП"}).endswith("цель — РОП")
    assert ru.profile_ru({**CLEAN_PROFILE, "flag_goals": "Не знаю"}).endswith(
        "цель — не знаю"
    )


def test_profile_ru_shows_the_candidates_own_words():
    """Слово «Другое» рекрутёру ничего не говорит — показываем сам ответ."""
    text = ru.profile_ru({
        **CLEAN_PROFILE,
        "flag_motive": ["Другое"],
        "flag_motive_other": "Хочу работать голосом",
    })
    assert "мотивация: хочу работать голосом" in text
    assert "Другое" not in text


def test_empty_profile_gives_no_text():
    assert ru.profile_ru({}) == ""
    assert ru.profile_ru(None) == ""


def test_flags_ru_appends_the_answers():
    text = ru.flags_ru(["MOBILE"], ["A понижен до B"], CLEAN_PROFILE)
    assert text.startswith("заполнено с телефона; A понижен до B; ")
    assert "цель — РОП" in text


def test_flags_ru_without_remarks_still_shows_the_answers():
    text = ru.flags_ru([], None, CLEAN_PROFILE)
    assert text.startswith("Замечаний нет; ")
    assert "доход 80–120к" in text


def test_flags_ru_without_profile_is_unchanged():
    assert ru.flags_ru([], None, None) == "Замечаний нет"
    assert ru.flags_ru(["MOBILE"]) == "заполнено с телефона"
