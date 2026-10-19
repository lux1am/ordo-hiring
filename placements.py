#!/usr/bin/env python3
"""Учёт размещений: четыре листа, которыми Ника пользуется каждый день.

    «Каналы»           справочник площадок: название, ссылка, тип
    «Размещения»       строка на пост: дата, канал из списка, готовая ссылка
    «Личные касания»   строка на день: дата, сколько отправлено, готовая ссылка
    «Эффективность»    сводка формулами: что принесло отклики, а что нет

Главное правило здесь одно: метку не вводит руками никто. Её выдаёт
таблица — по номеру строки в «Размещениях» (`p002`) и по дате в «Личных
касаниях» (`lk_16_10`), — а Ника копирует готовую ссылку целиком и
вставляет в пост или в сообщение. Одна опечатка в метке означала бы
отклики, приписанные не тому каналу, и неверную сводку, по которой
принимают решения.

Ручные колонки скрипт не трогает вовсе: он пишет шапку, колонки с
формулами и лист «Эффективность», собранный формулами целиком. Поэтому
повторный запуск безопасен в любой момент — введённое Никой остаётся на
месте.

Строки добавляйте вниз. Вставка строки между заполненными сдвигает номера,
а вместе с ними и метки размещений: ссылка, уже ушедшая в пост, начнёт
считаться другому каналу. Предупреждение об этом стоит примечанием к шапке.

Сводка «Эффективности» собрана одной формулой в A2, разворачивающей массив
на весь лист, а не строкой на строку. Иначе её пришлось бы пересобирать
скриптом после каждого нового размещения, и между запусками она показывала
бы устаревшие числа.

Листы создаются из sync_questions.py — вместе с «Карточкой» и видимостью
листов, одним движением.
"""
import logging

import gspread
from gspread.utils import ValidationConditionType

from HEADERS import FIRST_DATA_ROW, PREPARED_ROWS, col_letter, index_of
from app import ru, source
from app.config import (
    CHANNELS_SHEET,
    EFFICIENCY_SHEET,
    FORM_DOMAIN,
    OUTREACH_SHEET,
    PLACEMENTS_SHEET,
    PLAN_OUTREACH_DAY,
    WORKSHEET_NAME,
)

log = logging.getLogger("placements")

# --------------------------- раскладка листов -----------------------------
#
# Колонки нумеруются с единицы, как в Sheets. Ручные идут первыми — в них
# работает человек, и уводить их за формулы значит заставлять его каждый раз
# проезжать мимо.

CHANNELS_HEADER = ["Название", "Ссылка", "Тип площадки"]
CHANNEL_NAME_COL = 1
CHANNEL_ROWS = 100          # площадок столько не наберётся, но место дешёвое

PLACEMENTS_HEADER = ["Дата", "Канал", "Ссылка для размещения", "Откликов"]
PLACEMENT_DATE_COL = 1
PLACEMENT_CHANNEL_COL = 2
PLACEMENT_LINK_COL = 3
PLACEMENT_RESPONSES_COL = 4
# План — двадцать размещений в день, то есть около четырёхсот строк в месяц.
# Размечаем с запасом на пару месяцев: лист, у которого кончились строки с
# формулами, выдаёт пустую ссылку, и это замечают не сразу. Дорастить его
# можно в любой момент — повторный запуск скрипта ручные колонки не трогает.
PLACEMENT_ROWS = 1000

OUTREACH_HEADER = ["Дата", "Отправлено", "Метка", "Ссылка для размещения", "Откликов"]
OUTREACH_DATE_COL = 1
OUTREACH_SENT_COL = 2
OUTREACH_LABEL_COL = 3
OUTREACH_LINK_COL = 4
OUTREACH_RESPONSES_COL = 5
OUTREACH_ROWS = 400         # строка на день, с запасом больше года

EFFICIENCY_HEADER = [
    "Дата",
    "Канал",
    "Откликов",
    "Сильных (A)",
    "Спорных (B)",
    "Отказов (C)",
    "Средний балл",
    "Отсеялись на стоп-вопросах",
]
EFFICIENCY_COLS = len(EFFICIENCY_HEADER)
EFFICIENCY_DATE_COL = 1    # A — дата размещения
EFFICIENCY_SCORE_COL = 7   # G — средний балл

