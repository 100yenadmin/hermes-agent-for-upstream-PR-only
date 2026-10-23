"""Regression for #143587: gateway /compress must not rewrite a plugin engine's history."""
from types import SimpleNamespace

import pytest

from agent.context_engine import ContextEngine
from gateway.session_authority import SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_local import create_local_session
from hermes_cli.gateway_mutations import slash_mutation
import hermes_state_runtime as rt


class SentinelEngine(ContextEngine):
    name = 'sentinel'

    def __init__(self):
        self.calls = []

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, **kwargs):
        self.calls.append('compress')
        raise AssertionError('plugin compression cannot be rolled back by the gateway')

    def on_session_start(self, session_id, **kwargs):
        self.calls.append('notify')


@pytest.mark.asyncio
@pytest.mark.parametrize('resident', [False, True])
@pytest.mark.parametrize('args', ['', 'here 2'])
async def test_plugin_compress_refuses_before_side_effects(tmp_path, monkeypatch, resident, args):
    from agent.context_compressor import ContextCompressor
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    # The session's frozen policy, including a cold session without a resident engine, wins
    # over a later profile edit. This is the same mutation path used by default gateway ACP.
    config = {'context': {'engine': 'sentinel'}}
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: config)
    engine, side_effects = SentinelEngine(), []

    def native_compress(self, messages, **kwargs):
        side_effects.append('native')
        raise AssertionError('native compression must never replace the configured engine')

    def memory_hook(*args, **kwargs):
        side_effects.append('memory')
        return ''

    monkeypatch.setattr(ContextCompressor, '__init__', lambda self, *a, **k: None)
    monkeypatch.setattr(ContextCompressor, 'compress', native_compress)
    agent = SimpleNamespace(context_compressor=engine,
                            _memory_manager=SimpleNamespace(on_pre_compress=memory_hook))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
        _evict_cached_agent=lambda route: side_effects.append('evict'),
        _cached_agent_for=lambda route: agent if resident else None,
        _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    authority = SessionAuthority(runner, profile_id='owned', instance_id='owner', db=db,
                                 epoch=rt.begin_runtime_epoch(db, instance_id='owner'))
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    ref = create_local_session(authority, owner.actor, dict(request_id='engine', source='acp',
        cwd=str(tmp_path), model='frozen', toolsets=[]))
    config = {'context': {'engine': 'compressor'}}
    for i in range(4):
        db.append_message(ref.session_id, 'user', f'question {i}')
        db.append_message(ref.session_id, 'assistant', f'answer {i}')
    before = db.get_session(ref.session_id)
    history = db.get_messages(ref.session_id)
    stream = authority.sessions[ref.session_id].event_stream
    watermark = stream.watermark()

    async def compress(raw, request_id):
        operation, payload = slash_mutation('/compress', raw)
        return await owner.dispatch({'id': request_id, 'method': 'session.mutate', 'params': {
            'session_id': ref.session_id, 'request_id': request_id,
            'expected_revision': before['runtime_revision'],
            'expected_generation': before['runtime_generation'], 'operation': operation, 'payload': payload}})

    try:
        preview = await compress((args + ' --preview').strip(), 'preview')
        assert preview.get('result', {}).get('status') == 'preview', preview
        for request_id in ('compress', 'retry'):
            reply = await compress(args, request_id)
            result = reply.get('result', {})
            assert result.get('status') == 'noop', reply
            assert 'Manual /compress is unavailable for the configured context engine' in result['summary']['headline']
            assert 'History is unchanged' in result['summary']['note']
        assert side_effects == []
        assert engine.calls == []
        after = db.get_session(ref.session_id)
        assert (after['runtime_revision'], after['runtime_generation']) == (
            before['runtime_revision'], before['runtime_generation'])
        assert db.get_messages(ref.session_id) == history
        assert not db.list_meta_prefix('gateway.mutation.v1.')
        assert stream.since(*watermark)['events'] == []
    finally:
        await owner.close()
        store.close_all_db_handles()
