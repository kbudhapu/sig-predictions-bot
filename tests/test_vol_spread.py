"""Vol-aware MM half-spread core (shadow research input)."""
from strategies.market_making.vol_spread import VolSpreadConfig, realized_vol, vol_scaled_k


def test_realized_vol_too_few_samples_is_zero():
    assert realized_vol([]) == 0.0
    assert realized_vol([0.5]) == 0.0
    assert realized_vol([0.5, 0.51]) == 0.0


def test_realized_vol_flat_series_is_zero():
    assert realized_vol([0.5, 0.5, 0.5, 0.5]) == 0.0


def test_realized_vol_is_stdev_of_diffs():
    # diffs = [+0.01, -0.01, +0.01]; mean 1/3*0.01, population stdev computed directly
    v = realized_vol([0.50, 0.51, 0.50, 0.51])
    assert v > 0.0
    # constant-magnitude alternation -> vol near the step size
    assert 0.005 < v < 0.02


def test_vol_scaled_k_at_reference_equals_base():
    cfg = VolSpreadConfig(base_k=0.015, vol_ref=0.01, k_min=0.005, k_max=0.04)
    assert abs(vol_scaled_k(0.01, cfg) - 0.015) < 1e-9


def test_vol_scaled_k_widens_when_volatile_and_tightens_when_calm():
    cfg = VolSpreadConfig(base_k=0.015, vol_ref=0.01, k_min=0.005, k_max=0.04)
    assert vol_scaled_k(0.02, cfg) > 0.015    # double vol -> wider
    assert vol_scaled_k(0.004, cfg) < 0.015   # quiet -> tighter


def test_vol_scaled_k_clamps_to_bounds():
    cfg = VolSpreadConfig(base_k=0.015, vol_ref=0.01, k_min=0.005, k_max=0.04)
    assert vol_scaled_k(10.0, cfg) == 0.04     # huge vol -> k_max
    assert vol_scaled_k(1e-6, cfg) == 0.005    # ~0 vol -> k_min


def test_vol_scaled_k_zero_vol_returns_base():
    cfg = VolSpreadConfig(base_k=0.015, vol_ref=0.01)
    assert vol_scaled_k(0.0, cfg) == 0.015
