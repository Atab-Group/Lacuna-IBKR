"""Regression coverage; no real gateway connection or login."""
from types import SimpleNamespace
from unittest.mock import Mock
import ib_async
import ibclient
import gateway
import webui


def test_market_data_connect_skips_account_order_sync(monkeypatch):
    ib = Mock()
    monkeypatch.setattr(ib_async, 'IB', lambda: ib)
    with ibclient.Client(client_id=209) as client:
        assert client.ib is ib
        ib.client.connect.assert_called_once_with('127.0.0.1', 4001, clientId=209, timeout=20)
        ib.connect.assert_not_called()
    ib.disconnect.assert_called_once()


def test_login_probe_skips_account_order_sync(monkeypatch):
    ib = Mock()
    ib.managedAccounts.return_value = ['test-account']
    monkeypatch.setattr(ib_async, 'IB', lambda: ib)
    monkeypatch.setattr(gateway, 'port_open', lambda: True)
    assert gateway.api_session() == ['test-account']
    ib.client.connect.assert_called_once()
    ib.connect.assert_not_called()
    ib.disconnect.assert_called_once()


def test_failed_login_stops_container(monkeypatch):
    stop = Mock(return_value='')
    monkeypatch.setattr(webui.gw, 'compose', stop)
    monkeypatch.setattr(webui.gw, 'throttle_age', lambda: 0)
    webui.login_worker()
    assert webui._state['phase'] == 'failed'
    stop.assert_called_once_with('stop')


def test_login_exception_stops_container(monkeypatch):
    stop = Mock(return_value='')
    monkeypatch.setattr(webui.gw, 'compose', stop)
    monkeypatch.setattr(webui.gw, 'throttle_age', Mock(side_effect=RuntimeError('test')))
    webui.login_worker()
    assert webui._state['phase'] == 'failed'
    stop.assert_called_once_with('stop')


def test_missing_code_stops_container(monkeypatch):
    compose = Mock(return_value='')
    monkeypatch.setattr(webui.gw, 'compose', compose)
    monkeypatch.setattr(webui.gw, 'throttle_age', lambda: None)
    monkeypatch.setattr(webui.gw, 'logs', lambda *a, **kw: webui.gw.DIALOG)
    monkeypatch.setattr(webui.gw, 'env_value', lambda k: None)
    monkeypatch.setattr(webui.time, 'sleep', lambda n: None)
    monkeypatch.setattr(webui, '_code_event', SimpleNamespace(clear=lambda: None, wait=lambda **kw: False))
    webui.login_worker()
    assert webui._state['phase'] == 'failed'
    assert compose.call_args.args == ('stop',)
