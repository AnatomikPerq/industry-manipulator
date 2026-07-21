"""
Загрузка модуля из data/base_analysis_scripts по ПУТИ к файлу.

ПОЧЕМУ НЕ ОБЫЧНЫЙ import. Папка скрипт-парсеров не пакет и в sys.path не
лежит: она КОПИРУЕТСЯ В КАЖДУЮ СЕССИЮ (SessionStore.prepare_run), потому что
промпт агента описывает её как подпапку своей песочницы data/, а в архиве
сессии должна остаться та версия парсеров, которой её считали. Значит, грузить
надо именно ту копию, которая принадлежит этому прогону, - по пути, а не по
имени.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Этих загрузчиков было ЧЕТЫРЕ, все чуть-чуть разные:
ingest._load_parser (с проверкой extract_to_dir), main._load_parser_module,
full_project._load_font_fix и full_project._load_progress - и пятый, уже
написанный и никем не используемый, в самом progress.load(). Каждый со своим
ключом кэша, из-за чего один и тот же файл мог быть загружен в процесс дважды
как два разных модуля (_base_parser_schematic_rules и _stage_schematic_rules).

Ключ кэша включает ПУТЬ, а не только имя файла: иначе в одном процессе нельзя
было бы работать с двумя разными копиями папки скриптов, а именно это и
происходит, когда CLI и сессия живут рядом.
"""

import importlib.util
import sys
from pathlib import Path


class ScriptLoadError(Exception):
    """Скрипт не найден или не подходит по контракту."""


def load(scripts_dir, script_name: str, require=()):
    """Модуль script_name из scripts_dir.

    require: имена, которые модуль обязан предоставлять. Проверяются сразу,
    чтобы несоответствие контракту всплывало на загрузке понятным сообщением,
    а не позже - AttributeError'ом где-то в середине прогона.
    """
    path = (Path(scripts_dir) / script_name).resolve()
    if not path.is_file():
        raise ScriptLoadError(f"Скрипт не найден: {path}")

    # путь в ключе: у CLI и у каждой сессии своя копия папки скриптов, и
    # подменять одну другой нельзя
    mod_name = f"_bas_{abs(hash(str(path.parent))):x}_{path.stem}"
    module = sys.modules.get(mod_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        # в sys.modules ДО exec_module: скрипты импортируют друг друга
        # (assembly_drawing_to_data тянет schematic_diagram_to_data), и без
        # этого циклический импорт грузил бы модуль второй раз
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)   # не оставляем полумёртвый модуль
            raise

    for attr in require:
        if not hasattr(module, attr):
            raise ScriptLoadError(
                f"{script_name} не имеет {attr} - пайплайн не может его вызвать")
    return module


def try_load(scripts_dir, script_name: str):
    """То же, но None вместо исключения.

    Для необязательных вещей - прежде всего progress.py: сообщения о ходе
    разбора не повод ронять извлечение, и без них всё работает молча.
    """
    try:
        return load(scripts_dir, script_name)
    except Exception:  # noqa: BLE001 - причина не важна, важно что не упали
        return None
