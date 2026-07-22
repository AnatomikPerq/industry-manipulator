"""
ВЫБОР МОДЕЛЕЙ И ЧИСЛА АГЕНТОВ В ИНТЕРФЕЙСЕ (V2.0).

Модели на сервере меняются чаще, чем проект: одну выгрузили, другая не влезла
в память. До V2.0 выбор жил только в config.yaml, и человеку, у которого не
загрузилась модель агента, оставалось править YAML на сервере.

Здесь охраняется то, чего в отчёте не видно:
  * выбор хранится В СЕССИИ и не трогает общий config.yaml (иначе один
    пользователь молча перенастроил бы прогоны всех остальных);
  * при сборке конфига прогона переопределяется ТОЛЬКО названное - адрес
    сервера, лимиты и context_window продолжают браться из общего конфига;
  * бюджет протокола для стадии отчёта считается от РЕАЛЬНОГО контекста
    модели, а не от константы.
"""

import sys
from pathlib import Path

import pytest
import yaml

from sessions import SessionError, SessionStore

WEB_APP = Path(__file__).resolve().parents[2] / "web_app"
if str(WEB_APP) not in sys.path:
    sys.path.insert(0, str(WEB_APP))

import _pipeline_runner as runner            # noqa: E402


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions")


@pytest.fixture
def session(store):
    return store, store.create("выбор моделей")["id"]


# ---------------------------------------------------------------- хранение

def test_choice_is_stored_in_the_session(session):
    store, sid = session
    store.set_llm(sid, {"agent_1": "some-model", "agents_count": 2})
    assert store.get(sid)["llm"] == {"agent_1": "some-model", "agents_count": 2}


def test_empty_value_resets_to_config_default(session):
    """Пустой выбор - это «как в общем config.yaml», а не «никакая модель».

    Так пользователь возвращает значение по умолчанию, не зная, какое оно.
    """
    store, sid = session
    store.set_llm(sid, {"vision": "some-vision-model"})
    store.set_llm(sid, {"vision": None})
    assert "vision" not in store.get(sid)["llm"]


@pytest.mark.parametrize("bad", [
    {"agents_count": 3},                 # агентов бывает 1 или 2
    {"agents_count": 0},
    {"single_agent": "agent_3"},
    {"agent_1": "x" * 400},              # не имя модели, а простыня
    {"agent_1": 42},
])
def test_bad_settings_are_rejected(session, bad):
    store, sid = session
    with pytest.raises(SessionError):
        store.set_llm(sid, bad)


def test_settings_frozen_while_running(session):
    """Менять модели на ходу нельзя: прогон уже читает конфиг сессии."""
    store, sid = session
    store.update(sid, status="running")
    with pytest.raises(SessionError):
        store.set_llm(sid, {"agent_1": "another-model"})


def test_unknown_model_is_accepted(session):
    """Существование модели на сервере здесь НЕ проверяется сознательно.

    Сервер бывает временно выключен, а список моделей на нём меняется.
    Запретить сохранить выбор из-за того, что LM Studio сейчас не отвечает,
    значило бы связать настройку с состоянием сети.
    """
    store, sid = session
    store.set_llm(sid, {"agent_1": "модель-которой-пока-нет"})
    assert store.get(sid)["llm"]["agent_1"] == "модель-которой-пока-нет"


# ---------------------------------------------------------------- конфиг прогона

