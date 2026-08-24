"""Pydantic-схемы: телеметрия, форма, ответ скорера."""
import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import load_questions
from app.numbers import Q1Numbers, Q4Numbers, format_q1, format_q4

YES = "Да"
NO = "Нет"
STOP_FIELDS = [q["field"] for q in load_questions()["stop_questions"]]
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

    Числа Q1 и Q4 приходят готовыми объектами: их проверяет numbers.py ещё
    в обработчике, потому что ошибку про конкретное числовое поле нужно
    показать под этим полем, а pydantic назвал бы виноватой всю анкету.
    Здесь они склеиваются с текстовым ответом в одну ячейку — колонки
    таблицы прежние.
    """
    fio: str
    telegram: str
    resume_url: str
    pdn_consent: bool
    stops: dict[str, Literal["Да", "Нет"]]
    answers: dict[str, str]
    telemetry: Telemetry
    q1_numbers: Q1Numbers | None = None
    q4_numbers: Q4Numbers | None = None

    model_config = {"arbitrary_types_allowed": True}

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
        missing = [f for f in STOP_FIELDS if f not in self.stops]
        if missing:
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

        # Длина проверена по тексту, который писал кандидат, и только теперь
        # к нему спереди приписываются числа: иначе минимум в 40 знаков
        # набирался бы одними цифрами из соседних полей.
        #
        # Числа необязательны намеренно: без них анкета собирается из одного
        # текста, и подставить вместо пропущенных полей нули — молча
        # записать кандидату результат, которого он не заявлял.
        if self.q1_numbers is not None:
            self.answers["q1_text"] = format_q1(self.q1_numbers, self.answers["q1_text"])
        if self.q4_numbers is not None:
            self.answers["q4_text"] = format_q4(self.q4_numbers, self.answers["q4_text"])
        return self

    def has_stop_no(self) -> bool:
        return any(v == NO for v in self.stops.values())

    def rejected_conditions(self) -> list[str]:
        """Поля стоп-вопросов, на которые ответили «Нет»."""
        return [f for f in STOP_FIELDS if self.stops.get(f) == NO]


# =========================== ОТВЕТ СКОРЕРА ================================

Score = Annotated[int, Field(ge=0, le=3)]

# Порядок баллов в таблице — колонки AI..AM, все пять вопросов.
QKEYS = ["q1", "q2", "q3", "q4", "q5"]
# Что из этого оценивает модель. Балл за Q1 считает checks.q1_score по
# числам из формы: там нечего трактовать, а детерминированный балл ещё и
# воспроизводится при разборе спорной анкеты.
MODEL_QKEYS = ["q2", "q3", "q4", "q5"]


class AutoFilters(BaseModel):
    """Факты, которые модель обязана констатировать явно — на них висят автофильтры."""
    q3_senior_dm_confirmed: bool = False
    # Понятно ли из текста Q1, что кандидат считал закрытой сделкой. Без
    # этого балл за Q1 упирается в двойку: цифры без определения нечитаемы.
    q1_definition_ok: bool = False


class ScorerOutput(BaseModel):
    scores: dict[str, Score]
    evidence: dict[str, str]
    auto_filters: AutoFilters
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
