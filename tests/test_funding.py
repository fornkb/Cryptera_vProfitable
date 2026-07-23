"""B27.0: fetch_funding infers the funding interval from fundingTime deltas and
windows the 24h average by hours, instead of assuming 8h (last-3-rows)."""

import main


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _payload(interval_hours, rates, base=1_700_000_000_000):
    step = int(interval_hours * 3600 * 1000)
    return [{"fundingTime": base + i * step, "fundingRate": str(rates[i])}
            for i in range(len(rates))]


def test_funding_infers_4h_interval_and_windows_by_hours(monkeypatch):
    rates = [round(0.001 * i, 6) for i in range(10)]
    monkeypatch.setattr(main.requests, "get",
                        lambda *a, **k: _FakeResp(_payload(4, rates)))
    out = main.fetch_funding("BTC/USDT")
    assert out["interval_hours"] == 4.0
    # 24h / 4h = 6 samples averaged, NOT the fixed last 3
    assert out["avg_24h"] == round(sum(rates[-6:]) / 6, 6)


def test_funding_infers_8h_interval_keeps_three_sample_window(monkeypatch):
    rates = [round(0.001 * i, 6) for i in range(10)]
    monkeypatch.setattr(main.requests, "get",
                        lambda *a, **k: _FakeResp(_payload(8, rates)))
    out = main.fetch_funding("BTC/USDT")
    assert out["interval_hours"] == 8.0
    assert out["avg_24h"] == round(sum(rates[-3:]) / 3, 6)   # 24h / 8h = 3 (unchanged)
