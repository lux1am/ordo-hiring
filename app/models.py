"""Pydantic-схемы: телеметрия, форма, ответ скорера."""
import re
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import load_questions

STOP_FIELDS = [q["field"] for q in load_questions()["stop_questions"]]
FLAG_FIELDS = [q["field"] for q in load_questions()["flag_questions"]]
OPEN_FIELDS = [q["field"] for q in load_questions()["open_questions"]]


# ============================ ТЕЛЕМЕТРИЯ ==================================

class FieldTelemetry(BaseModel):
    """Метрики одной textarea. Считаются в браузере, здесь только проверяются."""
    length: int = 0
    pasted_chars: int = 0
    speed_cpm: float = 0.0
    backspaces: int = 0
    away_before_input_sec: float = 0.0   # самый долгий уход со вкладки,
                                         # после которого кандидат дописывал это поле

    @property
    def paste_pct(self) -> float:
        if self.length <= 0:
            return 0.0
        return min(100.0, round(self.pasted_chars / self.length * 100, 1))


class Telemetry(BaseModel):
    is_mobile: bool = False
    tab_switches: int = 0
    total_time_sec: float = 0.0
    fields: dict[str, FieldTelemetry] = Field(default_factory=dict)

    def field(self, name: str) -> FieldTelemetry:
        """Отсутствующее поле — не ошибка: JS мог не успеть его инициализировать."""
        return self.fields.get(name) or FieldTelemetry()

    @model_validator(mode="after")
    def _clamp(self):
        # Телеметрия приходит из браузера, то есть от кандидата. Значения,
        # которые ломают арифметику, обнуляем, но анкету не отклоняем.
        self.total_time_sec = max(0.0, min(self.total_time_sec, 86400.0))
        self.tab_switches = max(0, min(self.tab_switches, 10000))
        for t in self.fields.values():
            t.length = max(0, t.length)
            t.pasted_chars = max(0, min(t.pasted_chars, t.length))
            t.speed_cpm = max(0.0, min(t.speed_cpm, 100000.0))
            t.backspaces = max(0, t.backspaces)
            t.away_before_input_sec = max(0.0, min(t.away_before_input_sec, 86400.0))
        return self


# ============================== ФОРМА =====================================

TELEGRAM_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
URL_RE = re.compile(r"^https?://\S+\.\S+", re.IGNORECASE)


class Submission(BaseModel):
    """Провалидированная анкета. Собирается из тела POST /submit.

    Ответы на стоп-вопросы и вопросы-флаги приходят сюда уже разобранными:
    их проверяет profile.py ещё в обработчике, потому что ошибку про
    конкретный вариант нужно показать под этим вопросом, а pydantic назвал
    бы виноватой всю анкету. Здесь они проверяются повторно, но только на
    полноту — это вторая линия, не замена.

    В `stops` лежит текст ячейки целиком: у стоп-вопроса про часовой пояс
    к варианту приписано уточнение, см. profile.format_stop. Сравнивать
    ответ с проходным набором нужно через profile.stop_option.
    """
    fio: str
    telegram: str
    resume_url: str
    pdn_consent: bool
    stops: dict[str, str]
    profile: dict = Field(default_factory=dict)
    answers: dict[str, str]
    telemetry: Telemetry

    @field_validator("fio")
    @classmethod
    def _fio(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 5 or len(v.split()) < 2:
            raise ValueError("Укажите фамилию и имя полностью")
        if len(v) > 200:
            raise ValueError("Слишком длинное ФИО")
        return v

    @field_validator("telegram")
    @classmethod
    def _telegram(cls, v: str) -> str:
        v = v.strip()
        # Принимаем ссылку, @ник и голый ник — храним всегда как @ник.
        v = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", v, flags=re.IGNORECASE)
        if not TELEGRAM_RE.match(v):
            raise ValueError("Telegram укажите в виде @username")
        return "@" + v.lstrip("@")

    @field_validator("resume_url")
    @classmethod
    def _resume(cls, v: str) -> str:
        v = v.strip()
        if not URL_RE.match(v):
            raise ValueError("Ссылка на резюме должна начинаться с http:// или https://")
        if len(v) > 2000:
            raise ValueError("Слишком длинная ссылка")
        return v

    @field_validator("pdn_consent")
    @classmethod
    def _consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Без согласия на обработку персональных данных анкету не принять")
        return v

    @model_validator(mode="after")
    def _completeness(self):
        if [f for f in STOP_FIELDS if not str(self.stops.get(f, "")).strip()]:
            raise ValueError("Ответьте на все вопросы об условиях работы")

        limits = {q["field"]: q for q in load_questions()["open_questions"]}
        for f in OPEN_FIELDS:
            text = " ".join((self.answers.get(f) or "").split())
            if not text:
                raise ValueError(f"Не заполнен {limits[f]['label'].lower()}")
            lo = limits[f].get("min_length", 0)
            if len(text) < lo:
                raise ValueError(
                    f"{limits[f]['label']}: ответ слишком короткий "
                    f"({len(text)} из {lo} знаков минимум)"
                )
            hi = limits[f].get("max_length")
            if hi and len(text) > hi:
                raise ValueError(f"{limits[f]['label']}: не больше {hi} знаков")
            self.answers[f] = text
        return self


# =========================== ОТВЕТ СКОРЕРА ================================

Score = Annotated[int, Field(ge=0, le=3)]

# Порядок баллов в таблице — колонки AI..AM, все пять вопросов. Числовых
# полей в анкете больше нет, поэтому все пять баллов ставит модель.
QKEYS = ["q1", "q2", "q3", "q4", "q5"]
MODEL_QKEYS = QKEYS


class ScorerOutput(BaseModel):
    scores: dict[str, Score]
    evidence: dict[str, str]
    ai_style: Annotated[int, Field(ge=0, le=100)]
    ai_markers: list[str] = Field(default_factory=list)
    screening_questions: list[str]

    @field_validator("scores", "evidence")
    @classmethod
    def _all_scored(cls, v: dict):
        missing = [k for k in MODEL_QKEYS if k not in v]
        if missing:
            raise ValueError(f"нет ключей: {', '.join(missing)}")
        return {k: v[k] for k in MODEL_QKEYS}

    @field_validator("screening_questions")
    @classmethod
    def _three(cls, v: list[str]) -> list[str]:
        v = [q.strip() for q in v if q and q.strip()]
        if len(v) != 3:
            raise ValueError(f"нужно ровно 3 вопроса, получено {len(v)}")
        return v
