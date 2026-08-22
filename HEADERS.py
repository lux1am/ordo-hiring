"""Порядок колонок листа «Данные». Единственный источник истины.

Шапка — строка 1. Данные начинаются со строки 2.
Всего 54 колонки: A..BB.

Три зоны записи, они не пересекаются:
    A..AH   (1..34)   — анкета и телеметрия, пишет POST /submit
    AI..AM  (35..39)  — баллы score_q1..score_q5, пишет обработчик
    AN      (40)      — total_score, ФОРМУЛА В ТАБЛИЦЕ, код её не перезаписывает
    AO..AZ  (41..52)  — результат скоринга и витринные поля, пишет обработчик
    BA..BB  (53..54)  — manual_note, outcome, заполняются вручную
"""

HEADERS = [
    "id",              # 1  A
    "created_at",      # 2  B
    "fio",             # 3  C
    "telegram",        # 4  D
    "resume_url",      # 5  E
    "pdn_consent",     # 6  F

    "stop_1_oklad",    # 7  G
    "stop_2_komissiya",# 8  H
    "stop_3_kasaniya", # 9  I
    "stop_4_vstrechi", # 10 J
    "stop_5_vedenie",  # 11 K
    "stop_6_crm",      # 12 L

    "q1_text",         # 13 M
    "q2_text",         # 14 N
    "q3_text",         # 15 O
    "q4_text",         # 16 P
    "q5_text",         # 17 Q

    "q1_paste_pct",    # 18 R
    "q2_paste_pct",    # 19 S
    "q3_paste_pct",    # 20 T
    "q4_paste_pct",    # 21 U
    "q5_paste_pct",    # 22 V

    "q1_speed_cpm",    # 23 W
    "q2_speed_cpm",    # 24 X
    "q3_speed_cpm",    # 25 Y
    "q4_speed_cpm",    # 26 Z
    "q5_speed_cpm",    # 27 AA

    "q1_backspaces",   # 28 AB
    "q2_backspaces",   # 29 AC
    "q3_backspaces",   # 30 AD
    "q4_backspaces",   # 31 AE
    "q5_backspaces",   # 32 AF

    "tab_switches",    # 33 AG
    "total_time_sec",  # 34 AH

    "score_q1",        # 35 AI
    "score_q2",        # 36 AJ
    "score_q3",        # 37 AK
    "score_q4",        # 38 AL
    "score_q5",        # 39 AM
    "total_score",     # 40 AN  <-- формула, не трогаем

    "ai_style",        # 41 AO
    "ai_markers",      # 42 AP
    "evidence_json",   # 43 AQ
    "screening_questions",  # 44 AR
    "flags",           # 45 AS
    "verdict",         # 46 AT
    "status",          # 47 AU
    "notified",        # 48 AV
    "conditions_ru",   # 49 AW
    "decision_ru",     # 50 AX
    "ai_ru",           # 51 AY
    "flags_ru",        # 52 AZ

    "manual_note",     # 53 BA  <-- вручную
    "outcome",         # 54 BB  <-- вручную
]

# --- Границы зон (1-based номера колонок) ---------------------------------

COL_ID = 1
COL_FORM_START, COL_FORM_END = 1, 34        # A..AH
COL_SCORES_START, COL_SCORES_END = 35, 39   # AI..AM
COL_TOTAL_SCORE = 40                        # AN — формула
COL_RESULT_START, COL_RESULT_END = 41, 52   # AO..AZ
COL_MANUAL_START, COL_MANUAL_END = 53, 54   # BA..BB

FIRST_DATA_ROW = 2
PREPARED_ROWS = 300                          # строки 2..301 размечены формулой
LAST_PREPARED_ROW = FIRST_DATA_ROW + PREPARED_ROWS - 1   # 301
FORMULA_TOPUP_FROM_ROW = 251                 # с этой строки дописываем формулу сами

TOTAL_SCORE_FORMULA = '=IF(A{row}="","",SUM(AI{row}:AM{row}))'


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA, 54 -> BB."""
    if n < 1:
        raise ValueError(f"номер колонки должен быть >= 1, получено {n}")
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def index_of(name: str) -> int:
    """1-based номер колонки по имени заголовка."""
    return HEADERS.index(name) + 1


def a1_range(row: int, start_col: int, end_col: int) -> str:
    """Диапазон одной строки: a1_range(5, 35, 39) -> 'AI5:AM5'."""
    return f"{col_letter(start_col)}{row}:{col_letter(end_col)}{row}"


assert len(HEADERS) == 54, f"ожидалось 54 колонки, в списке {len(HEADERS)}"
assert col_letter(len(HEADERS)) == "BB"
assert col_letter(COL_SCORES_START) == "AI"
assert col_letter(COL_SCORES_END) == "AM"
assert col_letter(COL_TOTAL_SCORE) == "AN"
assert col_letter(COL_RESULT_START) == "AO"
assert col_letter(COL_RESULT_END) == "AZ"
assert index_of("status") == 47 and col_letter(47) == "AU"
