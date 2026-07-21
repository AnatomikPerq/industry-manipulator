"""
Очередь прогонов: скриптовые слоты против единственного слота к ИИ.

ЧТО ЗДЕСЬ ЛОВИТСЯ. Прогон состоит из двух стадий разной природы: скрипты
считают локальный процессор и друг другу не мешают, а к LM Studio пропускается
строго одна сессия - сервер ИИ на всех один. Пока поток воркера оставался
занят сессией до конца прогона, четырёх сессий, дошедших до гейта, хватало,
чтобы пятая не начала считать скрипты ВОВСЕ: все воркеры стояли в ожидании
слота ИИ. Отказ тихий - сессия просто висит в статусе «в очереди».

Настоящий пайплайн здесь не запускается: подменяется только скрипт-раннер
(RUNNER_SCRIPT), а весь механизм очереди, подпроцессов, гейта и отмены
работает ровно тот же, что в бою, - с настоящими процессами и настоящим
stdin/stdout между ними.
"""

import textwrap
import time

import pytest

import queue_worker
from queue_worker import AnalysisQueue
from sessions import SessionStore

# Заглушка вместо _pipeline_runner: печатает маркер гейта, замирает на чтении
# stdin (как настоящий раннер) и, получив разрешение, ДЕРЖИТ слот ИИ, пока
# тест не разрешит закончить (файл-отмашка). Без удержания стадия «ИИ»
# проскакивала быстрее, чем её можно было наблюдать, - а весь смысл теста в
# том, что происходит, ПОКА слот занят.
FAKE_RUNNER = textwrap.dedent("""
    import json, os, sys, time
    args_path, release = sys.argv[1], RELEASE_FILE
    print("скрипты отработали", flush=True)
    print("@@LLM_WAIT", flush=True)
    sys.stdin.readline()                     # ждём разрешения от очереди
    deadline = time.monotonic() + 60
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.02)
    print("агенты отработали", flush=True)
    with open(args_path + ".result.json", "w", encoding="utf-8") as f:
        json.dump({"ok": True, "n_findings": 1}, f)
""")

# Заглушка для режима «без ИИ»: до гейта дело не доходит вовсе.
FAKE_RUNNER_NO_LLM = textwrap.dedent("""
    import json, sys
    with open(sys.argv[1] + ".result.json", "w", encoding="utf-8") as f:
        json.dump({"ok": True, "n_findings": 0}, f)
""")

TIMEOUT = 30.0


