"""HTTP lifecycle regressions: in-memory ASGI and uvicorn stubs, no TCP ports."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import uvicorn

from chitta_bridge import server as bridge


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setenv('CHITTA_BRIDGE_SHUTDOWN_GRACE_S', '0.15')
    lifecycle = bridge._HTTPLifecycle()
    monkeypatch.setattr(bridge, '_peer_workers', {})
    monkeypatch.setattr(bridge, '_BG_TASKS', set())
    monkeypatch.setattr(bridge, '_bg_rooms', {})
    monkeypatch.setattr(bridge.rooms, '_bg_tasks', {})
    monkeypatch.setattr(bridge.rooms, '_room_locks', {})
    yield lifecycle
    lifecycle.close()


def test_health_ready_and_auth(state):
    async def run():
        app = bridge._make_http_app(state, 'test-secret')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            health = await client.get('/health')
            assert health.status_code == 200
            data = health.json()
            assert set(data) == {'status', 'version', 'uptime_s', 'mcp_sessions',
                                 'rooms_active', 'scheduler', 'shutting_down'}
            assert data == dict(status='ok', version=bridge.__version__, uptime_s=data['uptime_s'],
                                mcp_sessions=0, rooms_active=0, scheduler='disabled', shutting_down=False)
            assert data['uptime_s'] >= 0
            assert (await client.get('/ready')).status_code == 503
            async with app.app.router.lifespan_context(app.app):
                assert state.mcp_up
                assert (await client.get('/ready')).status_code == 503
                state.dashboard_up = True
                state.userver = SimpleNamespace(started=False)
                assert (await client.get('/ready')).status_code == 503
                state.userver.started = True
                state.scheduler = SimpleNamespace(_active=True)
                state.mcp_requests.add(object())
                lock = asyncio.Lock()
                await lock.acquire()
                bridge.rooms._room_locks['r'] = lock
                ready = await client.get('/ready')
                assert ready.status_code == 200
                assert ready.json()['scheduler'] == 'running'
                assert ready.json()['mcp_sessions'] == ready.json()['rooms_active'] == 1
                for path in ('/mcp', '/sse', '/messages/'):
                    assert (await client.get(path)).status_code == 401
                    assert (await client.get(path, headers={'Authorization': 'Bearer bad'})).status_code == 401
                state.shutting_down = True
                for path in ('/health', '/ready'):
                    response = await client.get(path)
                    assert response.status_code == 503
                    assert response.json()['status'] == 'shutting_down'
                    assert response.json()['shutting_down'] is True
                assert (await client.post('/mcp')).status_code == 503
                state.shutting_down = False
            assert not state.mcp_up
    asyncio.run(run())


@pytest.mark.parametrize('path', ['/sse', '/mcp'])
def test_open_stream_shutdown_with_uvicorn(state, path):
    async def run():
        connected, disconnected, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stream(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream; charset=utf-8')]})
            connected.set()
            try:
                await asyncio.Event().wait()  # a client that never disconnects
            finally:
                disconnected.set()

        async def stop_scheduler():
            stopped.set()
        state.scheduler = SimpleNamespace(stop=stop_scheduler)
        config = uvicorn.Config(bridge._HTTPStreams(stream, state), timeout_graceful_shutdown=state.grace)
        userver = uvicorn.Server(config)
        userver.servers = [SimpleNamespace(close=Mock(), wait_closed=AsyncMock())]
        userver.lifespan = SimpleNamespace(shutdown=AsyncMock())
        state.userver = userver
        send = AsyncMock()
        task = asyncio.create_task(config.app({'type': 'http', 'path': path}, AsyncMock(), send))
        userver.server_state.tasks.add(task)
        task.add_done_callback(userver.server_state.tasks.discard)
        await connected.wait()
        assert len(state.mcp_requests) == len(state.streams) == 1
        start = asyncio.get_running_loop().time()
        state.begin_shutdown()
        await userver.shutdown()
        await state.finish()
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < state.grace
        assert task.done() and task.exception() is None
        assert disconnected.is_set() and stopped.is_set()
        assert send.call_args.args[0] == {'type': 'http.response.body', 'body': b'', 'more_body': False}
        assert not state.streams and not state.mcp_requests
        assert userver.should_exit
        userver.servers[0].close.assert_called()
        userver.lifespan.shutdown.assert_awaited_once()
    asyncio.run(run())


def test_inflight_request_gets_grace_then_cancelled(state):
    async def run():
        userver = uvicorn.Server(uvicorn.Config(Mock(), timeout_graceful_shutdown=state.grace))
        userver.servers = []
        userver.lifespan = SimpleNamespace(shutdown=AsyncMock())
        state.userver = userver
        task = asyncio.create_task(asyncio.Event().wait())
        userver.server_state.tasks.add(task)
        state.begin_shutdown()
        if hasattr(task, "cancelling"):  # Task.cancelling() is 3.11+
            assert not task.cancelling()
        assert not task.done()
        await userver.shutdown()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        await state.finish()
    asyncio.run(run())


def test_second_signal_exits_zero_and_cleans_discovery(state, monkeypatch, tmp_path):
    async def run():
        state.port_file = tmp_path / 'ports'
        state.port_file.write_text(f'pid={bridge.os.getpid()}\n')
        state.peer = SimpleNamespace(_remove_registry=Mock(), stop=AsyncMock())
        timer = Mock()
        monkeypatch.setattr(bridge._threading, 'Timer', Mock(return_value=timer))
        exit_process = Mock()
        monkeypatch.setattr(bridge.os, '_exit', exit_process)
        state.on_signal()
        assert state.shutting_down
        timer.start.assert_called_once()
        exit_process.assert_not_called()
        state.on_signal()
        exit_process.assert_called_once_with(0)
        assert not state.port_file.exists()
        state.peer._remove_registry.assert_called_once()
        await state.finish()
    asyncio.run(run())


def test_cleanup_preserves_successor_port_file(state, tmp_path):
    state.port_file = tmp_path / 'ports'
    state.port_file.write_text('pid=-1\n')
    state.cleanup()
    assert state.port_file.exists()


@pytest.mark.parametrize('argv,expected', [
    (['/env/bin/python3', '-m', 'chitta_bridge.server', '--http'], True),
    (['python3.11', '-u', '/env/bin/chitta-bridge', '--http'], True),
    (['/env/bin/chitta-bridge', '--http'], True),
    (['python3', '/repo/chitta_bridge/server.py'], True),
    (['python3', '-c', 'print("chitta_bridge")', '-m', 'chitta_bridge.server'], False),
    (['python3', 'other.py', 'chitta-bridge'], False),
    (['bash', '-c', 'chitta-bridge --http'], False),
    (['/repo/chitta-bridge/tools/other'], False),
    (['python3', '-m', 'chitta_bridge.peer_worker'], False),
])
def test_port_owner_entrypoints(argv, expected):
    assert bridge._bridge_cmdline(argv) is expected


def test_evict_never_signals_unrelated_owner(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=SimpleNamespace(stdout='123 456')))
    monkeypatch.setattr(bridge.Path, 'read_text', lambda path: (
        'python3\0-m\0chitta_bridge.server\0--http\0' if '123' in str(path)
        else 'python3\0other.py\0chitta-bridge\0'))
    kill = Mock()
    monkeypatch.setattr(bridge.os, 'kill', kill)
    assert bridge._evict_port(17681, allow_http=False)
    kill.assert_called_once_with(123, bridge._signal.SIGTERM)
    kill.reset_mock()
    assert not bridge._evict_port(17681)
    kill.assert_not_called()


def test_port_busy_deadline_message(monkeypatch):
    ticks = iter([100, 109, 110])
    monkeypatch.setattr(bridge, '_time', SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(bridge, '_port_busy', lambda port: True)
    evict = Mock(return_value=False)
    monkeypatch.setattr(bridge, '_evict_port', evict)
    monkeypatch.setattr(bridge.asyncio, 'sleep', AsyncMock())
    with pytest.raises(RuntimeError, match='MCP port 17681 still busy after 10 s'):
        asyncio.run(bridge._wait_for_port(17681, 'MCP'))
    evict.assert_called_once_with(17681, allow_http=False)


def test_port_released_after_evict(monkeypatch):
    monkeypatch.setattr(bridge, '_port_busy', Mock(side_effect=[True, True, False]))
    monkeypatch.setattr(bridge, '_evict_port', Mock(return_value=True))
    monkeypatch.setattr(bridge.asyncio, 'sleep', AsyncMock())
    asyncio.run(bridge._wait_for_port(17681, 'MCP'))


@pytest.mark.parametrize('value', ['-1', 'nan', 'inf', 'bad'])
def test_invalid_grace(monkeypatch, value):
    monkeypatch.setenv('CHITTA_BRIDGE_SHUTDOWN_GRACE_S', value)
    with pytest.raises(ValueError):
        bridge._HTTPLifecycle()


def test_startup_failure_cleans_up(state, monkeypatch, tmp_path):
    state.port_file = tmp_path / 'ports'
    state.port_file.write_text(f'pid={bridge.os.getpid()}\n')
    monkeypatch.setattr(bridge, '_wait_for_port', AsyncMock(side_effect=RuntimeError('port busy')))
    with pytest.raises(RuntimeError, match='port busy'):
        asyncio.run(bridge._run_http_mode(lifecycle=state))
    assert not state.port_file.exists()


def test_watchdog_covers_stuck_shutdown(state, monkeypatch):
    async def run():
        monkeypatch.setattr(bridge._threading, 'Timer', Mock())
        monkeypatch.setattr(bridge.os, '_exit', Mock())
        state.on_signal()
        delay, callback = bridge._threading.Timer.call_args.args
        assert delay == state.grace + 1
        callback()
        bridge.os._exit.assert_called_once_with(0)
        await state.finish()
    asyncio.run(run())


def test_signal_during_startup_exits_cleanly(state, monkeypatch):
    async def wait_for_signal(*args):
        asyncio.get_running_loop().call_soon(state.on_signal)
        await asyncio.Event().wait()
    monkeypatch.setattr(bridge, '_wait_for_port', wait_for_signal)
    asyncio.run(bridge._run_http_mode(lifecycle=state))
    assert state.signalled and state.shutting_down


def test_background_rooms_and_peers_notified(state, monkeypatch):
    async def run():
        room = asyncio.create_task(asyncio.Event().wait())
        bridge._BG_TASKS.add(room)
        worker = SimpleNamespace(terminate=Mock())
        bridge._peer_workers['test'] = worker
        process = SimpleNamespace(returncode=None, terminate=Mock())
        bridge.rooms._bg_tasks['test'] = {'proc': process}
        state.peer = SimpleNamespace(stop=AsyncMock(), _remove_registry=Mock())
        state.begin_shutdown()
        await state.finish()
        await asyncio.gather(room, return_exceptions=True)
        assert room.cancelled()
        worker.terminate.assert_called_once()
        process.terminate.assert_called_once()
        state.peer.stop.assert_awaited_once()
        state.peer._remove_registry.assert_called_once()
    asyncio.run(run())


def test_runtime_version_matches_release():
    assert bridge.__version__ == '0.41.0'
    assert bridge._make_init_options().server_version == '0.41.0'
