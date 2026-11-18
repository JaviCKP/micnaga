"""Senal del micro: lector de registro falso (el real es solo lectura)."""

from __future__ import annotations

import os
import sys

import pytest

from micnaga.watcher.registry_mic import (
    RegistryMicSource,
    WinregMicReader,
    exe_basename,
    filetime_to_unix,
    friendly_app_name,
    own_exe_paths,
    psutil_exe_running,
    subkey_to_path,
)

CHROME = r"C:#Program Files#Google#Chrome#Application#chrome.exe"
EDGE = r"C:#Program Files (x86)#Microsoft#Edge#Application#msedge.exe"
DISCORD = r"C:#Users#javi#AppData#Local#Discord#app-1.0.9249#Discord.exe"
HANDY = r"C:#Users#javi#AppData#Local#Handy#handy.exe"
OWN_PY = r"C:#Users#javi#micnaga-app#.venv#Scripts#python.exe"
TEAMS = "MSTeams_8wekyb3d8bbwe"
WHATSAPP = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm"
FILETIME = 134368959000500216


class FakeReader:
    def __init__(
        self,
        data: dict[str, tuple[int, int]],
        packages: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.data = data
        self.packages = packages or {}
        self.enumerations = 0
        self.reads = 0

    def subkey_count(self) -> int:
        return len(self.data)

    def subkey_names(self) -> list[str]:
        self.enumerations += 1
        return list(self.data)

    def read_times(self, subkey: str) -> tuple[int, int] | None:
        self.reads += 1
        return self.data.get(subkey)

    def package_count(self) -> int:
        return len(self.packages) + 1

    def package_names(self) -> list[str]:
        return list(self.packages)

    def read_package_times(self, name: str) -> tuple[int, int] | None:
        return self.packages.get(name)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Procs:
    """Procesos vivos falsos: ``(ruta o nombre) -> create_time`` en segundos unix."""

    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.exe_asked: list[tuple[str, float]] = []
        self.pkg_asked: list[tuple[str, float]] = []

    def exe(self, path: str, started_before: float) -> bool:
        self.exe_asked.append((path, started_before))
        return self.running

    def pkg(self, family: str, started_before: float) -> bool:
        self.pkg_asked.append((family, started_before))
        return self.running


def make(
    data: dict[str, tuple[int, int]],
    running: bool = True,
    packages: dict[str, tuple[int, int]] | None = None,
    **kw: object,
) -> tuple[RegistryMicSource, FakeReader, Clock, Procs]:
    reader = FakeReader(data, packages)
    clock = Clock()
    procs = Procs(running)
    kw.setdefault("own_paths", frozenset())
    src = RegistryMicSource(
        ["chrome.exe", "msedge.exe", "brave.exe"],
        reader,
        procs.exe,
        package_running=procs.pkg,
        clock=clock,
        **kw,  # type: ignore[arg-type]
    )
    return src, reader, clock, procs


def test_helpers() -> None:
    assert exe_basename(CHROME) == "chrome.exe"
    assert exe_basename(DISCORD) == "discord.exe"
    assert subkey_to_path(CHROME) == r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def test_stop_zero_means_in_use() -> None:
    src, *_ = make({CHROME: (FILETIME, 0)})
    r = src.read()
    assert r.browsers == {"chrome.exe"} and r.reading.active
    (app,) = r.apps
    assert app.key == "app:chrome.exe" and app.is_browser and app.label == "navegador"


def test_stop_nonzero_means_idle() -> None:
    src, *_ = make({CHROME: (FILETIME, FILETIME + 10)})
    r = src.read()
    assert r.browsers == frozenset() and r.apps == frozenset()


def test_never_used_start_zero_is_not_in_use() -> None:
    src, *_ = make({CHROME: (0, 0)})
    assert src.read().browsers == frozenset()


def test_non_browser_apps_are_apps_but_not_browsers() -> None:
    src, *_ = make({DISCORD: (FILETIME, 0), EDGE: (FILETIME, 0)})
    r = src.read()
    assert r.browsers == {"msedge.exe"}
    assert {a.key for a in r.apps} == {"app:msedge.exe", "app:discord.exe"}


def test_generic_off_only_reports_browsers() -> None:
    src, reader, *_ = make({DISCORD: (FILETIME, 0), EDGE: (FILETIME, 0)}, generic=False)
    r = src.read()
    assert r.browsers == {"msedge.exe"} and r.apps == frozenset()
    assert reader.reads == 1  # ni siquiera lee las entradas que no son navegadores


def test_two_browsers_at_once() -> None:
    src, *_ = make({CHROME: (FILETIME, 0), EDGE: (FILETIME, 0)})
    assert src.read().browsers == {"chrome.exe", "msedge.exe"}


def test_packaged_apps_are_read() -> None:
    src, _r, _c, procs = make({}, packages={TEAMS: (FILETIME, 0), WHATSAPP: (FILETIME, 5)})
    r = src.read()
    (app,) = r.apps
    assert app.key == f"app:{TEAMS.lower()}" and app.label == "Teams" and app.package == TEAMS
    assert app.exe is None and not app.is_browser
    assert procs.pkg_asked and procs.pkg_asked[0][0] == TEAMS


def test_packaged_apps_ignored_when_generic_is_off() -> None:
    src, *_ = make({}, packages={TEAMS: (FILETIME, 0)}, generic=False)
    assert src.read().apps == frozenset()


def test_stale_entry_without_a_live_process_is_ignored() -> None:
    """Un navegador que murio con el micro abierto deja Stop == 0 para siempre."""
    src, *_ = make({CHROME: (FILETIME, 0)}, running=False)
    r = src.read()
    assert r.browsers == frozenset() and r.apps == frozenset()
    assert r.reading.detail["stale_entries_ignored"] == ["chrome.exe"]


def test_liveness_is_checked_against_the_mic_start_time() -> None:
    src, _r, _c, procs = make({CHROME: (FILETIME, 0)})
    src.read()
    (path, started_before) = procs.exe_asked[0]
    assert path == r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert started_before == pytest.approx(filetime_to_unix(FILETIME) + 10.0)


def test_process_check_is_cached_and_only_done_when_in_use() -> None:
    src, _reader, clock, procs = make({CHROME: (FILETIME, FILETIME + 1), EDGE: (FILETIME, 0)})
    for _ in range(4):
        src.read()
        clock.t += 1
    assert len(procs.exe_asked) == 1  # solo la entrada en uso, una vez en 4 s
    clock.t += 10
    src.read()
    assert len(procs.exe_asked) == 2


def test_stale_verdict_is_cached_long_but_a_new_mic_start_is_rechecked() -> None:
    src, reader, clock, procs = make({CHROME: (FILETIME, 0)}, running=False)
    for _ in range(20):
        src.read()
        clock.t += 1
    assert len(procs.exe_asked) == 1
    reader.data[CHROME] = (FILETIME + 10**9, 0)  # nuevo uso del micro
    src.read()
    assert len(procs.exe_asked) == 2


def test_subkey_enumeration_is_cached_until_the_count_changes() -> None:
    src, reader, clock, _ = make({CHROME: (FILETIME, 0)})
    for _ in range(10):
        src.read()
        clock.t += 1
    assert reader.enumerations == 1
    reader.data[EDGE] = (FILETIME, 0)  # primer uso de otro navegador -> nueva subclave
    assert src.read().browsers == {"chrome.exe", "msedge.exe"}
    assert reader.enumerations == 2


def test_new_package_subkey_is_picked_up() -> None:
    src, reader, _clock, _ = make({}, packages={})
    assert src.read().apps == frozenset()
    reader.packages[TEAMS] = (FILETIME, 0)
    assert {a.package for a in src.read().apps} == {TEAMS}


def test_enumeration_refreshes_periodically() -> None:
    src, reader, clock, _ = make({CHROME: (FILETIME, 0)})
    src.read()
    clock.t += 61
    src.read()
    assert reader.enumerations == 2


def test_reader_errors_do_not_raise() -> None:
    class Broken(FakeReader):
        def subkey_count(self) -> int:
            raise OSError("boom")

    src = RegistryMicSource(["chrome.exe"], Broken({}), lambda *_: True)
    r = src.read()
    assert r.browsers == frozenset() and r.reading.error and "boom" in r.reading.error


def test_missing_values_are_skipped() -> None:
    class Partial(FakeReader):
        def read_times(self, subkey: str) -> tuple[int, int] | None:
            return None

    src = RegistryMicSource(["chrome.exe"], Partial({CHROME: (1, 0)}), lambda *_: True)
    assert src.read().browsers == frozenset()


# ----------------------------------------------------------------------------- exclusiones


def test_own_interpreter_path_is_excluded_from_generic_apps() -> None:
    own = OWN_PY.replace("#", "\\").lower()
    src, *_ = make({OWN_PY: (FILETIME, 0), HANDY: (FILETIME, 0)}, own_paths=frozenset({own}))
    r = src.read()
    assert {a.key for a in r.apps} == {"app:handy.exe"}
    assert r.reading.detail["excluded_in_use"] == ["python.exe"]


def test_pythonw_variant_of_our_interpreter_is_excluded() -> None:
    paths = own_exe_paths()
    assert os.path.normcase(os.path.normpath(sys.executable)) in paths
    folder = os.path.dirname(sys.executable)
    assert os.path.normcase(os.path.join(folder, "pythonw.exe")) in paths


def test_micnaga_exe_is_always_excluded() -> None:
    src, *_ = make({r"C:#Program Files#Micnaga#micnaga.exe": (FILETIME, 0)})
    assert src.read().apps == frozenset()


def test_exclude_apps_by_exe_path_package_and_family_stem() -> None:
    src, *_ = make(
        {HANDY: (FILETIME, 0), DISCORD: (FILETIME, 0)},
        packages={TEAMS: (FILETIME, 0), WHATSAPP: (FILETIME, 0)},
        exclude=["Handy.exe", TEAMS, "5319275A.WhatsAppDesktop"],
    )
    assert {a.key for a in src.read().apps} == {"app:discord.exe"}


def test_excluding_a_browser_keeps_it_on_the_meet_fast_path() -> None:
    src, *_ = make({CHROME: (FILETIME, 0)}, exclude=["chrome.exe"])
    r = src.read()
    assert r.browsers == {"chrome.exe"} and r.apps == frozenset()


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("MSTeams_8wekyb3d8bbwe", "Teams"),
        ("ms-teams.exe", "Teams"),
        ("5319275A.WhatsAppDesktop_cv1g1gvanyjgm", "WhatsApp"),
        ("zoom.exe", "Zoom"),
        ("chrome.exe", "navegador"),
        ("msedge.exe", "navegador"),
        ("handy.exe", "handy.exe"),
        ("Foo.Bar_abc123", "Foo.Bar"),
    ],
)
def test_friendly_names(name: str, label: str) -> None:
    assert friendly_app_name(name) == label


