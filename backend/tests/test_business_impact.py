"""Business impact formula."""
import pytest

from app.services.business_impact import compute_impact


def test_impact_formula():
    delta, impact = compute_impact(
        affected_requests=1000,
        incident_error_rate=0.12,
        baseline_error_rate=0.02,
        revenue_per_request=0.50,
    )
    assert delta == pytest.approx(0.10)
    assert impact == pytest.approx(50.0)  # 1000 * 0.10 * 0.50


def test_negative_delta_clamped_to_zero():
    delta, impact = compute_impact(1000, 0.01, 0.05, 0.50)
    assert delta == 0.0
    assert impact == 0.0
