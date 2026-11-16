"""Service -> Repository -> Database.

RentalService і Postgres-репозиторії — реальні; RentalPolicy — Mock (зовнішня залежність).
Результат кожної операції перевіряється фактичним станом БД через Repository.
"""
import psycopg
import pytest

from rental.models import Equipment, RentalRequest, RentalStatus
from rental.service import RentalRejectedError


# ---------- rent: основні сценарії ----------

def test_rent_success_creates_active_rental_serviceState(
        service, rental_repo, equipment_repo, make_equipment, policy):
    eq = make_equipment(daily_rate=1000, stock=5)

    result = service.rent(RentalRequest("R-1", eq.id, quantity=2, days=5, premium=True))

    stored = rental_repo.find_by_id(result.id)
    assert stored == result
    assert stored.reference == "R-1"
    assert stored.status == RentalStatus.ACTIVE
    assert stored.premium is True               # преміальність зберігається
    assert stored.total == 1000 * 2 * 5         # знижки у варіанті немає
    assert stored.late_fee == 0
    assert (stored.item.equipment_id, stored.item.quantity, stored.item.daily_rate) == (eq.id, 2, 1000)
    assert equipment_repo.find_by_id(eq.id).stock == 5   # залишок не змінюється
    policy.is_allowed.assert_called_once_with(eq, 2, 5)  # взаємодія із Mock


def test_rent_rejected_by_external_policy_creates_nothing_serviceState(
        service, make_equipment, equipment_repo, policy, count_rows):
    eq = make_equipment()
    policy.is_allowed.return_value = False

    with pytest.raises(RentalRejectedError):
        service.rent(RentalRequest("R-1", eq.id, 1, 1))

    policy.is_allowed.assert_called_once()
    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0
    assert equipment_repo.find_by_id(eq.id) == eq


def test_rent_insufficient_stock_rejected_before_policy(
        service, make_equipment, policy, count_rows):
    eq = make_equipment(stock=2)

    with pytest.raises(RentalRejectedError):
        service.rent(RentalRequest("R-1", eq.id, quantity=3, days=1))

    policy.is_allowed.assert_not_called()
    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0


def test_rent_quantity_equal_to_stock_boundary_is_allowed(
        service, rental_repo, equipment_repo, make_equipment):
    eq = make_equipment(daily_rate=100, stock=20)

    result = service.rent(RentalRequest("R-MAX", eq.id, quantity=20, days=30))

    stored = rental_repo.find_by_id(result.id)
    assert stored.total == 100 * 20 * 30
    assert equipment_repo.find_by_id(eq.id).stock == 20


def test_rent_unknown_equipment_raises_value_error(service, policy, count_rows):
    with pytest.raises(ValueError):
        service.rent(RentalRequest("R-1", 4040, 1, 1))

    policy.is_allowed.assert_not_called()
    assert count_rows("rentals") == 0


@pytest.mark.parametrize("quantity, days", [(0, 1), (21, 1), (1, 0), (1, 31)])
def test_rent_invalid_request_creates_nothing(service, make_equipment, policy, count_rows, quantity, days):
    eq = make_equipment(stock=50)

    with pytest.raises(ValueError):
        service.rent(RentalRequest("R-1", eq.id, quantity, days))

    policy.is_allowed.assert_not_called()
    assert count_rows("rentals") == 0


def test_rent_policy_exception_is_propagated_and_nothing_saved(
        service, make_equipment, policy, count_rows):
    eq = make_equipment()
    policy.is_allowed.side_effect = TimeoutError("policy down")

    with pytest.raises(TimeoutError):
        service.rent(RentalRequest("R-1", eq.id, 1, 1))

    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0


def test_rent_policy_non_bool_answer_raises_value_error(
        service, make_equipment, policy, count_rows):
    eq = make_equipment()
    policy.is_allowed.return_value = "yes"

    with pytest.raises(ValueError):
        service.rent(RentalRequest("R-1", eq.id, 1, 1))

    assert count_rows("rentals") == 0


def test_rent_duplicate_reference_keeps_single_rental(
        service, make_equipment, rental_repo, count_rows):
    eq = make_equipment()
    first = service.rent(RentalRequest("DUP", eq.id, 1, 1))

    with pytest.raises(psycopg.errors.UniqueViolation):
        service.rent(RentalRequest("DUP", eq.id, 2, 2))

    assert count_rows("rentals") == 1
    assert count_rows("rental_items") == 1
    assert rental_repo.find_by_id(first.id) == first


