"""Публикация статической формы на GitHub Pages.

Домен живёт в собираемом файле, а не в настройках репозитория: Pages
разворачивает артефакт целиком, и всё, чего в нём нет, при деплое исчезает.
Эти тесты стерегут и сборку домена, и то, что публикуется именно dist/.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
BUILD_GS = (ROOT / "apps-script" / "build_gs.py").read_text(encoding="utf-8")

DOMAIN = "hr.aiordo.ru"


@pytest.fixture(scope="module")
def wf():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML разбирает ключ `on` как булево True — обрабатываем оба варианта.
    data["_on"] = data.get("on") or data.get(True)
    return data


def steps(wf, job):
    return wf["jobs"][job]["steps"]


def step_using(wf, job, action):
    found = [s for s in steps(wf, job) if str(s.get("uses", "")).startswith(action)]
    assert found, f"в задаче {job} нет шага {action}"
    return found[0]


# ------------------------------ запуск ------------------------------------

def test_runs_on_push_to_main(wf):
    assert wf["_on"]["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in wf["_on"], "ручной запуск нужен для проверки домена"


def test_permissions_are_enough_and_not_more(wf):
    """id-token нужен deploy-pages для OIDC, pages: write — для самой заливки."""
    assert wf["permissions"]["pages"] == "write"
    assert wf["permissions"]["id-token"] == "write"
    assert wf["permissions"]["contents"] == "read", "записывать в репозиторий незачем"


def test_concurrency_never_cancels_running_deploy(wf):
    """Прерванная публикация оставила бы на домене половину страницы."""
    assert wf["concurrency"]["group"] == "pages"
    assert wf["concurrency"]["cancel-in-progress"] is False


# ------------------------------ сборка ------------------------------------

def test_page_is_rebuilt_before_publishing(wf):
    """Публикуется собранное сейчас, а не то, что лежало в репозитории.

    Иначе на домене оказалась бы анкета с устаревшими вопросами — ровно в
    том случае, когда кто-то правил источники и забыл пересобрать.
    """
    runs = [str(s.get("run", "")) for s in steps(wf, "build")]
    ci = next(i for i, r in enumerate(runs) if "npm ci" in r)
    build = next(i for i, r in enumerate(runs) if "npm run build" in r)
    assert ci < build, "npm ci должен идти до сборки"

    upload = steps(wf, "build").index(step_using(wf, "build", "actions/upload-pages-artifact"))
    assert upload > build, "выкладывать нужно уже собранное"


def test_uploads_exactly_dist(wf):
    assert step_using(wf, "build", "actions/upload-pages-artifact")["with"]["path"] == "dist"


def test_node_is_pinned(wf):
    setup = step_using(wf, "build", "actions/setup-node")
    assert setup["with"]["node-version"] == "20"


def test_deploy_waits_for_build(wf):
    deploy = wf["jobs"]["deploy"]
    assert deploy["needs"] == "build"
    assert deploy["environment"]["name"] == "github-pages"
    step_using(wf, "deploy", "actions/deploy-pages")


def test_actions_are_pinned_to_major_versions():
    """Плавающая ссылка на действие однажды сломает публикацию молча."""
    text = WORKFLOW.read_text(encoding="utf-8")
    used = re.findall(r"uses:\s*(\S+)", text)
    assert used, "в workflow нет ни одного действия"
    for action in used:
        assert "@" in action, f"{action} без версии"
        assert not action.endswith("@main"), f"{action} привязан к ветке"


# ------------------------------- домен ------------------------------------

def test_cname_is_generated_not_handwritten():
    """Файл должен пересобираться вместе со страницей.

    Положенный руками, он пережил бы ровно один деплой: Pages выкладывает
    артефакт целиком и стирает всё, чего в артефакте нет.
    """
    assert '"dist/CNAME": build_cname' in BUILD_GS
    # Имя домена лежит в app/config.py: ту же строку подставляет формула
    # «Ссылка для размещения» в листе «Размещения».
    assert "CUSTOM_DOMAIN = FORM_DOMAIN" in BUILD_GS

    from app.config import FORM_DOMAIN

    assert FORM_DOMAIN == DOMAIN


def test_cname_content():
    cname = (ROOT / "dist" / "CNAME").read_text(encoding="utf-8")
    assert cname == DOMAIN + "\n"
    # GitHub ждёт голое имя: со схемой, слэшем или портом домен не примут.
    assert "://" not in cname and "/" not in cname
    assert cname.strip().count(".") >= 1


def test_internal_docs_are_not_published(wf):
    """dist/README.md — инструкция для своих, а не страница на домене.

    Артефакт собирается из папки целиком, поэтому файл убирается из копии
    прогона до выкладки. В репозитории он при этом остаётся.
    """
    build_steps = steps(wf, "build")
    remove = next(
        i for i, s in enumerate(build_steps) if "rm -f dist/README.md" in str(s.get("run", ""))
    )
    upload = build_steps.index(step_using(wf, "build", "actions/upload-pages-artifact"))
    assert remove < upload, "убирать нужно до выкладки"
    assert (ROOT / "dist" / "README.md").exists(), "из репозитория файл не убирается"


def test_workflow_checks_domain_survived_build(wf):
    """Пропавший CNAME должен ронять прогон, а не тихо снимать домен."""
    runs = " ".join(str(s.get("run", "")) for s in steps(wf, "build"))
    assert "dist/CNAME" in runs
    assert "exit 1" in runs