# Строк под сводку: размещения, каналы, неизвестные метки и подписи блоков.
# Массив в A2 разворачивается вниз, и не хватившие строки Sheets не добавит
# сам — он покажет ошибку вместо всей сводки.
EFFICIENCY_ROWS = 1 + PLACEMENT_ROWS + CHANNEL_ROWS + PREPARED_ROWS + 10

# Подписи блоков сводки. Это же и якоря: по ним блок находят тесты.
CHANNELS_BLOCK_TITLE = "ПО КАНАЛАМ: СКОЛЬКО РАЗ РАЗМЕЩАЛИСЬ И ЧТО ПРИШЛО"
CHANNELS_BLOCK_HEADER = [
    "Канал", "Размещений", "Откликов", "Первое размещение", "Последнее размещение"
]
OUTREACH_BLOCK_TITLE = "ЛИЧНЫЕ КАСАНИЯ ЗА ВСЁ ВРЕМЯ"
OUTREACH_SENT_LABEL = "Отправлено сообщений"
OUTREACH_RESPONSES_LABEL = "Откликов из личек"
UNKNOWN_BLOCK_TITLE = "МЕТКА ЕСТЬ, А РАЗМЕЩЕНИЯ С НЕЙ НЕТ"
UNKNOWN_ROW_NOTE = "нет в «Размещениях»"
UNKNOWN_EMPTY_TEXT = "таких откликов нет"
NO_LABEL_TITLE = "Отклики без метки"
NO_PLACEMENTS_TEXT = "Размещений пока нет — заполните лист «Размещения»"

# Примечания к шапке. Их читают ровно там, где ошибаются.
NOTES = {
    PLACEMENTS_SHEET: {
        "B1": "Канал выбирается из списка. Новую площадку сначала заведите "
              "в листе «Каналы» — тогда она появится здесь.",
        "C1": "Скопируйте ссылку целиком и вставьте в пост. Метку в ней не "
              "меняйте и руками нигде не вводите: по ней отклик находит своё "
              "размещение.\n\n"
              "Новые размещения добавляйте вниз. Вставленная между "
              "заполненными строка сдвигает номера, и ссылка, уже ушедшая в "
              "пост, начнёт считаться другому каналу.",
        "D1": "Считается формулой по метке из колонки C. Пусто — размещения "
              "ещё нет, ноль — отклики не приходили.",
    },
    OUTREACH_SHEET: {
        "A1": "Одна строка на день. Дату вводите датой, а не текстом: из неё "
              "складывается метка.",
        "B1": "Сколько сообщений отправлено за день. План — "
              f"{PLAN_OUTREACH_DAY} в день.",
        "D1": "Скопируйте ссылку целиком и вставьте в сообщение. Ссылка одна "
              "на весь день — метка собрана из даты.",
    },
    CHANNELS_SHEET: {
        "A1": "Название, как оно будет выбираться в «Размещениях». "
              "Переименуете позже — прежние размещения останутся с прежним "
              "названием.",
        "B1": "Любой текст или пусто: проверки здесь нет. Площадка может "
              "быть любой, не только телеграм.",
        "C1": "Телеграм-канал, чат, доска вакансий, форум — что угодно, "
              "свободным текстом.",
    },
}

DATE_FORMAT = "yyyy-mm-dd"
SCORE_FORMAT = "0.0"

# Формат даты в метке личных касаний — тот же, что в app/source.py, но
# словарём Sheets: `%d_%m` против `dd_mm`. Совпадение закреплено тестом.
SHEETS_DATE_TOKENS = {"%d": "dd", "%m": "mm", "%Y": "yyyy", "%y": "yy"}


def sheets_outreach_format() -> str:
    """Формат метки личных касаний для формулы TEXT: «%d_%m» -> «dd_mm»."""
    out = source.OUTREACH_DATE_FORMAT
    for py, sheet in SHEETS_DATE_TOKENS.items():
        out = out.replace(py, sheet)
    return out


# --------------------------- куски формул ---------------------------------
#
# Формулы собираются здесь и без обращения к книге: тогда их проверяет тест,
# а не красные #REF! на боевой таблице.


def lit(text: str) -> str:
    """Строковый литерал формулы: кавычки внутри удваиваются."""
    return '"' + str(text).replace('"', '""') + '"'


