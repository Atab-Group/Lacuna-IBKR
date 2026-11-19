import sys
from pathlib import Path

import pytest

# the service modules import each other flat (import bars, import store), the
# the same way the service does at runtime, so the service directory goes on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PRICE_ALERT_ENV = ("LACUNA_PRICE_ALERTS_DIR", "LACUNA_PRICE_ALERTS_RULES",
                    "LACUNA_PRICE_ALERTS_BACKTEST", "LACUNA_PRICE_ALERTS_PYTHON",
                    "LACUNA_PRICE_ALERTS_STDIO_WRITE")


@pytest.fixture(autouse=True)
def _no_price_alert_env(monkeypatch):
    """Every test starts with the optional price-alert tools switched off.

    A shell that exports these for a real engine would otherwise change the
    tool list every other test asserts on. A test that wants them sets them.
    """
    for name in _PRICE_ALERT_ENV:
        monkeypatch.delenv(name, raising=False)