def test_rent_snapshots_daily_rate_when_equipment_changes_later(
        service, rental_repo, equipment_repo, make_equipment):
    eq = make_equipment(daily_rate=1000)
    rented = service.rent(RentalRequest("R-1", eq.id, 2, 5))

    equipment_repo.update(Equipment(eq.id, eq.code, 9000, eq.stock))

    stored = rental_repo.find_by_id(rented.id)
    assert stored.item.daily_rate == 1000
    assert stored.total == 10000
    # повернення рахується за збереженим знімком ціни, а не за новою ціною обладнання
    service.return_rental(rented.id, overdue_days=1)
    assert rental_repo.find_by_id(rented.id).late_fee == 1000 * 2 * 10 // 100


# ---------- return_rental: персональне правило варіанта 12 ----------
# Перші 3 дні прострочення — 10% добової вартості всіх одиниць/день, далі — 20%.
# Доплата ≤ початкової суми; округлення донизу один раз.

@pytest.mark.parametrize("daily_rate, quantity, days, overdue, expected_fee", [
    (1000, 2, 5, 0, 0),            # 0 днів -> нульова доплата
    (1000, 2, 5, 1, 200),          # 1 день: 10% від 2000
    (1000, 2, 5, 3, 600),          # межа: рівно 3 дні по 10%
    (1000, 2, 5, 4, 1000),         # 3*10% + 1*20% = 50% від 2000
    (1000, 2, 5, 5, 1400),         # 3*10% + 2*20% = 70% від 2000
    (105, 1, 1, 1, 10),            # 10.5 -> округлення донизу
    (1000, 1, 1, 6, 900),          # 90% < total (1000)
    (1000, 1, 1, 7, 1000),         # 110% -> обмежено початковою сумою
    (1000, 2, 5, 36500, 10000),    # максимальне прострочення -> total
])
def test_return_rental_late_fee_matches_personal_rule(
        service, rental_repo, make_equipment, make_rental,
        daily_rate, quantity, days, overdue, expected_fee):
    eq = make_equipment(daily_rate=daily_rate, stock=20)
    rental = service.rent(RentalRequest("R-1", eq.id, quantity, days))
    total = daily_rate * quantity * days

    returned = service.return_rental(rental.id, overdue)

    stored = rental_repo.find_by_id(rental.id)
    assert returned.late_fee == expected_fee
    assert stored.late_fee == expected_fee
    assert stored.status == RentalStatus.RETURNED
    assert stored.total == total                       # початкова сума не змінюється
    assert stored.total + stored.late_fee == total + expected_fee


def test_return_rental_does_not_call_external_policy_or_change_stock(
        service, rental_repo, equipment_repo, make_equipment, policy):
    eq = make_equipment(stock=5)
    rental = service.rent(RentalRequest("R-1", eq.id, 2, 3))
    policy.is_allowed.reset_mock()

    service.return_rental(rental.id, 2)

    policy.is_allowed.assert_not_called()
    assert equipment_repo.find_by_id(eq.id).stock == 5


def test_return_rental_twice_is_rejected_and_state_unchanged(
        service, rental_repo, make_equipment):
    eq = make_equipment(daily_rate=1000)
    rental = service.rent(RentalRequest("R-1", eq.id, 1, 1))
    service.return_rental(rental.id, 1)
    before = rental_repo.find_by_id(rental.id)

    with pytest.raises(RentalRejectedError):
        service.return_rental(rental.id, 5)

    assert rental_repo.find_by_id(rental.id) == before
    assert before.late_fee == 100


def test_return_unknown_rental_raises_value_error(service, count_rows):
    with pytest.raises(ValueError):
        service.return_rental(4040, 0)
    assert count_rows("rentals") == 0


@pytest.mark.parametrize("overdue", [-1, 36501])
def test_return_invalid_overdue_leaves_rental_active(
        service, rental_repo, make_equipment, overdue):
    eq = make_equipment()
    rental = service.rent(RentalRequest("R-1", eq.id, 1, 1))

    with pytest.raises(ValueError):
        service.return_rental(rental.id, overdue)

    stored = rental_repo.find_by_id(rental.id)
    assert stored.status == RentalStatus.ACTIVE
    assert stored.late_fee == 0