def data_range(column: str) -> str:
    """Колонка листа «Данные» со второй строки и до конца: 'Данные'!$BC$2:$BC.

    Диапазон открытый: анкет станет больше трёхсот, и упираться сводке в
    разметку листа незачем. Все такие диапазоны одной высоты — иначе
    COUNTIFS по двум колонкам отказался бы считать.
    """
    letter = col_letter(index_of(column))
    return f"'{WORKSHEET_NAME}'!${letter}${FIRST_DATA_ROW}:${letter}"


def sheet_range(title: str, column: int, last_row: int) -> str:
    """Колонка другого листа с границей: 'Размещения'!$A$2:$A$201.

    Здесь граница обязательна: по высоте этого диапазона считаются номера
    строк, из которых складываются метки.
    """
    letter = col_letter(column)
    return f"'{title}'!${letter}${FIRST_DATA_ROW}:${letter}${last_row}"


LINK_PREFIX = FORM_DOMAIN + "?src="


def form_link(label_expression: str) -> str:
    """Выражение «домен плюс метка»: hr.aiordo.ru?src=p002."""
    return f"{lit(LINK_PREFIX)}&{label_expression}"


def form_link_of(label: str) -> str:
    """Готовая ссылка одним литералом — метка известна на сборке."""
    return lit(LINK_PREFIX + label)


# --- «Размещения» ---------------------------------------------------------

def placement_link_formula(row: int) -> str:
    """Готовая ссылка для поста. Пустая строка — пустая ячейка.

    Метка берётся из номера строки и подставляется текстом, а не считается
    формулой от ROW(): строку могут вставить или удалить, и тогда ссылка,
    уже ушедшая в пост, поменяла бы смысл прямо в таблице.
    """
    date_cell = f"${col_letter(PLACEMENT_DATE_COL)}{row}"
    channel_cell = f"${col_letter(PLACEMENT_CHANNEL_COL)}{row}"
    return (
        f'=IF(AND({date_cell}="",{channel_cell}=""),"",'
        f"{form_link_of(source.label(row))})"
    )


def placement_responses_formula(row: int) -> str:
    """Сколько анкет пришло по метке этой строки."""
    link_cell = f"${col_letter(PLACEMENT_LINK_COL)}{row}"
    return (
        f'=IF({link_cell}="","",'
        f"COUNTIF({data_range('source')},{lit(source.label(row))}))"
    )


# --- «Личные касания» -----------------------------------------------------

def outreach_label_formula(row: int) -> str:
    """Метка дня: lk_16_10. Дата текстом даёт пустую ячейку, а не ошибку."""
    date_cell = f"${col_letter(OUTREACH_DATE_COL)}{row}"
    return (
        f'=IF({date_cell}="","",'
        f'IFERROR({lit(source.OUTREACH_PREFIX)}&'
        f'TEXT({date_cell},{lit(sheets_outreach_format())}),""))'
    )


def outreach_link_formula(row: int) -> str:
    label_cell = f"${col_letter(OUTREACH_LABEL_COL)}{row}"
    return f'=IF({label_cell}="","",{form_link(label_cell)})'


def outreach_responses_formula(row: int) -> str:
    label_cell = f"${col_letter(OUTREACH_LABEL_COL)}{row}"
    return (
        f'=IF({label_cell}="","",'
        f"COUNTIF({data_range('source')},{label_cell}))"
    )


# --- «Эффективность» ------------------------------------------------------
#
# Вся сводка — одно выражение в A2. Собирается из именованных кусков через
# LET: имена короткие, потому что их здесь двадцать, и каждое объяснено
# рядом. Читать формулу в таблице всё равно никто не будет — на то и тест.


def let(bindings: list[tuple[str, str]], body: str) -> str:
    """LET(имя,значение,...,выражение) одной строкой."""
    pairs = ",".join(f"{name},{value}" for name, value in bindings)
    return f"LET({pairs},{body})"


def hstack(*parts: str) -> str:
    return "HSTACK(" + ",".join(parts) + ")"


def vstack(*parts: str) -> str:
    return "VSTACK(" + ",".join(parts) + ")"


def _per_label(expression: str) -> str:
    """MAP по меткам размещений: по значению на строку сводки."""
    return f"MAP(lab,LAMBDA(l,{expression}))"


