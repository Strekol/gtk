"""Deep link T3 Code (spec 003) : clic sur une session T3 → bon thread.

003-AC7 (symboles de détection partagés intacts) est couvert par
tests/test_core_parity.py : pas dupliqué ici.
"""

import inspect
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ENV_ID = '11111111-2222-3333-4444-555555555555'
SESSION = '212b6857-aaaa-bbbb-cccc-000000000001'
THREAD = 'fb1bb28a-aaaa-bbbb-cccc-000000000002'
TABLE = 'orchestration_v2_projection_provider_threads'


def _payload(native_id: str, app_thread_id: str | None) -> str:
    return json.dumps({
        'appThreadId': app_thread_id,
        'nativeThreadRef': {'driver': 'claudeAgent', 'nativeId': native_id, 'strength': 'strong'},
    })


# Écrivain T3 simulé dans un AUTRE process : un écrivain in-process partagerait
# le mapping -shm de SQLite avec le lecteur testé, et on ne pourrait pas mesurer
# ce que le lecteur y écrit.
_WRITER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
# Une écriture, comme T3 en continu : des trames dans -wal obligent un lecteur à
# poser une marque de lecture neuve dans -shm (c'est ce qu'on veut voir refusé).
conn.execute('CREATE TABLE writer_tick (x)')
print('ready', flush=True)
sys.stdin.read()
"""


@pytest.fixture
def make_home(tmp_path):
    """Fabrique un home T3 dont la base est en WAL, comme la vraie.

    live=True lance un écrivain dans un sous-process (T3 lancé) : -wal/-shm
    existent, comme en vrai. live=False : aucune connexion ouverte, SQLite a
    checkpointé et supprimé -wal (T3 fermé).
    """
    writers: list[subprocess.Popen[str]] = []

    def make(rows=((SESSION, THREAD, '2026-10-19T10:00:00Z'),), env_id: str | None = ENV_ID,
             schema: bool = True, live: bool = True) -> Path:
        home = tmp_path / '.t3'
        userdata = home / 'userdata'
        userdata.mkdir(parents=True)
        if env_id is not None:
            (userdata / 'environment-id').write_text(env_id + '\n')
        db = userdata / 'statev2.sqlite'
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute('PRAGMA journal_mode=WAL')
        if schema:
            conn.execute(f'CREATE TABLE {TABLE} (provider_thread_id TEXT, thread_id TEXT,'
                         ' updated_at TEXT, payload_json TEXT)')
            for i, (native, app, updated) in enumerate(rows):
                conn.execute(f'INSERT INTO {TABLE} VALUES (?, ?, ?, ?)',
                             (f'p{i}', f't{i}', updated, _payload(native, app)))
        else:
            conn.execute('CREATE TABLE other (x TEXT)')
        conn.close()
        assert not (userdata / 'statev2.sqlite-wal').exists()
        if live:
            proc = subprocess.Popen([sys.executable, '-c', _WRITER, str(db)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            writers.append(proc)
            assert proc.stdout and proc.stdout.readline().strip() == 'ready'
            assert (userdata / 'statev2.sqlite-wal').exists()
            assert (userdata / 'statev2.sqlite-shm').exists()
        return home

    yield make
    # Pas d'orphelin : stdin fermé = fin propre ; kill en dernier recours.
    for proc in writers:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()


def _session(home: Path | None, **kw) -> dict:
    return {'pid': 42, 'window_id': '0xabc', 'terminal_pid': 7, 'kitty_socket': None,
            'kitty_window_id': None, 'session_id': SESSION,
            't3_home': str(home) if home else None, **kw}


@pytest.fixture
def calls(watcher, monkeypatch):
    """Journal ordonné : raise (subprocess.run) puis launch (Gio)."""
    log: list[tuple] = []
    monkeypatch.setattr(watcher, 'IS_WAYLAND', False)
    monkeypatch.setattr(watcher.subprocess, 'run',
                        lambda *a, **k: log.append(('run', a[0])) or MagicMock(returncode=0, stdout=''))
    gio = MagicMock()
    gio.AppInfo.launch_default_for_uri.side_effect = lambda uri, ctx: log.append(('launch', uri)) or True
    monkeypatch.setattr(watcher, 'Gio', gio)
    return log


def _launches(log):
    return [c for c in log if c[0] == 'launch']


def _runs(log):
    return [c for c in log if c[0] == 'run']


# ── t3_home ────────────────────────────────────────────────────────────────────

def test_t3_home_marker_absent(watcher):
    assert watcher.t3_home({}) is None
    assert watcher.t3_home({'CHROME_DESKTOP': 'code.desktop', 'T3CODE_CLI_PATH': '/x/bin/t3'}) is None


def test_t3_home_from_cli_path(watcher):
    env = {'CHROME_DESKTOP': watcher.T3_DESKTOP_ID, 'T3CODE_CLI_PATH': '/opt/t3home/bin/t3'}
    assert watcher.t3_home(env) == Path('/opt/t3home')


@pytest.mark.parametrize('env', [{}, {'T3CODE_CLI_PATH': ''}])
def test_t3_home_fallback(watcher, env):
    env = {'CHROME_DESKTOP': watcher.T3_DESKTOP_ID, **env}
    assert watcher.t3_home(env) == Path.home() / '.t3'


# ── 003-AC1 ────────────────────────────────────────────────────────────────────

def test_ac1_t3_session_opens_thread_after_raise(watcher, calls, make_home):
    """003-AC1 : URI exacte, passée à Gio APRÈS le raise wmctrl."""
    home = make_home()
    assert watcher.focus_terminal(_session(home)) is True
    expected = f't3code://threads/{ENV_ID}/{THREAD}'
    assert _launches(calls) == [('launch', expected)]
    watcher.Gio.AppInfo.launch_default_for_uri.assert_called_once_with(expected, None)
    first_launch = calls.index(('launch', expected))
    assert any(c[0] == 'run' for c in calls[:first_launch])


def test_ac1_wayland_path_also_opens_thread(watcher, calls, make_home, monkeypatch):
    """003-AC1 : la branche Wayland enchaîne aussi sur le deep link."""
    monkeypatch.setattr(watcher, 'IS_WAYLAND', True)
    home = make_home()
    assert watcher.focus_terminal(_session(home)) is True
    assert _launches(calls) == [('launch', f't3code://threads/{ENV_ID}/{THREAD}')]
    assert calls.index(_launches(calls)[0]) > 0


# ── 003-AC2 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('row', [
    {'t3_home': None},                              # terminal / kitty
    {'t3_home': None, 'daemon': True},              # démon
])
def test_ac2_non_t3_session_never_launches(watcher, calls, row):
    """003-AC2 : hors T3, aucun deep link."""
    watcher.focus_terminal(dict(_session(None), **row))
    assert _launches(calls) == []
    assert _runs(calls) != []


def test_ac2_remote_session_never_launches(watcher, calls, make_home):
    """003-AC2 : session distante → garde intacte, ni raise ni launch."""
    home = make_home()
    assert watcher.focus_terminal(_session(home, remote={'name': 'r'})) is False
    assert calls == []


# ── 003-AC3 ────────────────────────────────────────────────────────────────────

def _assert_degraded(watcher, calls, home):
    assert watcher.focus_terminal(_session(home)) is True  # raise wmctrl seul
    assert _launches(calls) == []
    assert _runs(calls) != []


def test_ac3_missing_db(watcher, calls, tmp_path):
    """003-AC3 : base absente."""
    home = tmp_path / '.t3'
    (home / 'userdata').mkdir(parents=True)
    (home / 'userdata' / 'environment-id').write_text(ENV_ID)
    _assert_degraded(watcher, calls, home)


def test_ac3_missing_environment_id(watcher, calls, make_home):
    """003-AC3 : environment-id absent."""
    _assert_degraded(watcher, calls, make_home(env_id=None))


def test_ac3_no_row(watcher, calls, make_home):
    """003-AC3 : session inconnue de T3."""
    _assert_degraded(watcher, calls, make_home(rows=()))


def test_ac3_null_app_thread_id(watcher, calls, make_home):
    """003-AC3 : appThreadId NULL."""
    _assert_degraded(watcher, calls, make_home(rows=((SESSION, None, '2026'),)))


def test_ac3_wrong_schema(watcher, calls, make_home):
    """003-AC3 : table absente (schéma T3 changé)."""
    _assert_degraded(watcher, calls, make_home(schema=False))


def test_ac3_locked_db(watcher, calls, make_home):
    """003-AC3 : base verrouillée par T3 → timeout court, pas de launch, rien ne fuit.

    En WAL un écrivain ne bloque pas les lecteurs : il faut locking_mode=EXCLUSIVE
    + une écriture pour que le verrou tienne et que le chemin timeout soit exercé.
    """
    home = make_home(live=False)
    holder = sqlite3.connect(home / 'userdata' / 'statev2.sqlite', isolation_level=None)
    # Une lecture AVANT le mode exclusif : -shm est créé sur disque (sinon SQLite
    # le garde en mémoire et le garde -wal/-shm court-circuiterait le timeout).
    holder.execute('SELECT 1 FROM sqlite_master').fetchall()
    holder.execute('PRAGMA locking_mode=EXCLUSIVE')
    holder.execute(f"UPDATE {TABLE} SET updated_at = updated_at")
    assert (home / 'userdata' / 'statev2.sqlite-shm').exists()  # garde franchi : timeout exercé
    # Borne absolue : la spec promet ~200 ms, le clic GTK ne doit jamais geler.
    assert watcher.T3_DB_TIMEOUT <= 0.2
    started = time.monotonic()
    assert watcher.t3_thread_id(home, SESSION) is None
    elapsed = time.monotonic() - started
    assert watcher.T3_DB_TIMEOUT * 0.9 <= elapsed < 1.0  # vraiment bloqué, puis abandonné vite
    try:
        _assert_degraded(watcher, calls, home)
    finally:
        holder.close()


def test_ac3_empty_environment_id(watcher, calls, make_home):
    """003-AC3 : environment-id vide (juste un saut de ligne) → pas de deep link."""
    home = make_home(env_id='')
    assert watcher.t3_deeplink(home, SESSION) is None
    _assert_degraded(watcher, calls, home)


def test_ac3_launch_error_swallowed(watcher, calls, make_home):
    """003-AC3 : aucun handler t3code: (GLib.Error) → erreur avalée, raise conservé."""
    watcher.Gio.AppInfo.launch_default_for_uri.side_effect = RuntimeError('no handler')
    home = make_home()
    assert watcher.focus_terminal(_session(home)) is True
    watcher.Gio.AppInfo.launch_default_for_uri.assert_called_once()
    assert _runs(calls) != []


# ── 003-AC4 ────────────────────────────────────────────────────────────────────

def test_ac4_missing_db_not_created(watcher, tmp_path):
    """003-AC4 : mode=ro ne crée pas le fichier."""
    home = tmp_path / '.t3'
    assert watcher.t3_thread_id(home, SESSION) is None
    assert not (home / 'userdata' / 'statev2.sqlite').exists()
    assert not home.exists()


def test_ac4_db_untouched(watcher, make_home):
    """003-AC4 : T3 lancé (WAL vivant) → base, -wal ET -shm intacts octet pour octet, rien de créé."""
    home = make_home()
    userdata = home / 'userdata'

    def snapshot():
        return {p.name: p.read_bytes() for p in userdata.iterdir()}

    names_before, before = sorted(p.name for p in userdata.iterdir()), snapshot()
    assert watcher.t3_thread_id(home, SESSION) == THREAD
    assert sorted(p.name for p in userdata.iterdir()) == names_before
    assert snapshot() == before


def test_ac4_wal_db_without_writer_creates_nothing(watcher, make_home):
    """003-AC4 : T3 fermé (pas de -wal) → aucune connexion, donc ni -wal ni -shm créés."""
    home = make_home(live=False)
    userdata = home / 'userdata'
    before = sorted(p.name for p in userdata.iterdir())
    assert watcher.t3_thread_id(home, SESSION) is None
    assert watcher.t3_deeplink(home, SESSION) is None
    assert sorted(p.name for p in userdata.iterdir()) == before


def test_ac4_ro_never_creates_db(watcher, tmp_path):
    """003-AC4 : -wal/-shm orphelins sans base → `mode=ro` refuse de créer statev2.sqlite.

    Les deux annexes présentes : le garde « T3 fermé » laisse passer, c'est bien
    `mode=ro` qui protège ici.
    """
    userdata = tmp_path / '.t3' / 'userdata'
    userdata.mkdir(parents=True)
    (userdata / 'statev2.sqlite-wal').write_bytes(b'')
    (userdata / 'statev2.sqlite-shm').write_bytes(b'')
    before = sorted(p.name for p in userdata.iterdir())
    assert watcher.t3_thread_id(tmp_path / '.t3', SESSION) is None
    assert sorted(p.name for p in userdata.iterdir()) == before


def test_ac4_stale_wal_without_shm_creates_nothing(watcher, make_home):
    """003-AC4 : -wal orphelin sans -shm (crash/restauration, T3 fermé) → rien de créé.

    Sans le garde, l'ouverture `mode=ro` recrée -shm sous le home T3.
    """
    home = make_home(live=False)
    userdata = home / 'userdata'
    (userdata / 'statev2.sqlite-wal').write_bytes(b'')
    assert not (userdata / 'statev2.sqlite-shm').exists()
    before = sorted(p.name for p in userdata.iterdir())
    result = watcher.t3_thread_id(home, SESSION)
    # Listing d'abord : c'est l'invariant AC4, il doit être visible même si le résultat change.
    assert sorted(p.name for p in userdata.iterdir()) == before
    assert result is None


# ── 003-AC5 ────────────────────────────────────────────────────────────────────

def test_ac5_scan_does_no_db_io(watcher):
    """003-AC5 : la lecture SQLite n'a lieu qu'au clic, jamais dans le scan périodique."""
    src = inspect.getsource(watcher.scan_local_sessions)
    for forbidden in ('t3_thread_id', 't3_deeplink', 'sqlite3', '_open_t3_thread'):
        assert forbidden not in src


def test_ac5_t3_home_is_pure(watcher, monkeypatch):
    """003-AC5 : t3_home ne touche ni au disque ni à os.environ, seulement au dict reçu."""
    def boom(*a, **k):
        raise AssertionError('I/O in t3_home')
    monkeypatch.setattr(Path, 'exists', boom)
    monkeypatch.setattr(Path, 'read_text', boom)
    monkeypatch.setattr(watcher.os, 'environ', {})
    env = {'CHROME_DESKTOP': watcher.T3_DESKTOP_ID, 'T3CODE_CLI_PATH': '/a/bin/t3'}
    assert watcher.t3_home(env) == Path('/a')


# ── 003-AC6 ────────────────────────────────────────────────────────────────────

def test_ac6_newest_row_wins(watcher, make_home):
    """003-AC6 : deux lignes pour la même session → updated_at le plus récent."""
    home = make_home(rows=(
        (SESSION, 'old-thread', '2026-10-18T10:00:00Z'),
        (SESSION, 'new-thread', '2026-10-19T10:00:00Z'),
        ('other-session', 'unrelated', '2026-10-20T10:00:00Z'),
    ))
    assert watcher.t3_thread_id(home, SESSION) == 'new-thread'
    assert watcher.t3_deeplink(home, SESSION) == f't3code://threads/{ENV_ID}/new-thread'


def test_deeplink_quotes_slash_in_ids(watcher, make_home):
    """003-AC1 : un '/' dans un id ne doit pas créer de segment de route en plus."""
    home = make_home(env_id='a/b')
    assert watcher.t3_deeplink(home, SESSION) == f't3code://threads/a%2Fb/{THREAD}'


# ── câblage du scan ────────────────────────────────────────────────────────────

def test_ac1_scan_tags_t3_rows(watcher, monkeypatch, tmp_path):
    """003-AC1 : scan_local_sessions pose t3_home/session_id sur une ligne T3, None ailleurs."""
    t3_root = tmp_path / 't3home'
    envs = {
        1: {'CHROME_DESKTOP': watcher.T3_DESKTOP_ID, 'T3CODE_CLI_PATH': str(t3_root / 'bin' / 't3')},
        2: {'TERM': 'xterm-kitty'},
    }
    sids = {1: 'sid-t3', 2: 'sid-term'}
    procs = [{'pid': pid, 'starttime': 1, 'start_unix': 0.0, 'elapsed': '1m'} for pid in envs]
    monkeypatch.setattr(watcher, 'get_all_windows', lambda: [])
    monkeypatch.setattr(watcher, 'scan_proc', lambda *a, **k: (procs, {}))
    monkeypatch.setattr(watcher, 'get_cwd', lambda pid: '/tmp')
    monkeypatch.setattr(watcher, 'get_parent_terminal', lambda *a, **k: None)
    monkeypatch.setattr(watcher, 'get_env', lambda pid: envs[pid])
    monkeypatch.setattr(watcher, 'find_best_window', lambda *a, **k: None)
    monkeypatch.setattr(watcher, 'resolve_config_dir', lambda env: None)
    monkeypatch.setattr(watcher, 'get_session_state',
                        lambda pid, *a, **k: ('idle', None, None, None, None, sids[pid], False))
    monkeypatch.setattr(watcher, 'session_agents', lambda *a, **k: [])
    monkeypatch.setattr(watcher, 'worktree_of', lambda cwd, found: (cwd, None))
    rows = {r['pid']: r for r in watcher.scan_local_sessions()}
    assert rows[1]['t3_home'] == str(t3_root)
    assert rows[1]['session_id'] == 'sid-t3'
    assert rows[2]['t3_home'] is None
    assert rows[2]['session_id'] == 'sid-term'
