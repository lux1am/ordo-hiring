"""Сверка цитат: балл выше нуля живёт только при дословной цитате."""
import pytest

from app.llm import normalize
from app.models import MODEL_QKEYS, ScorerOutput
from app.scorer import enforce_evidence

ANSWER = "В мае я закрыл 4 сделки — выручка составила 480 000 ₽, средний чек 120 000 ₽."


def make(evidence: dict, scores=None) -> ScorerOutput:
    return ScorerOutput(
        scores=scores or {k: 3 for k in MODEL_QKEYS},
        evidence={k: evidence.get(k, "") for k in MODEL_QKEYS},
        auto_filters={"q3_senior_dm_confirmed": True, "q1_definition_ok": True},
        ai_style=10,
        ai_markers=[],
        screening_questions=["а", "б", "в"],
    )


def answers(q2=ANSWER):
    # Проверяется судьба q2: балл за q1 модель больше не ставит, и цитат по
    # нему не даёт — его считает checks.q1_score по числам из формы.
    return {"q1_text": "", "q2_text": q2, "q3_text": "", "q4_text": "", "q5_text": ""}


@pytest.mark.parametrize(
    "quote,kept,case",
    [
        ("В мае я закрыл 4 сделки", True, "точная цитата"),
        ("в мае я закрыл 4 сделки", True, "другой регистр"),
        ("В мае  я   закрыл 4 сделки", True, "лишние пробелы"),
        ("В мае я закрыл 4 сделки\nвыручка", True, "перенос строки на месте тире"),
        ("«В мае я закрыл 4 сделки»", True, "в кавычках"),
        ("В мае я закрыл 4 сделки - выручка составила 480 000 ₽", True, "дефис вместо тире"),
        ("", False, "цитаты нет"),
        ("В июне я закрыл 9 сделок", False, "цитата выдумана"),
    ],
)
def test_normalization_keeps_honest_quotes(quote, kept, case):
    # Остальные баллы намеренно оставлены без цитат — они обнуляются, здесь
    # проверяется только судьба q2.
    result, _ = enforce_evidence(make({"q2": quote}), answers())
    assert (result.scores["q2"] == 3) is kept, f"{case}: {quote!r}"


def test_long_quote_is_retelling():
    """Цитата длиннее 15 слов — пересказ, а не доказательство."""
    long_answer = " ".join(f"слово{i}" for i in range(30))
    quote_16 = " ".join(f"слово{i}" for i in range(16))
    quote_15 = " ".join(f"слово{i}" for i in range(15))

    r, _ = enforce_evidence(make({"q2": quote_16}), answers(long_answer))
    assert r.scores["q2"] == 0, "16 слов — обнуляем"

    r, _ = enforce_evidence(make({"q2": quote_15}), answers(long_answer))
    assert r.scores["q2"] == 3, "ровно 15 слов — оставляем"


def test_zero_scores_are_not_touched():
    """Нулевой балл не требует цитаты и не попадает в список обнулённых."""
    r, zeroed = enforce_evidence(make({}, scores={k: 0 for k in ["q1","q2","q3","q4","q5"]}), answers())
    assert all(v == 0 for v in r.scores.values())
    assert zeroed == []


def test_normalize():
    assert normalize("  Привет,\n  «мир» — да  ") == "привет, мир да"
    assert normalize("A—B") == "a b"


def test_schema_rejects_bad_output():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):   # балл вне 0..3
        make({}, scores={"q1": 4, "q2": 0, "q3": 0, "q4": 0, "q5": 0})

    with pytest.raises(ValidationError):   # не хватает вопроса
        ScorerOutput(
            scores={"q1": 1}, evidence={"q1": "x"},
            auto_filters={}, ai_style=10, screening_questions=["а", "б", "в"],
        )

    with pytest.raises(ValidationError):   # не три вопроса на интервью
        make({}) .model_copy(update={"screening_questions": ["а"]}) and ScorerOutput(
            scores={k: 0 for k in ["q1","q2","q3","q4","q5"]},
            evidence={k: "" for k in ["q1","q2","q3","q4","q5"]},
            auto_filters={}, ai_style=10, screening_questions=["а", "б"],
        )