def efficiency_bindings(
    placement_last_row: int, outreach_last_row: int
) -> list[tuple[str, str]]:
    """Имена формулы и их значения. Отдельной функцией — чтобы тест сверял
    каждое имя с колонкой, на которую оно смотрит, а не искал её в строке
    длиной в две тысячи знаков."""
    rejected = lit(ru.REJECTED_PREFIX + "*")

    # Метки размещений считаются из номеров строк — ровно так же, как их
    # подставляет placement_link_formula, только массивом.
    labels = (
        f"MAP(SEQUENCE(ROWS(pd),1,{FIRST_DATA_ROW}),"
        f"LAMBDA(r,{lit(source.LABEL_PREFIX)}&TEXT(r,{lit('0' * source.LABEL_DIGITS)})))"
    )

    # Размещение считается заведённым, если в строке есть дата или канал:
    # пустые строки разметки в сводке не нужны.
    used = "FILTER(HSTACK(pd,pc,pl),(pd<>\"\")+(pc<>\"\"))"

    # Средний балл — по тем откликам, которые до баллов дошли. Отсеявшиеся
    # на стоп-вопросах не оцениваются вовсе, и держать их в знаменателе
    # значит занижать отдачу канала тем сильнее, чем честнее он отобрал.
    average = (
        "LET(k,COUNTIF(src,l)-COUNTIFS(src,l,cnd,rej),"
        'IF(k=0,"",ROUND(SUMIF(src,l,tot)/k,1)))'
    )

    # Отдача первого и последнего размещения в канале: канал, который
    # выдохся, виден по тому, что второе число много меньше первого.
    def channel_edge(ascending: str) -> str:
        return (
            "MAP(chan,LAMBDA(c,LET("
            'd,FILTER(HSTACK(pd,pl),(pc=c)*(pd<>"")),'
            f'IFERROR(COUNTIF(src,INDEX(SORT(d,1,{ascending}),1,2)),""))))'
        )

    return [
        # --- лист «Данные» ---
        ("src", data_range("source")),         # метка отклика
        ("ver", data_range("verdict")),        # A / B / C
        ("tot", data_range("total_score")),    # сумма баллов
        ("cnd", data_range("conditions_ru")),  # условия: по ним виден отказ
        ("idc", data_range("id")),             # заполнена ли строка вообще
        ("rej", rejected),
        # --- лист «Размещения» ---
        ("pd", sheet_range(PLACEMENTS_SHEET, PLACEMENT_DATE_COL, placement_last_row)),
        ("pc", sheet_range(PLACEMENTS_SHEET, PLACEMENT_CHANNEL_COL, placement_last_row)),
        # --- лист «Личные касания» ---
        ("os", sheet_range(OUTREACH_SHEET, OUTREACH_SENT_COL, outreach_last_row)),
        ("pl", labels),
        ("use", used),
        ("lab", "CHOOSECOLS(use,3)"),
        # --- по размещению ---
        ("n", _per_label("COUNTIF(src,l)")),
        ("na", _per_label('COUNTIFS(src,l,ver,"A")')),
        ("nb", _per_label('COUNTIFS(src,l,ver,"B")')),
        ("nc", _per_label('COUNTIFS(src,l,ver,"C")')),
        ("ns", _per_label("COUNTIFS(src,l,cnd,rej)")),
        ("avg", _per_label(average)),
        # --- по каналу ---
        ("chan", 'UNIQUE(FILTER(pc,pc<>""))'),
        ("cp", "MAP(chan,LAMBDA(c,COUNTIF(pc,c)))"),
        ("cr", "MAP(chan,LAMBDA(c,SUM(MAP(FILTER(pl,pc=c),LAMBDA(l,COUNTIF(src,l))))))"),
        ("cf", channel_edge("TRUE")),
        ("cl", channel_edge("FALSE")),
        # --- личные касания ---
        # LET читает имена сверху вниз, и lkp обязан стоять до тех, кто его
        # использует: имя, объявленное ниже, для Sheets не существует.
        ("lkp", lit(source.OUTREACH_PREFIX)),
        ("osent", "SUM(os)"),
        ("lkn", f'SUMPRODUCT((LEFT(src,{len(source.OUTREACH_PREFIX)})=lkp)*1)'),
        # --- чужие метки и отклики без метки ---
        # Метка, которой нет ни у одного заведённого размещения: опечатка в
        # ссылке, старый пост или метка, оставшаяся от удалённой строки.
        # Метка личных касаний чужой не считается: её выдаёт лист «Личные
        # касания», и размещения у неё нет по определению.
        (
            "unk",
            'UNIQUE(FILTER(src,MAP(src,LAMBDA(s,'
            f'IF(OR(s="",LEFT(s,{len(source.OUTREACH_PREFIX)})=lkp),FALSE,'
            "COUNTIF(lab,s)=0)))))",
        ),
        ("none", 'SUMPRODUCT((idc<>"")*(src=""))'),
    ]


