"""Работа с листом «Данные». Три зоны записи, AN не трогаем никогда."""
import json
import logging
import threading
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

from HEADERS import (
    COL_FORM_END,
    COL_FORM_START,
    COL_RESULT_END,
    COL_RESULT_START,
    COL_SCORES_END,
    COL_SCORES_START,
    COL_TOTAL_SCORE,
    FIRST_DATA_ROW,
    FORMULA_TOPUP_FROM_ROW,
    HEADERS,
    LAST_PREPARED_ROW,
    TOTAL_SCORE_FORMULA,
    a1_range,
    col_letter,
    index_of,
)
from app.config import (
    GOOGLE_CREDENTIALS_JSON,
    SHEET_ID,
    STATUS_NEW,
    STATUS_PROCESSING,
    WORKSHEET_NAME,
)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Аллокация строки — «прочитать длину колонки A и записать в следующую» —
# не атомарна. Замок делает её атомарной внутри процесса; при нескольких
# инстансах Render этого мало (см. README, раздел про масштабирование).
_alloc_lock = threading.Lock()

COL_ID = index_of("id")
COL_TELEGRAM = index_of("telegram")
COL_CREATED_AT = index_of("created_at")
COL_STATUS = index_of("status")
COL_FLAGS = index_of("flags")
STATUS_A1 = col_letter(COL_STATUS)
FLAGS_A1 = col_letter(COL_FLAGS)


@lru_cache(maxsize=1)
def worksheet() -> gspread.Worksheet:
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("не задана переменная окружения GOOGLE_CREDENTIALS_JSON")
    if not SHEET_ID:
        raise RuntimeError("не задана переменная окружения SHEET_ID")

    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON не разбирается как JSON — проверьте, что "
            "переменная содержит весь файл ключа целиком"
        ) from e

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    ws = gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)
    log.info("подключён лист «%s» таблицы %s", WORKSHEET_NAME, SHEET_ID)
    return ws


# --- Аллокация строки -----------------------------------------------------

def allocate_row() -> int:
    """Номер следующей свободной строки.

    Считаем по колонке A, а не через append_row: в AN стоит формула на
    300 строк вперёд, и для Sheets API эти ячейки не пустые — append ушёл бы
    сразу за размеченный диапазон.
    """
    with _alloc_lock:
        ws = worksheet()
        row = max(len(ws.col_values(COL_ID)) + 1, FIRST_DATA_ROW)
        if row > FORMULA_TOPUP_FROM_ROW:
            _ensure_total_formula(ws, row)
        return row


def _ensure_total_formula(ws: gspread.Worksheet, row: int) -> None:
    """Дописывает формулу total_score в AN этой строки.

    Начиная с 251-й строки подстраховываемся сами: для уже размеченных
    строк это перезапись той же формулы, а после 301-й — единственный
    способ не оставить total_score пустым.
    """
    if row > LAST_PREPARED_ROW:
        log.warning(
            "строка %d вне размеченных %d — формула total_score дописывается кодом",
            row, LAST_PREPARED_ROW,
        )
    try:
        ws.update(
            values=[[TOTAL_SCORE_FORMULA.format(row=row)]],
            range_name=f"{col_letter(COL_TOTAL_SCORE)}{row}",
            value_input_option="USER_ENTERED",   # единственное место с формулой
        )
    except Exception:
        log.exception("не удалось дописать формулу total_score в строку %d", row)


# --- Запись ---------------------------------------------------------------

def write_form(row: int, values: list, flags: str, status: str = STATUS_NEW) -> None:
    """Зона A..AH, флаги телеметрии в AS и статус в AU. Всё RAW, AN не задевается.

    Флаги телеметрии пишутся уже здесь, а не при обработке: is_mobile и время
    ухода со вкладки в таблицу не попадают (отдельных колонок под них нет), и
    после перезапуска процесса восстановить их было бы неоткуда.
    """
    assert len(values) == COL_FORM_END - COL_FORM_START + 1, (
        f"ожидалось {COL_FORM_END - COL_FORM_START + 1} значений, получено {len(values)}"
    )
    worksheet().batch_update(
        [
            {"range": a1_range(row, COL_FORM_START, COL_FORM_END), "values": [values]},
            {"range": f"{FLAGS_A1}{row}", "values": [[flags]]},
            {"range": f"{STATUS_A1}{row}", "values": [[status]]},
        ],
        value_input_option="RAW",
    )