def wait_for(predicate, timeout=TIMEOUT, what=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Rig:
    """Очередь с подменённым раннером, фабрика сессий и рубильник «закончить».

    Пока рубильник не отпущен, сессия, дошедшая до стадии ИИ, ДЕРЖИТ слот -
    именно в этом состоянии и проверяется всё остальное.
    """

    def __init__(self, store, queue, tmp_path):
        self.store, self.queue = store, queue
        self._tmp = tmp_path
        self.release_file = tmp_path / "release"

    def new_session(self, name):
        sid = self.store.create(name)["id"]
        base = self.store.paths_of(sid)["base_files_dir"]
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{name} Э3.pdf").write_bytes(b"%PDF-")
        return sid

    def release(self):
        self.release_file.touch()

    def stages(self, sessions):
        return [self.store.get(s).get("stage") for s in sessions]


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    release_file = tmp_path / "release"
    runner = tmp_path / "fake_runner.py"
    runner.write_text(
        f"RELEASE_FILE = {str(release_file)!r}\n" + FAKE_RUNNER, encoding="utf-8")
    monkeypatch.setattr(queue_worker, "RUNNER_SCRIPT", runner)

    store = SessionStore(tmp_path / "sessions")
    queue = AnalysisQueue(store, tmp_path / "config.yaml")
    queue.start()
    rig = Rig(store, queue, tmp_path)
    yield rig
    rig.release()          # чтобы зависшие заглушки не пережили тест


def test_scripts_run_beyond_the_number_of_llm_slots(rig):
    """ГЛАВНЫЙ ТЕСТ МОДУЛЯ.

    Слот к ИИ ровно один, сессий в полном режиме - шесть, и все шесть упираются
    в гейт. Скриптовая стадия обязана отработать у ВСЕХ ШЕСТИ: она процессор
    отпустила. При прежнем фиксированном пуле дальше четвёртой сессии дело не
    шло - потоки стояли в ожидании ИИ.
    """
    n = 6
    assert n > queue_worker.SCRIPT_WORKERS, "тест бессмыслен без превышения пула"

    sessions = [rig.new_session(f"с{i}") for i in range(n)]
    for sid in sessions:
        rig.queue.enqueue(sid, "full")

    def all_past_scripts():
        # одна прошла гейт и работает с ИИ, остальные стоят к нему в очереди
        return all(s in ("очередь к ИИ", "ИИ") for s in rig.stages(sessions))

    assert wait_for(all_past_scripts), (
        "не все сессии добрались до стадии ИИ: " + str(rig.stages(sessions)))

    # ...и при этом к самому ИИ пропущена ровно одна
    snap = rig.queue.snapshot()
    assert snap["llm_busy"] == queue_worker.LLM_SLOTS == 1
    assert len(snap["llm_queue"]) == n - 1
    # скриптовые слоты все свободны: считать уже нечего
    assert snap["script_busy"] == 0
    assert rig.queue.snapshot()["queued"] == []


def test_llm_slot_is_strictly_serial(rig):
    """К серверу ИИ пропускается строго одна сессия за раз - иначе две
    дерутся за одну видеокарту и обе идут медленнее, чем шли бы по очереди."""
    sessions = [rig.new_session(f"с{i}") for i in range(3)]
    for sid in sessions:
        rig.queue.enqueue(sid, "full")

    assert wait_for(lambda: len(rig.queue.snapshot()["llm_queue"]) == 2)
    for _ in range(10):
        assert rig.queue.snapshot()["llm_busy"] <= 1
        time.sleep(0.05)


def test_llm_queue_drains(rig):
    """Очередь к ИИ рассасывается: отпущенный слот достаётся следующей, и все
    доходят до конца. Если бы слот терялся при переходе, вторая сессия висела
    бы «в очереди к ИИ» вечно."""
    sessions = [rig.new_session(f"с{i}") for i in range(3)]
    for sid in sessions:
        rig.queue.enqueue(sid, "full")
    assert wait_for(lambda: len(rig.queue.snapshot()["llm_queue"]) == 2)

    rig.release()
    assert wait_for(
        lambda: all(rig.store.get(s)["status"] == "done" for s in sessions),
        timeout=60), [rig.store.get(s)["status"] for s in sessions]
    assert [rig.store.get(s)["n_findings"] for s in sessions] == [1, 1, 1]
    assert rig.queue.snapshot()["llm_busy"] == 0


def test_scripts_only_mode_never_touches_llm_queue(tmp_path, monkeypatch):
    """Режим «без ИИ» к серверу не обращается вовсе и ждать очереди не должен -
    ровно на этом ловилась прежняя схема «в очередь встаёт весь прогон»."""
    runner = tmp_path / "fake_runner.py"
    runner.write_text(FAKE_RUNNER_NO_LLM, encoding="utf-8")
    monkeypatch.setattr(queue_worker, "RUNNER_SCRIPT", runner)

    store = SessionStore(tmp_path / "sessions")
    queue = AnalysisQueue(store, tmp_path / "config.yaml")
    queue.start()

    sid = store.create("без ии")["id"]
    base = store.paths_of(sid)["base_files_dir"]
    base.mkdir(parents=True, exist_ok=True)
    (base / "схема Э3.pdf").write_bytes(b"%PDF-")

    queue.enqueue(sid, "scripts")
    assert wait_for(lambda: store.get(sid)["status"] == "done"), store.get(sid)
    assert queue.snapshot()["llm_queue"] == []
    assert queue.snapshot()["llm_busy"] == 0


def test_cancel_while_waiting_for_llm(rig):
    """Отмена во время ожидания слота ИИ. Подпроцесс висит на чтении stdin, и
    смерть процесса снимает это ожидание вернее любого флага; слот при этом
    обязан достаться следующему, а не потеряться."""
    first, second = rig.new_session("первая"), rig.new_session("вторая")
    rig.queue.enqueue(first, "full")
    assert wait_for(lambda: rig.store.get(first).get("stage") == "ИИ")
    rig.queue.enqueue(second, "full")
    assert wait_for(lambda: rig.store.get(second).get("stage") == "очередь к ИИ")

    rig.queue.cancel(second)
    assert wait_for(lambda: rig.store.get(second)["status"] == "cancelled")
    assert second not in rig.queue.snapshot()["llm_queue"]

    rig.queue.cancel(first)
    assert wait_for(lambda: rig.store.get(first)["status"] == "cancelled")
    # слот отпущен - очередь не осталась запертой навсегда
    assert wait_for(lambda: rig.queue.snapshot()["llm_busy"] == 0)


def test_cancelled_session_releases_script_slot(rig):
    """Отменённая сессия обязана отпустить и скриптовый слот - иначе каждая
    отмена навсегда сужала бы пул."""
    sid = rig.new_session("ждущая")
    rig.queue.enqueue(sid, "full")
    assert wait_for(lambda: rig.store.get(sid)["status"] == "running")
    rig.queue.cancel(sid)
    assert wait_for(lambda: rig.store.get(sid)["status"] == "cancelled")
    assert wait_for(lambda: rig.queue.snapshot()["script_busy"] == 0)
    # статус сессии пишет сам прогон, а из running её убирает поток уже ПОСЛЕ
    # этого - поэтому ждём, а не проверяем сразу
    assert wait_for(lambda: rig.queue.snapshot()["running"] == [])


def test_slot_freed_after_cancel_lets_next_session_start(rig):
    """После отмены пул принимает новую сессию: слот не «протёк»."""
    sids = [rig.new_session(f"с{i}") for i in range(queue_worker.SCRIPT_WORKERS)]
    for sid in sids:
        rig.queue.enqueue(sid, "full")
    assert wait_for(lambda: all(s in ("очередь к ИИ", "ИИ")
                                for s in rig.stages(sids)))
    for sid in sids:
        rig.queue.cancel(sid)
    assert wait_for(lambda: rig.queue.snapshot()["script_busy"] == 0)

    fresh = rig.new_session("новая")
    rig.queue.enqueue(fresh, "full")
    assert wait_for(lambda: rig.store.get(fresh)["status"] == "running")


def test_enqueue_rejects_session_without_files(rig):
    sid = rig.store.create("пустая")["id"]
    with pytest.raises(Exception) as e:
        rig.queue.enqueue(sid, "full")
    assert "нет файлов" in str(e.value)
