"""Скоринг ответов кандидата через Anthropic API."""
import json
import logging

from pydantic import ValidationError

from app.config import (
    EVIDENCE_MAX_WORDS,
    SCORER_MAX_TOKENS,
    SCORER_MODEL,
    load_questions,
    load_system_prompt,
)
from app.llm import client, normalize, parse_json, text_of
from app.models import MODEL_QKEYS, ScorerOutput
from app.retry import RetriesExhausted, call_with_backoff

log = logging.getLogger(__name__)


class ScoringFailed(Exception):
    """Модель не вернула валидный ответ или недоступна — анкета уходит в manual."""


def build_prompt(answers: dict[str, str]) -> str:
    """Собирает вопросы и ответы для модели.

    Числовых полей в анкете нет: все пять ответов — свободный текст, и
    каждый уходит модели как есть, без предварительной обработки.
    """
    parts = []
    for i, q in enumerate(load_questions()["open_questions"], start=1):
        raw = answers.get(q["field"], "").strip()
        parts.append(
            f"=== Вопрос {i} ===\n{q['text']}\n\n"
            f"--- Ответ кандидата ---\n{raw}"
        )
    return "\n\n".join(parts)


def enforce_evidence(
    result: ScorerOutput, answers: dict[str, str]
) -> tuple[ScorerOutput, list[str]]:
    """Обнуляет баллы, не подтверждённые дословной цитатой.

    Промпт требует цитату, но полагаться только на промпт нельзя — правило
    проверяется кодом. Сверка идёт по нормализованным строкам, иначе
    честные ответы резались бы из-за другого типа кавычек или лишнего
    переноса строки.
    """
    zeroed: list[str] = []

    for key in MODEL_QKEYS:
        if result.scores[key] == 0:
            continue

        quote = (result.evidence.get(key) or "").strip()
        answer = answers.get(f"{key}_text", "")
        reason = None

        if not quote:
            reason = "цитата не приведена"
        elif len(quote.split()) > EVIDENCE_MAX_WORDS:
            # Длинная «цитата» — это пересказ всего ответа, а не доказательство.
            reason = f"цитата длиннее {EVIDENCE_MAX_WORDS} слов"
        elif normalize(quote) not in normalize(answer):
            reason = "цитаты нет в ответе кандидата"

        if reason:
            log.info("%s: балл %d обнулён — %s", key, result.scores[key], reason)
            zeroed.append(f"{key} обнулён ({reason})")
            result.scores[key] = 0

    return result, zeroed


def score(answers: dict[str, str]) -> tuple[ScorerOutput, list[str]]:
    """Оценивает ответы. Один повтор при невалидном JSON, дальше — ScoringFailed."""
    system = load_system_prompt()
    messages: list[dict] = [{"role": "user", "content": build_prompt(answers)}]
    last_error = ""

    # Две попытки: первая обычная, вторая — с указанием, что было не так.
    for attempt in (1, 2):
        def call():
            return client().messages.create(
                model=SCORER_MODEL,
                max_tokens=SCORER_MAX_TOKENS,
                temperature=0,
                system=system,
                messages=messages,
            )

        try:
            raw = text_of(call_with_backoff(call, label=f"скорер (попытка {attempt})"))
        except RetriesExhausted as e:
            raise ScoringFailed(f"API недоступен: {e}") from e

        try:
            result = ScorerOutput.model_validate(parse_json(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = str(e)
            log.warning("скорер, попытка %d: невалидный ответ — %s", attempt, last_error)
            if attempt == 2:
                break
            messages = messages + [
                {"role": "assistant", "content": raw or "(пустой ответ)"},
                {
                    "role": "user",
                    "content": (
                        "Твой ответ не прошёл проверку формата:\n"
                        f"{last_error}\n\n"
                        "Верни только JSON-объект требуемой структуры, без "
                        "markdown-обёртки и без пояснений."
                    ),
                },
            ]
            continue

        result, zeroed = enforce_evidence(result, answers)
        log.info(
            "скоринг готов: баллы %s, стиль ИИ %d%s",
            [result.scores[k] for k in MODEL_QKEYS],
            result.ai_style,
            f", обнулено: {len(zeroed)}" if zeroed else "",
        )
        return result, zeroed

    raise ScoringFailed(f"невалидный JSON после повтора: {last_error}")