def efficiency_body() -> str:
    """Что формула выводит: четыре блока сверху вниз, через пустую строку."""
    table = "SORT(" + hstack(
        "CHOOSECOLS(use,1)", "CHOOSECOLS(use,2)", "n", "na", "nb", "nc", "avg", "ns"
    ) + ",1,FALSE)"

    channels = hstack("chan", "cp", "cr", "cf", "cl")

    unknown = hstack(
        "unk", f"MAP(unk,LAMBDA(u,{lit(UNKNOWN_ROW_NOTE)}))",
        "MAP(unk,LAMBDA(u,COUNTIF(src,u)))",
    )

    body = vstack(
        f"IFERROR({table},{lit(NO_PLACEMENTS_TEXT)})",
        '""',
        lit(CHANNELS_BLOCK_TITLE),
        hstack(*[lit(name) for name in CHANNELS_BLOCK_HEADER]),
        f"IFERROR({channels},{lit(NO_PLACEMENTS_TEXT)})",
        '""',
        lit(OUTREACH_BLOCK_TITLE),
        hstack(lit(OUTREACH_SENT_LABEL), "osent"),
        hstack(lit(OUTREACH_RESPONSES_LABEL), "lkn"),
        '""',
        lit(UNKNOWN_BLOCK_TITLE),
        f"IFERROR({unknown},{lit(UNKNOWN_EMPTY_TEXT)})",
        '""',
        hstack(lit(NO_LABEL_TITLE), '""', "none"),
    )

    # Блоки разной ширины: VSTACK добивает короткие строки ошибкой #N/A, и
    # без IFNA сводка стояла бы в решётках. Сортировка — по первой колонке
    # вниз: свежая дата сверху.
    return f'IFNA({body},"")'


def efficiency_formula(placement_last_row: int, outreach_last_row: int) -> str:
    """Сводка целиком: размещения, каналы, личные касания и чужие метки."""
    return "=" + let(
        efficiency_bindings(placement_last_row, outreach_last_row), efficiency_body()
    )


# ----------------------------- содержимое листов --------------------------

def placements_values(last_row: int) -> list[list[str]]:
    """Формулы колонок C и D для строк 2..last_row."""
    return [
        [placement_link_formula(row), placement_responses_formula(row)]
        for row in range(FIRST_DATA_ROW, last_row + 1)
    ]


def outreach_values(last_row: int) -> list[list[str]]:
    """Формулы колонок C, D и E для строк 2..last_row."""
    return [
        [
            outreach_label_formula(row),
            outreach_link_formula(row),
            outreach_responses_formula(row),
        ]
        for row in range(FIRST_DATA_ROW, last_row + 1)
    ]


# ------------------------------ работа с книгой ---------------------------

def open_sheet(
    book: gspread.Spreadsheet, title: str, rows: int, cols: int
) -> gspread.Worksheet:
    """Лист: существующий или новый. Ужимать не ужимает — только растит.

    Ужать лист значило бы стереть то, что Ника завела руками ниже или правее
    размеченного; дорасти до нужного размера, наоборот, обязательно, иначе
    формулам некуда писаться.
    """
    try:
        ws = book.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=title, rows=rows, cols=cols)
        log.info("создан лист «%s»", title)
        return ws

    if ws.row_count < rows or ws.col_count < cols:
        grown_rows, grown_cols = max(ws.row_count, rows), max(ws.col_count, cols)
        ws.resize(rows=grown_rows, cols=grown_cols)
        log.info("лист «%s» дорос до %d строк", title, grown_rows)
    return ws


