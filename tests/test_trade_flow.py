"""Trade-tape taker-flow filter: imbalance sign, direction-aware veto, freshness + thin-tape safety."""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.trade_flow import evaluate

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def _t(side, size, age_s=10.0):
    return {"side": side, "size": size, "createdAt": (NOW - timedelta(seconds=age_s)).isoformat()}


def test_imbalance_sign_and_neutral():
    v = evaluate([_t("YES", 300), _t("NO", 300)], long_yes=True, now=NOW)
    assert v.imbalance == 0.0 and v.reason == "neutral" and not v.contradicts


def test_long_yes_contradicted_by_no_aggression():
    # heavy NO buying (selling YES) while we want to BUY YES -> contradicted
    v = evaluate([_t("NO", 900), _t("YES", 100)], long_yes=True, imbalance_veto=0.6, now=NOW)
    assert v.imbalance == -0.8 and v.contradicts and v.reason == "contradicted"


def test_buy_no_contradicted_by_yes_aggression():
    # heavy YES buying (someone aggressively buying the longshot) while we want to FADE (buy NO)
    v = evaluate([_t("YES", 900), _t("NO", 100)], long_yes=False, imbalance_veto=0.6, now=NOW)
    assert v.imbalance == 0.8 and v.contradicts and v.reason == "contradicted"


def test_confirming_flow_does_not_veto():
    # YES aggression while we are long YES -> confirming, proceed
    v = evaluate([_t("YES", 900), _t("NO", 100)], long_yes=True, imbalance_veto=0.6, now=NOW)
    assert not v.contradicts and v.reason == "confirming"


def test_thin_tape_never_vetoes():
    v = evaluate([_t("NO", 10)], long_yes=True, min_volume=100, now=NOW)
    assert v.reason == "insufficient_flow" and not v.contradicts and v.imbalance is None


def test_stale_trades_excluded():
    # all NO aggression but OLD (beyond window) -> ignored -> thin -> no veto
    v = evaluate([_t("NO", 900, age_s=5000.0)], long_yes=True, max_age_s=1800.0, min_volume=100, now=NOW)
    assert v.reason == "insufficient_flow" and v.total_vol == 0


def test_malformed_trades_skipped():
    trades = [_t("YES", 500), {"side": "YES"}, {"size": 100}, {"side": "MAYBE", "size": 100,
              "createdAt": NOW.isoformat()}, _t("NO", 100)]
    v = evaluate(trades, long_yes=True, min_volume=100, now=NOW)
    assert v.yes_vol == 500 and v.no_vol == 100 and v.n_trades == 2
