"""Unit tests for RentalService (isolated from PostgreSQL and HTTP)."""

from unittest.mock import Mock
import pytest

from rental.models import Equipment, RentalItem, RentalOrder, RentalRequest, RentalStatus
from rental.service import RentalRejectedError, RentalService


@pytest.fixture
def unit_deps():
    equipment_repo = Mock()
    rental_repo = Mock()
    policy = Mock()
    policy.is_allowed.return_value = True
    service = RentalService(equipment_repo, rental_repo, policy)
    return service, equipment_repo, rental_repo, policy


def test_calculate_total_returns_expected_amount(unit_deps):
    service, *_ = unit_deps
    assert service.calculate_total(daily_rate=1000, quantity=2, days=5, premium=False) == 10000


def test_calculate_late_fee_applies_tiered_rates_and_caps_at_total(unit_deps):
    service, *_ = unit_deps
    # 3 дні по 10% + 2 дні по 20% = 70% від (1000 * 2) = 1400
    assert service.calculate_late_fee(daily_rate=1000, quantity=2, days=5, overdue_days=5) == 1400
    # Штраф не може перевищувати повну вартість оренди (1000 * 1 * 1 = 1000)
    assert service.calculate_late_fee(daily_rate=1000, quantity=1, days=1, overdue_days=20) == 1000


def test_calculate_total_rejects_out_of_range_values(unit_deps):
    service, *_ = unit_deps
    with pytest.raises(ValueError):
        service.calculate_total(daily_rate=50, quantity=1, days=1)
    with pytest.raises(ValueError):
        service.calculate_total(daily_rate=1000, quantity=0, days=1)
    with pytest.raises(ValueError):
        service.calculate_total(daily_rate=1000, quantity=1, days=31)


def test_rent_succeeds_with_mocked_dependencies(unit_deps):
    service, equipment_repo, rental_repo, policy = unit_deps
    eq = Equipment(id=1, code="CAM-1", daily_rate=1000, stock=5)
    equipment_repo.find_by_id.return_value = eq
    rental_repo.create.side_effect = lambda order: order

    result = service.rent(RentalRequest(reference="R-100", equipment_id=1, quantity=2, days=3))

    assert result.total == 6000
    assert result.status == RentalStatus.ACTIVE
    assert result.item == RentalItem(0, 1, 2, 1000)
    policy.is_allowed.assert_called_once_with(eq, 2, 3)


def test_rent_raises_when_stock_is_insufficient(unit_deps):
    service, equipment_repo, rental_repo, _ = unit_deps
    equipment_repo.find_by_id.return_value = Equipment(id=1, code="CAM-1", daily_rate=1000, stock=1)

    with pytest.raises(RentalRejectedError):
        service.rent(RentalRequest(reference="R-101", equipment_id=1, quantity=2, days=3))

    rental_repo.create.assert_not_called()


def test_rent_raises_when_policy_denies(unit_deps):
    service, equipment_repo, rental_repo, policy = unit_deps
    equipment_repo.find_by_id.return_value = Equipment(id=1, code="CAM-1", daily_rate=1000, stock=5)
    policy.is_allowed.return_value = False

    with pytest.raises(RentalRejectedError):
        service.rent(RentalRequest(reference="R-102", equipment_id=1, quantity=1, days=2))

    rental_repo.create.assert_not_called()


def test_return_rental_marks_order_returned_with_late_fee(unit_deps):
    service, _, rental_repo, _ = unit_deps
    order = RentalOrder(
        id=10,
        reference="R-103",
        days=5,
        premium=False,
        total=10000,
        late_fee=0,
        status=RentalStatus.ACTIVE,
        item=RentalItem(id=1, equipment_id=1, quantity=2, daily_rate=1000),
    )
    rental_repo.find_by_id.return_value = order

    returned = service.return_rental(rental_id=10, overdue_days=2)

    assert returned.status == RentalStatus.RETURNED
    assert returned.late_fee == 400
    rental_repo.mark_returned.assert_called_once_with(10, 400)
