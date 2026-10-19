"""Лист «Сводка для Поли»: выгрузка числами для внешнего бота.

Здесь проверяется две вещи, и вторая важнее первой.

Первая — числа. Сводка считается кодом и ложится в лист значениями, то есть
ошибка в ней не покажется ни ошибкой, ни пустотой: бот прочитает чужое
число и примет его за своё. Поэтому каждый блок собирается на выдуманной
неделе с заранее посчитанным ответом.

Вторая — персональные данные. Лист уходит наружу, в чужого бота, и это
единственный лист книги, про который известно заранее, что его прочитает
кто-то кроме Ники. В «Данных» лежат ФИО, телеграмы, ссылки на резюме,
тексты ответов и заметки о разговорах; в сводке не должно быть ни одного из
них. Проверка здесь не «поискать ФИО глазами», а белый список: каждая
ячейка листа обязана быть числом, датой, названием канала или подписью из
самого digest.py. Что угодно другое — это утечка, и тест о ней скажет.

Работа с книгой проверяется в test_migration.py — там поднят лист в памяти.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

import digest
import placements
from HEADERS import HEADERS
from app import checks, report, ru, source

TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 19, 14, 3, tzinfo=TZ)
TODAY = NOW.date()


# --------------------------- персональные данные --------------------------
#
# Эти строки заведомо лежат в «Данных» каждой анкеты фикстуры — и заведомо
# не должны попасть в сводку ни одной своей частью.

FIO = "Иванова Мария Петровна"
TELEGRAM = "@maria_ivanova"
RESUME = "https://hh.ru/resume/0ff8c1"
ANSWER = "Март 2026: 11 встреч, выручка 420 000 ₽ — подтверждаю актом"
NOTE = "созвонились 14 октября, просит выше вилку"
OUTCOME = "вышла на стажировку"

PERSONAL = (FIO, TELEGRAM, RESUME, ANSWER, NOTE, OUTCOME)


# ------------------------------- фикстуры ---------------------------------

def response_row(
    n: int,
    day: date,
    label: str = "",
    verdict: str = "A",
    total: float | str = 14,
    outcome: str = "",
    passes: bool = True,
) -> list:
    """Строка листа «Данные»: анкета целиком, с персональными данными."""
    row = [""] * len(HEADERS)

    def put(name: str, value) -> None:
        row[HEADERS.index(name)] = value

    put("id", f"id{n:03d}")
    put("created_at", f"{day.isoformat()} 10:{n:02d}:00")
    put("fio", f"{FIO} ({n})")
    put("telegram", f"{TELEGRAM}{n}")
    put("resume_url", f"{RESUME}{n}")
    for q in range(1, 6):
        put(f"q{q}_text", f"{ANSWER} ({n})")
    put("verdict", verdict)
    put("total_score", total)
    put("source", label)
    put("manual_note", NOTE)
    put("outcome", outcome)

    for field, allowed in checks.STOP_PASS.items():
        put(field, allowed[0] if passes else "Нет")
    if not passes:
        put("conditions_ru", ru.REJECTED_PREFIX + "сдельный доход без оклада")
    return row


def responses_of(rows: list[list]) -> list[report.Response]:
    return report.read_responses([list(HEADERS)] + rows)


def posts_of(rows: list[tuple]) -> list[report.Placement]:
    """Лист «Размещения»: (дата или пусто, канал). Метка — по номеру строки."""
    values = [list(placements.PLACEMENTS_HEADER)]
    for day, channel in rows:
        values.append([day.isoformat() if day else "", channel, "ссылка", ""])
    return report.read_placements(
        values,
        date_col=placements.PLACEMENT_DATE_COL,
        channel_col=placements.PLACEMENT_CHANNEL_COL,
    )


def touches_of(rows: list[tuple]) -> list[report.Outreach]:
    """Лист «Личные касания»: (дата, сколько отправлено)."""
    values = [list(placements.OUTREACH_HEADER)]
    for day, sent in rows:
        values.append(
            [day.isoformat(), sent, source.outreach_label(day), "ссылка", ""]
        )
    return report.read_outreach(
        values,
        date_col=placements.OUTREACH_DATE_COL,
        sent_col=placements.OUTREACH_SENT_COL,
    )


# Выдуманная неделя. Размещения — четыре строки листа, то есть метки
# p002…p005; у «Хедхантера» даты нет, и порядок его размещений неизвестен.
POSTS = [
    (date(2026, 10, 5), "Авито"),        # p002 — первое размещение «Авито»
    (date(2026, 10, 17), "Авито"),       # p003 — последнее
    (date(2026, 10, 18), "Телеграм"),    # p004 — размещались, откликов нет
    ("", "Хедхантер"),                   # p005 — канал без даты
]

TOUCHES = [
    (date(2026, 10, 18), 20),
    (date(2026, 10, 1), 5),              # вне окна в 14 дней
]

RESPONSES = [
    response_row(1, date(2026, 10, 6), "p002", verdict="C", total=5),
    response_row(2, date(2026, 10, 6), "p002", verdict="B", total=9,
                 outcome=OUTCOME),
    # Отсеялся на стоп-вопросах: до баллов такая анкета не доходит.
    response_row(3, date(2026, 10, 18), "p002", verdict="", total="",
                 passes=False),
    response_row(4, date(2026, 10, 18), "p003", verdict="A", total=14),
    # Без метки, сегодня: прямой переход по ссылке.
    response_row(5, TODAY, "", verdict="A", total=13),
    response_row(6, TODAY, source.outreach_label(date(2026, 10, 18)),
                 verdict="B", total=10),
    # Старая анкета: вне обоих окон, но в базе и в счёте канала.
    response_row(7, date(2026, 9, 20), "p002", verdict="A", total=12,
                 outcome="отказалась"),
]


@pytest.fixture(scope="module")
def rows() -> list[list]:
    return digest.values(
        NOW, responses_of(RESPONSES), posts_of(POSTS), touches_of(TOUCHES)
    )


def block(rows: list[list], title: str) -> list[list]:
    """Строки блока: всё между его шапкой и пустой строкой."""
    start = next(i for i, row in enumerate(rows) if row and row[0] == title)
    body = []
    for row in rows[start + 2:]:
        if not row:
            break
        body.append(row)
    return body


def header_of(rows: list[list], title: str) -> list:
    start = next(i for i, row in enumerate(rows) if row and row[0] == title)
    return rows[start + 1]


def channel(rows: list[list], name: str) -> list:
    return next(row for row in block(rows, digest.CHANNELS_TITLE) if row[0] == name)


def day_row(rows: list[list], day: date) -> list:
    return next(
        row for row in block(rows, digest.DAYS_TITLE) if row[0] == day.isoformat()
    )


def totals(rows: list[list]) -> dict:
    return {row[0]: row[1] for row in block(rows, digest.TOTALS_TITLE)}


# ------------------------------- раскладка --------------------------------

def test_three_blocks_each_with_a_header_row(rows):
    titles = [digest.CHANNELS_TITLE, digest.DAYS_TITLE, digest.TOTALS_TITLE]
    positions = [
        next(i for i, row in enumerate(rows) if row and row[0] == title)
        for title in titles
    ]
    assert positions == sorted(positions), "блоки идут не в том порядке"
    assert positions[0] == 0, "первый блок начинается с первой строки листа"

    assert header_of(rows, digest.CHANNELS_TITLE) == digest.CHANNELS_HEADER
    assert header_of(rows, digest.DAYS_TITLE) == digest.DAYS_HEADER
    assert header_of(rows, digest.TOTALS_TITLE) == digest.TOTALS_HEADER


def test_header_asks_for_everything_the_bot_reads(rows):
    assert digest.CHANNELS_HEADER[:6] == [
        "Канал", "Размещений всего", "Откликов всего", "A", "B", "C",
    ]
    assert digest.DAYS_HEADER[0] == "Дата"
    assert digest.DIGEST_COLS == len(digest.CHANNELS_HEADER), (
        "ширина листа — по самому широкому блоку"
    )
    for header in (digest.DAYS_HEADER, digest.TOTALS_HEADER):
        assert len(header) <= digest.DIGEST_COLS


def test_every_row_is_padded_to_the_same_width(rows):
    """Короткая строка оставила бы на месте хвоста числа прошлой сборки:
    запись задаёт только те ячейки, которые в неё переданы."""
    padded = digest.padded(rows)
    assert {len(row) for row in padded} == {digest.DIGEST_COLS}
    assert len(padded) == len(rows)


def test_blocks_are_separated_by_a_blank_line(rows):
    assert [] in rows
    assert rows.count([]) == 2, "две пустые строки на три блока"


# -------------------------------- КАНАЛЫ ----------------------------------

def test_channel_counts_placements_and_responses_for_all_time(rows):
    name, posts, answers, a, b, c = channel(rows, "Авито")[:6]
    assert (name, posts, answers) == ("Авито", 2, 5)
    # Четыре отклика по p002 и один по p003; у третьего вердикта нет —
    # он отсеялся на стоп-вопросах.
    assert (a, b, c) == (2, 1, 1)


def test_average_score_counts_only_those_who_were_scored(rows):
    """Как в «Эффективности»: отсеявшиеся на стоп-вопросах в знаменателе не
    стоят. Не прошедшие скоринг — тоже: их баллы появятся через минуту."""
    assert channel(rows, "Авито")[6] == 10.0   # (5 + 9 + 14 + 12) / 4


def test_average_is_blank_when_nobody_was_scored(rows):
    """Ноль здесь означал бы «все ответили на нуль баллов»."""
    assert channel(rows, "Телеграм")[6] == ""


def test_first_and_last_placement_yield_show_a_fading_channel(rows):
    row = channel(rows, "Авито")
    assert row[7] == 4, "первое размещение — самая ранняя дата"
    assert row[8] == 1, "последнее — самая поздняя"
    assert row[9] == "2026-10-17", "дата последнего размещения"


def test_channel_without_a_date_has_no_order_and_no_last_day(rows):
    row = channel(rows, "Хедхантер")
    assert row[1:3] == [1, 0], "размещение считается и без даты"
    assert row[7:] == ["", "", ""], "порядок размещений неизвестен"


def test_channels_are_sorted_by_what_they_brought(rows):
    assert [row[0] for row in block(rows, digest.CHANNELS_TITLE)] == [
        "Авито", "Телеграм", "Хедхантер",
    ]


def test_placement_without_a_channel_is_still_counted():
    """Канал забыли выбрать — работа всё равно была сделана, и её отклики
    не должны пропасть: иначе блоки не сойдутся с ИТОГО."""
    rows = digest.values(
        NOW,
        responses_of([response_row(1, TODAY, "p002", verdict="A", total=12)]),
        posts_of([(TODAY, "")]),
        touches_of([]),
    )
    assert channel(rows, report.NO_CHANNEL)[1:3] == [1, 1]


def test_strange_label_belongs_to_no_channel():
    """Метка, которой нет ни у одного размещения, не должна приписаться
    первому попавшемуся каналу."""
    rows = digest.values(
        NOW,
        responses_of([response_row(1, TODAY, "p099", verdict="A", total=12)]),
        posts_of([(TODAY, "Авито")]),
        touches_of([]),
    )
    assert channel(rows, "Авито")[2] == 0
    assert totals(rows)[digest.TOTAL_CANDIDATES] == 1


# --------------------------------- ДНИ ------------------------------------

def test_days_cover_fourteen_calendar_days_newest_first(rows):
    days = block(rows, digest.DAYS_TITLE)
    assert len(days) == digest.DAYS_WINDOW == 14
    assert days[0][0] == TODAY.isoformat(), "свежий день сверху"
    assert days[-1][0] == (TODAY - timedelta(days=13)).isoformat()


def test_day_row_counts_work_and_what_it_brought(rows):
    assert day_row(rows, date(2026, 10, 18)) == ["2026-10-18", 1, 20, 2, 1]
    assert day_row(rows, date(2026, 10, 17)) == ["2026-10-17", 1, 0, 0, 0]
    assert day_row(rows, TODAY) == [TODAY.isoformat(), 0, 0, 2, 1]


def test_quiet_day_stays_a_row_of_zeros(rows):
    """Пропавшая строка читалась бы как пропавший день."""
    assert day_row(rows, date(2026, 10, 10)) == ["2026-10-10", 0, 0, 0, 0]


def test_what_happened_before_the_window_stays_out_of_it(rows):
    """Размещение «Авито» от 5 октября и касания от 1 октября в окно не
    входят — но в блоке КАНАЛОВ они есть, он за всё время."""
    shown = {row[0] for row in block(rows, digest.DAYS_TITLE)}
    assert "2026-10-05" not in shown and "2026-10-01" not in shown
    assert sum(row[2] for row in block(rows, digest.DAYS_TITLE)) == 20


# -------------------------------- ИТОГО -----------------------------------

def test_totals_count_the_base_and_the_last_seven_days(rows):
    numbers = totals(rows)
    assert numbers[digest.TOTAL_CANDIDATES] == 7
    assert numbers[digest.RESPONSES_RECENT] == 4
    assert numbers[digest.UNLABELLED_RECENT] == 1


def test_strong_candidates_waiting_for_a_decision(rows):
    """Список дел: уровень A, у которого «Что было дальше» пуста. Считается
    за всё время — сильный кандидат, которого не отработали месяц назад, из
    него выпадать не должен."""
    assert totals(rows)[digest.STRONG_UNTOUCHED] == 2


def test_filled_outcome_leaves_the_waiting_list():
    made = [response_row(1, TODAY, "p002", verdict="A", total=14)]
    before = digest.values(NOW, responses_of(made), posts_of(POSTS), [])
    assert totals(before)[digest.STRONG_UNTOUCHED] == 1

    made = [
        response_row(1, TODAY, "p002", verdict="A", total=14, outcome=OUTCOME)
    ]
    after = digest.values(NOW, responses_of(made), posts_of(POSTS), [])
    assert totals(after)[digest.STRONG_UNTOUCHED] == 0


def test_update_time_is_a_date_and_a_time(rows):
    assert totals(rows)[digest.UPDATED_AT] == "2026-10-19 14:03"


def test_empty_book_still_gives_three_blocks():
    """Книга собрана, учёт не начат: блоки на месте, строк в них нет."""
    rows = digest.values(NOW, [], [], [])
    assert block(rows, digest.CHANNELS_TITLE) == []
    assert len(block(rows, digest.DAYS_TITLE)) == digest.DAYS_WINDOW
    assert totals(rows)[digest.TOTAL_CANDIDATES] == 0


# ------------------------- персональных данных нет ------------------------

def iso_date(text: str) -> bool:
    try:
        datetime.strptime(text, digest.DATE_FORMAT)
    except ValueError:
        try:
            datetime.strptime(text, digest.UPDATED_FORMAT)
        except ValueError:
            return False
    return True


def allowed_text() -> set[str]:
    """Единственные слова, которым место в этом листе."""
    return {
        "",
        digest.CHANNELS_TITLE,
        digest.DAYS_TITLE,
        digest.TOTALS_TITLE,
        *digest.CHANNELS_HEADER,
        *digest.DAYS_HEADER,
        *digest.TOTALS_HEADER,
        digest.TOTAL_CANDIDATES,
        digest.RESPONSES_RECENT,
        digest.STRONG_UNTOUCHED,
        digest.UNLABELLED_RECENT,
        digest.UPDATED_AT,
        report.NO_CHANNEL,
        # Названия каналов — единственное, что приходит в лист из книги.
        *(channel for _, channel in POSTS if channel),
    }


def test_no_personal_data_leaves_the_book(rows):
    """Белый список: каждая ячейка — число, дата, название канала или
    подпись из digest.py. Что угодно другое — утечка.

    Проверка намеренно устроена «от разрешённого», а не «от запрещённого»:
    новую колонку с текстом из «Данных» список поймает, а поиск ФИО по
    образцу — нет.
    """
    known = allowed_text()
    for row in digest.padded(rows):
        for value in row:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                continue
            assert isinstance(value, str), (row, value)
            assert value in known or iso_date(value), (
                f"в сводку попало постороннее: {value!r} в строке {row}"
            )


@pytest.mark.parametrize("secret", PERSONAL, ids=range(len(PERSONAL)))
def test_not_a_word_of_the_candidate_himself(rows, secret):
    """То же, но прямо: ФИО, телеграм, резюме, текст ответа, заметка и
    «Что было дальше» лежат в «Данных» каждой анкеты фикстуры."""
    text = "\n".join(
        "\t".join(str(value) for value in row) for row in digest.padded(rows)
    )
    assert secret not in text
    for word in secret.split():
        if len(word) > 5:
            assert word not in text, word


def test_outcome_is_read_only_as_filled_or_not(rows):
    """Колонка «Что было дальше» — свободный текст про живого человека.
    В сводку из неё попадает одно число: сколько сильных не разобрано."""
    assert OUTCOME not in str(rows)
    assert digest.OUTCOME_COLUMN in digest.STRONG_UNTOUCHED
