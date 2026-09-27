"""Checks on the sources: Python 3.8 syntax, string ids, the manifest."""

import ast
import os
import re
import threading
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "service.movieparadise")
STRINGS = os.path.join(ADDON, "resources", "language", "resource.language.en_gb", "strings.po")


def python_sources():
    for base, _, files in os.walk(ADDON):
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def test_every_source_parses_as_python_3_8():
    """Kodi 19 Matrix ships Python 3.8: no newer syntax may reach the add-on."""
    for path in python_sources():
        with open(path) as handle:
            ast.parse(handle.read(), filename=path, feature_version=(3, 8))


def _string_ids():
    with open(STRINGS, encoding="utf-8") as handle:
        return set(int(m) for m in re.findall(r'msgctxt "#(\d+)"', handle.read()))


def test_every_string_the_code_uses_exists():
    ids = _string_ids()
    used = set()
    for path in python_sources():
        with open(path) as handle:
            used.update(int(m) for m in re.findall(r"\b(3[0-9]{4})\b", handle.read()))
    missing = sorted(used - ids)
    assert not missing, missing


def test_settings_reference_existing_strings():
    ids = _string_ids()
    tree = ET.parse(os.path.join(ADDON, "resources", "settings.xml"))
    for node in tree.iter():
        for attr in ("label", "help"):
            value = node.get(attr)
            if value and value.isdigit():
                assert int(value) in ids, (node.tag, attr, value)


def test_the_manifest():
    root = ET.parse(os.path.join(ADDON, "addon.xml")).getroot()
    assert root.get("id") == "service.movieparadise"
    assert re.match(r"^\d+\.\d+\.\d+$", root.get("version"))
    requires = root.find("requires/import")
    assert requires.get("addon") == "xbmc.python" and requires.get("version") == "3.0.0"
    points = set(e.get("point") for e in root.findall("extension"))
    assert {"xbmc.service", "xbmc.python.script", "xbmc.addon.metadata"} <= points


# threading.Thread's private names on Python 3.8 (Kodi 19). Newer Pythons
# dropped some of them, _stop included, so a clash wouldn't show up there.
_THREAD_PRIVATE_3_8 = frozenset((
    "_args", "_bootstrap", "_bootstrap_inner", "_daemonic", "_delete", "_ident", "_initialized",
    "_invoke_excepthook", "_is_stopped", "_kwargs", "_name", "_native_id", "_reset_internal_locks",
    "_set_ident", "_set_native_id", "_set_tstate_lock", "_started", "_stderr", "_stop", "_target",
    "_tstate_lock", "_wait_for_tstate_lock",
))


def _thread_private_names():
    here = set(vars(threading.Thread())) | set(dir(threading.Thread))
    return _THREAD_PRIVATE_3_8 | set(n for n in here if n.startswith("_") and not n.startswith("__"))


def _is_thread_base(node):
    return (isinstance(node, ast.Attribute) and node.attr == "Thread") or (isinstance(node, ast.Name) and node.id == "Thread")


def test_no_thread_subclass_shadows_threadings_own_names():
    reserved = _thread_private_names()
    for path in python_sources():
        with open(path) as handle:
            tree = ast.parse(handle.read(), filename=path)
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef) or not any(_is_thread_base(b) for b in cls.bases):
                continue
            names = set()
            for node in ast.walk(cls):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node in cls.body:
                    names.add(node.name)
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) \
                        and isinstance(node.value, ast.Name) and node.value.id == "self":
                    names.add(node.attr)
            clash = sorted(names & reserved)
            assert not clash, (path, cls.name, clash)


def test_the_shadowing_check_catches_the_old_bug():
    source = "import threading\nclass T(threading.Thread):\n    def __init__(self):\n        self._stop = 1\n"
    cls = ast.parse(source).body[1]
    stored = set(n.attr for n in ast.walk(cls) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store))
    assert stored & _thread_private_names() == {"_stop"}