def test_real_registry_is_readable_read_only() -> None:
    """Humo con el registro real (solo lectura): no assert sobre el contenido."""
    reader = WinregMicReader()
    names = reader.subkey_names()
    assert reader.subkey_count() == len(names)
    for name in names[:3]:
        times = reader.read_times(name)
        assert times is None or (isinstance(times[0], int) and isinstance(times[1], int))
    assert reader.read_times("no-such-key#nothing.exe") is None
    packages = reader.package_names()
    assert "nonpackaged" not in [p.lower() for p in packages]
    assert reader.package_count() == len(packages) + 1 or reader.package_count() >= len(packages)
    for pkg in packages[:3]:
        times = reader.read_package_times(pkg)
        assert times is None or len(times) == 2


def test_real_process_check_handles_missing_and_future_start() -> None:
    assert psutil_exe_running(r"C:\no\such\thing.exe", 1e12) is False
    # El proceso real (python) se creo despues de t=0: no puede tener un uso que empezo antes.
    assert psutil_exe_running(sys.executable, 0.0) is False


@pytest.mark.parametrize("name", ["chrome.exe", "msedge.exe", "brave.exe"])
def test_default_browsers_match_basenames(name: str) -> None:
    src, *_ = make({f"C:#x#{name}": (FILETIME, 0)})
    assert src.read().browsers == {name}
