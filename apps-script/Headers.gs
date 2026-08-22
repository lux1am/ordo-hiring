// СГЕНЕРИРОВАНО apps-script/build_gs.py — не редактировать вручную.
// Источник: HEADERS.py

const HEADERS = [
  "id",
  "created_at",
  "fio",
  "telegram",
  "resume_url",
  "pdn_consent",
  "stop_1_oklad",
  "stop_2_komissiya",
  "stop_3_kasaniya",
  "stop_4_vstrechi",
  "stop_5_vedenie",
  "stop_6_crm",
  "q1_text",
  "q2_text",
  "q3_text",
  "q4_text",
  "q5_text",
  "q1_paste_pct",
  "q2_paste_pct",
  "q3_paste_pct",
  "q4_paste_pct",
  "q5_paste_pct",
  "q1_speed_cpm",
  "q2_speed_cpm",
  "q3_speed_cpm",
  "q4_speed_cpm",
  "q5_speed_cpm",
  "q1_backspaces",
  "q2_backspaces",
  "q3_backspaces",
  "q4_backspaces",
  "q5_backspaces",
  "tab_switches",
  "total_time_sec",
  "score_q1",
  "score_q2",
  "score_q3",
  "score_q4",
  "score_q5",
  "total_score",
  "ai_style",
  "ai_markers",
  "evidence_json",
  "screening_questions",
  "flags",
  "verdict",
  "status",
  "notified",
  "conditions_ru",
  "decision_ru",
  "ai_ru",
  "flags_ru",
  "manual_note",
  "outcome"
];

const COL_FORM_END = 34;        // AH — последняя колонка анкеты
const COL_FLAGS = 45;             // AS
const COL_STATUS = 47;            // AU
const COL_TELEGRAM = 4;           // D
const COL_TOTAL_SCORE = 40;        // AN — формула, не трогаем
const FIRST_DATA_ROW = 2;
const LAST_PREPARED_ROW = 301;
const FORMULA_TOPUP_FROM_ROW = 251;
const TOTAL_SCORE_FORMULA = "=IF(A{row}=\"\",\"\",SUM(AI{row}:AM{row}))";
