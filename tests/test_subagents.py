"""Sous-agents in-process (outil Agent). Fichier IDENTIQUE dans les deux clients.

Ils n'ont pas de process : seuls `<session>/subagents/agent-<id>.jsonl` et son
`.meta.json` les trahissent. Sans eux, une session qui a lancé un agent en fond
lisait `idle` et n'affichait aucun agent.
"""

import json
import os

SID = 'sess-1'
CWD = '/tmp/proj'


def _ev(kind: str, **kw) -> str:
    return json.dumps({'type': kind, **kw})


def _assistant(stop_reason, *blocks) -> str:
    return _ev('assistant', message={'stop_reason': stop_reason, 'content': list(blocks)})


def _result(tool_use_id: str, success: bool) -> str:
    return _ev('user', message={'content': [{'type': 'tool_result', 'tool_use_id': tool_use_id}]},
               toolUseResult={'success': success, 'message': 'x'})


HANDBACK = {'type': 'tool_use', 'id': 'tu-1', 'name': 'SubagentHandback'}


def test_a_terminal_stop_reason_means_finished(watcher):
    assert watcher._agent_running([_assistant('end_turn')]) is False


def test_a_pending_tool_means_running(watcher):
    assert watcher._agent_running([_assistant('tool_use')]) is True
    assert watcher._agent_running([_assistant('tool_use'), _result('tu-9', True)]) is True


def test_a_delivered_handback_means_finished(watcher):
    lines = [_assistant('tool_use', HANDBACK), _result('tu-1', True),
             _ev('attachment', attachment={})]
    assert watcher._agent_running(lines) is False


def test_a_refused_handback_means_still_running(watcher):
    """`success: false` : le rapport n'est pas parti, l'agent continue."""
    assert watcher._agent_running([_assistant('tool_use', HANDBACK), _result('tu-1', False)]) is True


def test_a_message_after_the_handback_means_resumed(watcher):
    """Une de ses tâches de fond le réveille : il retravaille."""
    lines = [_assistant('tool_use', HANDBACK), _result('tu-1', True),
             _ev('user', message={'content': 'notification'})]
    assert watcher._agent_running(lines) is True


def test_an_interruption_means_finished(watcher):
    """TaskStop : Claude écrit le marqueur d'interruption, rien d'autre."""
    lines = [_assistant('tool_use'), _result('tu-9', False),
             _ev('user', message={'content': [{'type': 'text',
                                               'text': '[Request interrupted by user for tool use]'}]})]
    assert watcher._agent_running(lines) is False


def test_no_message_is_unknown(watcher):
    assert watcher._agent_running([_ev('attachment', attachment={}), '', '{broken']) is None


def _session(tmp_path, agents: dict[str, tuple[list[str], dict | None]]):
    proj = tmp_path / 'projects' / '-tmp-proj'
    sub = proj / SID / 'subagents'
    sub.mkdir(parents=True)
    (proj / f'{SID}.jsonl').write_text(_assistant('end_turn') + '\n')
    for aid, (lines, meta) in agents.items():
        (sub / f'agent-{aid}.jsonl').write_text('\n'.join(lines) + '\n')
        if meta is not None:
            (sub / f'agent-{aid}.meta.json').write_text(json.dumps(meta))
    return sub


def test_only_running_agents_are_listed_with_their_meta(watcher, tmp_path):
    _session(tmp_path, {
        'a1': ([_assistant('tool_use')], {'description': 'Review', 'agentType': 'Explore',
                                          'model': 'claude-haiku-5-5'}),
        'a2': ([_assistant('end_turn')], {'description': 'Done one'}),
        'a3': ([_assistant(None)], None),
    })
    agents = watcher.session_agents(SID, CWD, str(tmp_path), since=0)
    assert agents == [
        {'pid': None, 'name': 'Review', 'type': 'Explore', 'model': 'haiku-5-5'},
        {'pid': None, 'name': 'a3', 'type': None, 'model': None},
    ]


def test_agents_older_than_the_process_are_ignored(watcher, tmp_path):
    """Vie antérieure de la session (`claude -r`) : tué sans fin écrite."""
    sub = _session(tmp_path, {'old': ([_assistant('tool_use')], None)})
    os.utime(sub / 'agent-old.jsonl', (1000, 1000))
    assert watcher.session_agents(SID, CWD, str(tmp_path), since=2000) == []


def test_no_session_id_or_no_subagents_dir(watcher, tmp_path):
    assert watcher.session_agents(None, CWD, str(tmp_path), since=0) == []
    _session(tmp_path, {})
    assert watcher.session_agents(SID, CWD, str(tmp_path), since=0) == []


PID, STARTTIME = 4321, 7


def _registry(tmp_path, status: str):
    (tmp_path / 'sessions').mkdir()
    (tmp_path / 'sessions' / f'{PID}.json').write_text(json.dumps(
        {'procStart': STARTTIME, 'sessionId': SID, 'cwd': CWD, 'status': status}))


def _state(watcher, tmp_path, since=0):
    return watcher.get_session_state(PID, CWD, STARTTIME, config_dir=str(tmp_path), since=since)[0]


def test_a_running_agent_keeps_a_busy_session_working(watcher, tmp_path):
    """Mesuré : agent de fond lancé, le registre reste 'busy' et le JSONL finit
    sur end_turn → sans l'agent, réconcilié en fin de tour."""
    _session(tmp_path, {'a1': ([_assistant('tool_use')], None)})
    _registry(tmp_path, 'busy')
    assert _state(watcher, tmp_path) == 'working'
    assert _state(watcher, tmp_path, since=None) == 'waiting'


def test_a_running_agent_makes_an_idle_session_working(watcher, tmp_path):
    _session(tmp_path, {'a1': ([_assistant('tool_use')], None)})
    _registry(tmp_path, 'idle')
    assert _state(watcher, tmp_path) == 'working'


def test_a_registry_waiting_outranks_a_running_agent(watcher, tmp_path):
    """Une permission en attente exige l'utilisateur, agent ou pas."""
    _session(tmp_path, {'a1': ([_assistant('tool_use')], None)})
    _registry(tmp_path, 'waiting')
    assert _state(watcher, tmp_path) == 'waiting'


def test_a_finished_agent_leaves_the_state_alone(watcher, tmp_path):
    _session(tmp_path, {'a1': ([_assistant('end_turn')], None)})
    _registry(tmp_path, 'busy')
    assert _state(watcher, tmp_path) == 'waiting'