def write_values(
    ws: gspread.Worksheet, a1: str, values: list[list[str]], formulas: bool = False
) -> None:
    """Запись блока. Формулы — только USER_ENTERED, иначе лягут текстом."""
    ws.update(
        values=values,
        range_name=a1,
        value_input_option="USER_ENTERED" if formulas else "RAW",
    )


def set_notes(ws: gspread.Worksheet, title: str) -> None:
    notes = NOTES.get(title)
    if not notes:
        return
    try:
        ws.update_notes(notes)
    except Exception:
        log.exception("примечания к шапке «%s» не поставлены", title)


HEADER_FORMAT = {
    "textFormat": {"bold": True},
    "wrapStrategy": "WRAP",
    "verticalAlignment": "BOTTOM",
}


def cell_formats(
    columns: int,
    last_row: int,
    date_col: int | None = None,
    score_col: int | None = None,
) -> list[dict]:
    """Форматы ячеек: шапка, колонка даты, колонка среднего балла.

    Дата без формата показывалась бы числом вроде 46 300 — и в «Личных
    касаниях» из такой ячейки не собралась бы метка дня.
    """
    out = [{"range": f"A1:{col_letter(columns)}1", "format": HEADER_FORMAT}]

    def column(col: int, number_format: dict) -> None:
        letter = col_letter(col)
        out.append(
            {
                "range": f"{letter}{FIRST_DATA_ROW}:{letter}{last_row}",
                "format": {"numberFormat": number_format},
            }
        )

    if date_col:
        column(date_col, {"type": "DATE", "pattern": DATE_FORMAT})
    if score_col:
        column(score_col, {"type": "NUMBER", "pattern": SCORE_FORMAT})
    return out


def layout_requests(
    sheet_id: int, widths: list[int], rules: list[dict] | None = None
) -> list[dict]:
    """Запросы на закреплённую шапку, ширину колонок и подсветку строк."""
    return [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        *[
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": i,
                        "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
            for i, width in enumerate(widths)
        ],
        *(rules or []),
    ]


def decorate(
    ws: gspread.Worksheet,
    columns: int,
    widths: list[int],
    *,
    date_col: int | None = None,
    score_col: int | None = None,
    rules: list[dict] | None = None,
) -> None:
    """Оформление листа двумя запросами, а не десятком.

    Sheets отдаёт шестьдесят записей в минуту на пользователя. Четыре листа,
    оформленные по частям — шапка, даты, ширина, подсветка каждый своим
    запросом, — съедали бы половину этого лимита за прогон, а прогон бывает
    и повторный, сразу за первым: скрипт для этого и сделан идемпотентным.

    Оформление — не содержание. Отказ Sheets здесь прогон не роняет:
    содержимое к этому моменту уже записано и читается.
    """
    try:
        ws.batch_format(cell_formats(columns, ws.row_count, date_col, score_col))
        ws.spreadsheet.batch_update(
            {"requests": layout_requests(ws.id, widths, rules)}
        )
    except Exception:
        log.exception("лист «%s» не оформлен — содержимое на месте", ws.title)


# --- Каналы ---------------------------------------------------------------

CHANNEL_WIDTHS = [220, 320, 160]
PLACEMENT_WIDTHS = [110, 220, 280, 90]
OUTREACH_WIDTHS = [110, 120, 110, 280, 90]
EFFICIENCY_WIDTHS = [110, 220, 90, 100, 100, 90, 110, 200]


def sync_channels(book: gspread.Spreadsheet) -> gspread.Worksheet:
    """Справочник площадок. Заполняется руками, один раз на канал."""
    ws = open_sheet(book, CHANNELS_SHEET, 1 + CHANNEL_ROWS, len(CHANNELS_HEADER))
    write_values(ws, f"A1:{col_letter(len(CHANNELS_HEADER))}1", [CHANNELS_HEADER])
    decorate(ws, len(CHANNELS_HEADER), CHANNEL_WIDTHS)
    set_notes(ws, CHANNELS_SHEET)
    log.info("лист «%s»: шапка записана", CHANNELS_SHEET)
    return ws


def channel_source(last_row: int) -> str:
    """Источник выпадающего списка каналов для «Размещений»."""
    letter = col_letter(CHANNEL_NAME_COL)
    return f"='{CHANNELS_SHEET}'!${letter}${FIRST_DATA_ROW}:${letter}${last_row}"


