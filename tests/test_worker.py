"""Расписание обработки: workflow и сводка прогона."""
from collections import Counter
from pathlib import Path

import re

import pytest
import yaml

import process_new
from app.config import STATUS_DONE, STATUS_MANUAL, STATUS_NEW
from app.pipeline import OUTCOME_FAILED, OUTCOME_SKIPPED

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "worker.yml"


@pytest.fixture(scope="module")
def wf():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML разбирает ключ `on` как булево True — обрабатываем оба варианта.
    data["_on"] = data.get("on") or data.get(True)
    return data


# ------------------------------ workflow ----------------------------------

def test_schedule_and_manual_trigger(wf):
    assert wf["_on"]["schedule"] == [{"cron": "*/10 * * * *"}]
    assert "workflow_dispatch" in wf["_on"]


def test_concurrency_never_cancels_running_job(wf):
    """Отмена идущего прогона оставила бы строки в processing на середине."""
    assert wf["concurrency"]["group"] == "worker"
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_job_settings(wf):
    job = wf["jobs"]["process"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 10

    setup = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert setup["with"]["python-version"] == "3.12"

    assert any("pip install -r requirements.txt" in str(s.get("run", "")) for s in job["steps"])
    assert any("process_new.py" in str(s.get("run", "")) for s in job["steps"])


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "GOOGLE_CREDENTIALS_JSON", "SHEET_ID"]
)
def test_secrets_are_passed_from_github_secrets(wf, name):
    step = next(s for s in wf["jobs"]["process"]["steps"] if "process_new.py" in str(s.get("run", "")))
    assert step["env"][name] == "${{ secrets." + name + " }}"


def test_secrets_are_not_hardcoded():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "sk-ant-" not in text
    assert "private_key" not in text


def test_python_version_matches_project():
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip().startswith("3.12")


# ------------------------------- сводка -----------------------------------

def has_line(text: str, label: str, value: int) -> bool:
    """Строка сводки вида «метка : число», без привязки к выравниванию."""
    return re.search(rf"{re.escape(label)}\s*:\s*{value}\b", text) is not None

class FakeRun:
    """Подменяет чтение листа, восстановление и обработку строк."""

    def __init__(self, rows, outcomes, revived=0):
        self.rows = rows
        self.outcomes = outcomes
        self.revived = revived
        self.processed = []

    def install(self, monkeypatch):
        monkeypatch.setattr(process_new.sheets, "rows_with_status", lambda st: list(self.rows))
        monkeypatch.setattr(
            process_new.recovery, "recover", lambda **kw: list(range(self.revived))
        )
        monkeypatch.setattr(process_new, "missing_secrets", lambda: [])

        def fake_process(row):
            self.processed.append(row)
            return self.outcomes[self.rows.index(row)]

        monkeypatch.setattr(process_new, "process_row", fake_process)


def test_summary_counts_every_outcome(monkeypatch, capsys):
    run = FakeRun([2, 3, 4], [STATUS_DONE, STATUS_MANUAL, STATUS_DONE], revived=2)
    run.install(monkeypatch)

    assert process_new.main([]) == 0
    out = capsys.readouterr().out

    assert "СВОДКА ПРОГОНА" in out
    assert has_line(out, "зависших строк возвращено", 2)
    assert has_line(out, "строк обработано", 3)
    assert has_line(out, "оценено", 2)
    assert has_line(out, "в ручную проверку", 1)
    assert run.processed == [2, 3, 4]


def test_summary_printed_even_when_nothing_to_do(monkeypatch, capsys):
    """Пустой прогон тоже должен отчитаться — иначе непонятно, работает ли он."""
    FakeRun([], []).install(monkeypatch)

    assert process_new.main([]) == 0
    out = capsys.readouterr().out
    assert "СВОДКА ПРОГОНА" in out
    assert has_line(out, "строк обработано", 0)


def test_manual_is_not_an_error(monkeypatch):
    """Ручная проверка — штатный исход, прогон должен быть зелёным."""
    FakeRun([2, 3], [STATUS_MANUAL, STATUS_MANUAL]).install(monkeypatch)
    assert process_new.main([]) == 0


def test_failed_row_makes_run_red(monkeypatch):
    FakeRun([2, 3], [STATUS_DONE, OUTCOME_FAILED]).install(monkeypatch)
    assert process_new.main([]) == 1


def test_limit(monkeypatch):
    run = FakeRun([2, 3, 4, 5], [STATUS_DONE] * 4)
    run.install(monkeypatch)
    process_new.main(["--limit", "2"])
    assert run.processed == [2, 3]


def test_dry_run_touches_nothing(monkeypatch):
    run = FakeRun([2, 3], [STATUS_DONE, STATUS_DONE])
    run.install(monkeypatch)
    assert process_new.main(["--dry-run"]) == 0
    assert run.processed == []


def test_missing_env_exits_with_2(monkeypatch):
    monkeypatch.setattr(process_new, "missing_secrets", lambda: ["SHEET_ID"])
    assert process_new.main([]) == 2


def test_skipped_rows_are_counted(monkeypatch, capsys):
    """Строка, у которой статус успел измениться, попадает в сводку отдельно."""
    FakeRun([2, 3], [STATUS_DONE, OUTCOME_SKIPPED]).install(monkeypatch)
    process_new.main([])
    assert has_line(capsys.readouterr().out, "пропущено (статус изменился)", 1)


def test_step_summary_written_for_actions(monkeypatch, tmp_path, capsys):
    """На странице прогона сводка видна без разворачивания логов."""
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    FakeRun([2], [STATUS_DONE], revived=1).install(monkeypatch)

    process_new.main([])

    text = path.read_text(encoding="utf-8")
    assert "### Обработка анкет" in text
    assert "| обработано | 1 |" in text
    assert "| зависших возвращено | 1 |" in text


def test_step_summary_skipped_outside_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    FakeRun([2], [STATUS_DONE]).install(monkeypatch)
    assert process_new.main([]) == 0   # не должно падать без переменной


def test_recovery_failure_does_not_stop_processing(monkeypatch, capsys):
    """Сбой восстановления не должен отменять обработку новых анкет."""
    run = FakeRun([2], [STATUS_DONE])
    run.install(monkeypatch)

    def boom(**kw):
        raise RuntimeError("Google API недоступен")

    monkeypatch.setattr(process_new.recovery, "recover", boom)

    assert process_new.main([]) == 0
    assert run.processed == [2]


def test_sheet_read_failure_exits_with_1(monkeypatch):
    run = FakeRun([2], [STATUS_DONE])
    run.install(monkeypatch)

    def boom(status):
        raise RuntimeError("Google API недоступен")

    monkeypatch.setattr(process_new.sheets, "rows_with_status", boom)
    assert process_new.main([]) == 1
