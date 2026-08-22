"""Возврат зависших строк: без него падение процесса теряет кандидата."""
import time

import pytest

from app import recovery
from app.config import STATUS_NEW


@pytest.fixture(autouse=True)
def clean_registry():
    recovery._claimed.clear()
    yield
    recovery._claimed.clear()


class Spy:
    def __init__(self, processing):
        self.processing = processing
        self.set = []
        self.restarted = []

    def rows_in_processing(self):
        return list(self.processing)

    def set_status(self, row, status):
        self.set.append((row, status))


def install(monkeypatch, spy):
    monkeypatch.setattr(recovery.sheets, "rows_in_processing", spy.rows_in_processing)
    monkeypatch.setattr(recovery.sheets, "set_status", spy.set_status)
    # Перезапуск обработки делаем синхронно, чтобы тест не гонялся с потоком.
    monkeypatch.setattr(
        recovery.threading, "Thread",
        lambda target, args, name, daemon: type(
            "T", (), {"start": lambda self: spy.restarted.append(args[0])}
        )(),
    )


def test_startup_revives_everything(monkeypatch):
    """После перезапуска процесса все processing осиротевшие по определению."""
    spy = Spy([4, 7, 9])
    install(monkeypatch, spy)

    revived = recovery.recover(startup=True)

    assert revived == [4, 7, 9]
    assert spy.set == [(4, STATUS_NEW), (7, STATUS_NEW), (9, STATUS_NEW)]
    assert spy.restarted == [4, 7, 9], "строки должны быть поставлены в обработку заново"


def test_scan_spares_rows_in_flight(monkeypatch):
    """Строку, которую этот процесс взял только что, трогать нельзя."""
    spy = Spy([5])
    install(monkeypatch, spy)
    recovery.mark_claimed(5)

    assert recovery.recover() == []
    assert spy.set == []


def test_scan_revives_timed_out_row(monkeypatch):
    """Своя же строка, висящая дольше таймаута, возвращается в работу."""
    spy = Spy([5])
    install(monkeypatch, spy)
    recovery._claimed[5] = time.time() - (recovery.STUCK_TIMEOUT_SEC + 1)

    assert recovery.recover() == [5]
    assert spy.set == [(5, STATUS_NEW)]
    assert 5 not in recovery._claimed, "реестр должен освободиться"


def test_scan_revives_orphan_row(monkeypatch):
    """Строка, которую этот процесс не захватывал, считается осиротевшей."""
    spy = Spy([11])
    install(monkeypatch, spy)

    assert recovery.recover() == [11]
    assert spy.restarted == [11]


def test_sheet_error_does_not_kill_scanner(monkeypatch):
    def boom():
        raise RuntimeError("Google API недоступен")

    monkeypatch.setattr(recovery.sheets, "rows_in_processing", boom)
    assert recovery.recover() == [], "ошибка чтения не должна ронять сканер"


def test_released_row_is_not_revived(monkeypatch):
    """Завершённая обработка снимает строку с учёта."""
    recovery.mark_claimed(3)
    recovery.mark_released(3)
    assert 3 not in recovery._claimed