# --- Размещения -----------------------------------------------------------

def sync_placements(book: gspread.Spreadsheet, channel_last_row: int) -> int:
    """Лист размещений. Возвращает номер последней размеченной строки."""
    ws = open_sheet(
        book, PLACEMENTS_SHEET, 1 + PLACEMENT_ROWS, len(PLACEMENTS_HEADER)
    )
    last = ws.row_count

    write_values(ws, f"A1:{col_letter(len(PLACEMENTS_HEADER))}1", [PLACEMENTS_HEADER])
    write_values(
        ws,
        f"{col_letter(PLACEMENT_LINK_COL)}{FIRST_DATA_ROW}"
        f":{col_letter(PLACEMENT_RESPONSES_COL)}{last}",
        placements_values(last),
        formulas=True,
    )
    add_channel_dropdown(ws, last, channel_last_row)
    decorate(
        ws, len(PLACEMENTS_HEADER), PLACEMENT_WIDTHS, date_col=PLACEMENT_DATE_COL
    )
    set_notes(ws, PLACEMENTS_SHEET)
    log.info("лист «%s»: формулы в строках %d..%d", PLACEMENTS_SHEET, FIRST_DATA_ROW, last)
    return last


def add_channel_dropdown(
    ws: gspread.Worksheet, last_row: int, channel_last_row: int
) -> None:
    """Канал выбирается из «Каналов», а не вписывается.

    Проверка строгая намеренно: от написания названия зависит, сойдётся ли
    сводка по каналу. «Авито» и «авито» стали бы двумя разными каналами, и
    отдача каждого — вдвое меньше настоящей.
    """
    letter = col_letter(PLACEMENT_CHANNEL_COL)
    try:
        ws.add_validation(
            f"{letter}{FIRST_DATA_ROW}:{letter}{last_row}",
            ValidationConditionType.one_of_range,
            [channel_source(channel_last_row)],
            showCustomUi=True,
            strict=True,
        )
    except Exception:
        log.exception("список каналов не поставлен — выбирайте канал руками")


# --- Личные касания -------------------------------------------------------

def sync_outreach(book: gspread.Spreadsheet) -> int:
    ws = open_sheet(book, OUTREACH_SHEET, 1 + OUTREACH_ROWS, len(OUTREACH_HEADER))
    last = ws.row_count

    write_values(ws, f"A1:{col_letter(len(OUTREACH_HEADER))}1", [OUTREACH_HEADER])
    write_values(
        ws,
        f"{col_letter(OUTREACH_LABEL_COL)}{FIRST_DATA_ROW}"
        f":{col_letter(OUTREACH_RESPONSES_COL)}{last}",
        outreach_values(last),
        formulas=True,
    )
    decorate(ws, len(OUTREACH_HEADER), OUTREACH_WIDTHS, date_col=OUTREACH_DATE_COL)
    set_notes(ws, OUTREACH_SHEET)
    log.info("лист «%s»: формулы в строках %d..%d", OUTREACH_SHEET, FIRST_DATA_ROW, last)
    return last


# --- Эффективность --------------------------------------------------------

GRAY = {"red": 0.93, "green": 0.93, "blue": 0.93}
GREEN = {"red": 0.85, "green": 0.94, "blue": 0.85}

# Серой красится строка, в которой откликов ноль, зелёной — та, где есть
# хотя бы один сильный кандидат. Правила смотрят на числа, а не на текст:
# ISNUMBER отсекает подписи блоков, где в тех же колонках стоят слова.
ZERO_RULE = '=AND(ISNUMBER($C2),$C2=0)'
HAS_A_RULE = '=AND(ISNUMBER($A2),ISNUMBER($D2),$D2>0)'


def sync_efficiency(
    book: gspread.Spreadsheet, placement_last_row: int, outreach_last_row: int
) -> None:
    ws = open_sheet(book, EFFICIENCY_SHEET, EFFICIENCY_ROWS, EFFICIENCY_COLS)

    write_values(ws, f"A1:{col_letter(EFFICIENCY_COLS)}1", [EFFICIENCY_HEADER])
    write_values(
        ws,
        "A2",
        [[efficiency_formula(placement_last_row, outreach_last_row)]],
        formulas=True,
    )
    decorate(
        ws,
        EFFICIENCY_COLS,
        EFFICIENCY_WIDTHS,
        date_col=EFFICIENCY_DATE_COL,
        score_col=EFFICIENCY_SCORE_COL,
        rules=refresh_rules(ws),
    )
    log.info("лист «%s»: сводка собрана формулой", EFFICIENCY_SHEET)