def read_form(row: int) -> dict:
    """Анкета из листа как словарь по именам колонок A..AH.

    Обработка всегда идёт от номера строки и читает данные отсюда — тогда
    восстановление зависшей строки использует ровно тот же путь, что и
    обычный разбор свежей анкеты.
    """
    values = worksheet().get(a1_range(row, COL_FORM_START, COL_FORM_END))
    cells = (values[0] if values else []) + [""] * (COL_FORM_END - COL_FORM_START + 1)
    return {name: cells[i] for i, name in enumerate(HEADERS[:COL_FORM_END])}


def read_flags(row: int) -> list[str]:
    """Флаги, записанные при сабмите."""
    values = worksheet().get(f"{FLAGS_A1}{row}")
    if not values or not values[0]:
        return []
    return [f.strip() for f in str(values[0][0]).split(",") if f.strip()]


def write_result(row: int, scores: list | None, result: list) -> None:
    """Зоны AI..AM (баллы) и AO..AZ (результат). Между ними AN — пропускаем."""
    assert len(result) == COL_RESULT_END - COL_RESULT_START + 1, (
        f"ожидалось {COL_RESULT_END - COL_RESULT_START + 1} значений, получено {len(result)}"
    )
    data = [{"range": a1_range(row, COL_RESULT_START, COL_RESULT_END), "values": [result]}]
    if scores is not None:
        assert len(scores) == COL_SCORES_END - COL_SCORES_START + 1
        data.insert(
            0,
            {"range": a1_range(row, COL_SCORES_START, COL_SCORES_END), "values": [scores]},
        )
    worksheet().batch_update(data, value_input_option="RAW")


def set_status(row: int, status: str) -> None:
    worksheet().update(
        values=[[status]], range_name=f"{STATUS_A1}{row}", value_input_option="RAW"
    )


# --- Чтение ---------------------------------------------------------------

def read_status(row: int) -> str:
    values = worksheet().get(f"{STATUS_A1}{row}")
    if not values or not values[0]:
        return ""
    return str(values[0][0]).strip()


def telegram_exists(telegram: str) -> bool:
    """Есть ли уже анкета с таким телеграмом. Сравнение без учёта регистра."""
    needle = telegram.strip().lstrip("@").lower()
    if not needle:
        return False
    column = worksheet().col_values(COL_TELEGRAM)[1:]   # без шапки
    return any(str(v).strip().lstrip("@").lower() == needle for v in column)


def rows_with_status(status: str) -> list[int]:
    """Номера строк с указанным статусом."""
    column = worksheet().col_values(COL_STATUS)
    return [
        i
        for i, v in enumerate(column, start=1)
        if i >= FIRST_DATA_ROW and str(v).strip() == status
    ]


def rows_in_processing() -> list[int]:
    return rows_with_status(STATUS_PROCESSING)


def ensure_header() -> None:
    """Сверяет шапку листа с HEADERS.py и громко жалуется на расхождение.

    Порядок колонок задаётся HEADERS.py, но физически живёт в таблице.
    Молчаливое расхождение означало бы, что баллы пишутся не в те ячейки.
    """
    actual = worksheet().row_values(1)
    if actual[: len(HEADERS)] == HEADERS:
        log.info("шапка листа совпадает с HEADERS.py (%d колонок)", len(HEADERS))
        return
    for i, (want, got) in enumerate(zip(HEADERS, actual + [""] * len(HEADERS)), start=1):
        if want != got:
            log.error(
                "ШАПКА РАСХОДИТСЯ: колонка %s (%d) — ожидалось «%s», в таблице «%s»",
                col_letter(i), i, want, got,
            )
            break
    if len(actual) != len(HEADERS):
        log.error("в таблице %d колонок, в HEADERS.py — %d", len(actual), len(HEADERS))