def base_config(tmp_path):
    cfg = {
        "llm_servers": {
            "agent_1": {"base_url": "http://sample:1234/v1", "model": "из-конфига-1",
                        "context_window": 200000, "max_tokens": 50000,
                        "temperature": 0.2},
            "agent_2": {"base_url": "http://sample:1234/v1", "model": "из-конфига-2",
                        "context_window": 200000, "max_tokens": 50000},
            "vision": {"use_agent": "agent_1", "model": "из-конфига-зрение"},
            "merger": {"use_agent": "agent_1"},
        },
        "agents": {"count": 1, "single_agent": "agent_1"},
        "paths": {"base_files_dir": "./data/base_files",
                  "full_projects_dir": "./data/full_projects",
                  "scripts_dir": "./data/base_analysis_scripts",
                  "helper_scripts_dir": "./data/helper",
                  "input_dir": "./data", "output_dir": "./output",
                  "known_errors_file": "./known_errors.json"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def build(tmp_path, llm):
    out = tmp_path / "session.yaml"
    paths = {k: str(tmp_path / k) for k in
             ("base_files_dir", "full_projects_dir", "scripts_dir",
              "helper_scripts_dir", "input_dir", "output_dir")}
    runner.build_session_config(str(base_config(tmp_path)), paths, str(out), llm=llm)
    return yaml.safe_load(out.read_text(encoding="utf-8"))


def test_chosen_models_reach_the_run_config(tmp_path):
    cfg = build(tmp_path, {"agent_1": "выбранная", "vision": "выбранное-зрение",
                           "agents_count": 2})
    assert cfg["llm_servers"]["agent_1"]["model"] == "выбранная"
    assert cfg["llm_servers"]["vision"]["model"] == "выбранное-зрение"
    assert cfg["agents"]["count"] == 2


def test_only_the_named_is_overridden(tmp_path):
    """Адрес сервера, лимиты и температура - из общего конфига.

    Там они выверены; подменять их из интерфейса незачем, а незаметная подмена
    base_url означала бы прогон не на том сервере.
    """
    cfg = build(tmp_path, {"agent_1": "выбранная"})
    server = cfg["llm_servers"]["agent_1"]
    assert server["base_url"] == "http://sample:1234/v1"
    assert server["context_window"] == 200000
    assert server["max_tokens"] == 50000
    assert server["temperature"] == 0.2
    # чужая ветка не тронута
    assert cfg["llm_servers"]["agent_2"]["model"] == "из-конфига-2"


def test_without_choice_config_is_unchanged(tmp_path):
    cfg = build(tmp_path, None)
    assert cfg["llm_servers"]["agent_1"]["model"] == "из-конфига-1"
    assert cfg["agents"] == {"count": 1, "single_agent": "agent_1"}


# ---------------------------------------------------------------- бюджет контекста

def test_transcript_budget_shrinks_with_context():
    """Протокол режется по РЕАЛЬНОМУ контексту, а не по константе.

    Прежде он резался по 60000 символов - числу, ни от чего не зависящему. На
    модели, загруженной с контекстом 8192, это около 20000 токенов при
    доступных шести тысячах, и стадия отчёта падала сразу после того, как агент
    честно отработал пять минут и собрал факты.
    """
    from oi_agent import _transcript_budget

    big = _transcript_budget(200000, 8000, fixed_chars=15000)
    small = _transcript_budget(8192, 2048, fixed_chars=15000)
    assert big > 100000
    # при контексте 8192 фиксированная часть промпта уже не влезает
    assert small < 0


def test_real_context_wins_over_config(monkeypatch):
    """Правда о контексте - у сервера. context_window в конфиге пишет человек,
    и он расходится с тем, как модель загрузили в LM Studio."""
    import oi_agent

    monkeypatch.setattr(oi_agent.llm_client, "loaded_context_length",
                        lambda cfg, timeout=5.0: 8192)
    ctx, max_tokens = oi_agent._sane_token_limits(
        {"model": "m", "context_window": 200000, "max_tokens": 50000})
    assert ctx == 8192
    assert max_tokens <= 8192 * oi_agent.MAX_TOKENS_SHARE_OF_CONTEXT


def test_unreachable_server_keeps_config_value(monkeypatch):
    """Сервер не ответил - работаем по конфигу, как раньше: недоступность
    проверки не должна становиться условием запуска."""
    import oi_agent

    monkeypatch.setattr(oi_agent.llm_client, "loaded_context_length",
                        lambda cfg, timeout=5.0: None)
    ctx, _ = oi_agent._sane_token_limits(
        {"model": "m", "context_window": 120000, "max_tokens": 1000})
    assert ctx == 120000