def color_rules(sheet_id: int, last_row: int) -> list[dict]:
    """Запросы на два правила подсветки.

    Порядок задан явно: в Sheets побеждает первое подошедшее правило, и
    зелёное обязано стоять выше серого. Пересечься они не должны — строка с
    сильным кандидатом не бывает строкой с нулём откликов, — но рассчитывать
    на это, расставляя правила, незачем.
    """
    grid = {
        "sheetId": sheet_id,
        "startRowIndex": FIRST_DATA_ROW - 1,
        "endRowIndex": last_row,
        "startColumnIndex": 0,
        "endColumnIndex": EFFICIENCY_COLS,
    }
    return [
        {
            "addConditionalFormatRule": {
                "index": index,
                "rule": {
                    "ranges": [grid],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": formula}],
                        },
                        "format": {"backgroundColor": color},
                    },
                },
            }
        }
        for index, (formula, color) in enumerate(
            ((HAS_A_RULE, GREEN), (ZERO_RULE, GRAY))
        )
    ]


def existing_rules(book: gspread.Spreadsheet, sheet_id: int) -> int:
    """Сколько правил подсветки уже стоит на листе.

    Нужно, чтобы снять их перед записью своих: иначе каждый запуск добавлял
    бы ещё два, и через месяц лист тащил бы их десятками.
    """
    meta = book.fetch_sheet_metadata(
        params={"fields": "sheets(properties(sheetId),conditionalFormats)"}
    )
    for sheet in meta.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == sheet_id:
            return len(sheet.get("conditionalFormats") or [])
    return 0


def refresh_rules(ws: gspread.Worksheet) -> list[dict]:
    """Снять прежние правила подсветки и поставить свои — одним списком.

    Снимать обязательно: иначе каждый запуск добавлял бы ещё два правила, и
    через месяц лист тащил бы их десятками. Не прочитались прежние — ставим
    свои поверх: подсветка дважды лучше, чем сводка без неё.
    """
    try:
        drop = existing_rules(ws.spreadsheet, ws.id)
    except Exception:
        log.exception("прежние правила подсветки не прочитались")
        drop = 0
    return [
        {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}}
    ] * drop + color_rules(ws.id, ws.row_count)


# ------------------------------- точка входа ------------------------------

def sync(book: gspread.Spreadsheet) -> None:
    """Собирает все четыре листа. Ручные колонки не трогает."""
    channels = sync_channels(book)
    placement_last = sync_placements(book, channels.row_count)
    outreach_last = sync_outreach(book)
    sync_efficiency(book, placement_last, outreach_last)


def dry_run() -> None:
    """Печатает шапки и формулы в терминал. В книгу не ходит."""
    blocks = [
        (CHANNELS_SHEET, CHANNELS_HEADER, []),
        (
            PLACEMENTS_SHEET,
            PLACEMENTS_HEADER,
            placements_values(FIRST_DATA_ROW + 1),
        ),
        (OUTREACH_SHEET, OUTREACH_HEADER, outreach_values(FIRST_DATA_ROW + 1)),
    ]
    for title, header, rows in blocks:
        print(f"\n=== лист «{title}» ===", flush=True)
        print("  " + " | ".join(header), flush=True)
        for i, row in enumerate(rows, start=FIRST_DATA_ROW):
            print(f"{i:>3}  " + " | ".join(row), flush=True)

    print(f"\n=== лист «{EFFICIENCY_SHEET}» ===", flush=True)
    print("  " + " | ".join(EFFICIENCY_HEADER), flush=True)
    print(
        "A2  " + efficiency_formula(1 + PLACEMENT_ROWS, 1 + OUTREACH_ROWS),
        flush=True,
    )
    print(
        f"\nподсветка: серая строка — {ZERO_RULE}, зелёная — {HAS_A_RULE}",
        flush=True,
    )
