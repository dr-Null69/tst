"""Repository <-> Database: реальні Postgres-репозиторії + реальна test database."""
import psycopg
import pytest

from rental.models import Equipment, RentalStatus
from rental.service import RentalRejectedError


# ---------- Equipment repository ----------

def test_equipment_create_and_find_by_id_repositoryState(equipment_repo):
    created = equipment_repo.create(Equipment(999, "DRILL-1", 2500, 7))

    # create ігнорує вхідний id і повертає id, згенерований БД
    assert created.id != 999
    found = equipment_repo.find_by_id(created.id)
    assert found == Equipment(created.id, "DRILL-1", 2500, 7)


def test_equipment_find_by_id_returns_none_for_missing(equipment_repo):
    assert equipment_repo.find_by_id(12345) is None


def test_equipment_update_changes_stored_values(equipment_repo, make_equipment):
    eq = make_equipment(code="OLD", daily_rate=1000, stock=5)

    equipment_repo.update(Equipment(eq.id, "NEW", 1500, 2))

    assert equipment_repo.find_by_id(eq.id) == Equipment(eq.id, "NEW", 1500, 2)


def test_equipment_update_missing_raises_value_error(equipment_repo, count_rows):
    with pytest.raises(ValueError):
        equipment_repo.update(Equipment(404, "X", 1000, 1))
    assert count_rows("equipment") == 0


def test_equipment_delete_removes_row_and_missing_delete_is_noop(equipment_repo, make_equipment):
    eq = make_equipment()
    other = make_equipment(code="OTHER")

    equipment_repo.delete(eq.id)
    equipment_repo.delete(eq.id)  # повторне / відсутнє видалення нічого не змінює

    assert equipment_repo.find_by_id(eq.id) is None
    assert equipment_repo.find_by_id(other.id) == other


def test_equipment_duplicate_code_rejected_by_database(equipment_repo, make_equipment, count_rows):
    make_equipment(code="DUP")
    with pytest.raises(psycopg.errors.UniqueViolation):
        equipment_repo.create(Equipment(0, "DUP", 1000, 1))
    assert count_rows("equipment") == 1


def test_equipment_with_rentals_cannot_be_deleted(equipment_repo, make_equipment, make_rental, count_rows):
    eq = make_equipment()
    make_rental(eq)

    with pytest.raises((psycopg.errors.ForeignKeyViolation, psycopg.errors.RestrictViolation)):
        equipment_repo.delete(eq.id)

    assert equipment_repo.find_by_id(eq.id) == eq
    assert count_rows("rental_items") == 1


# ---------- Rental repository ----------

def test_rental_create_and_find_by_id_repositoryState(rental_repo, make_equipment, make_rental):
    eq = make_equipment(daily_rate=1000)

    created = make_rental(eq, reference="R-100", quantity=3, days=4, premium=True)

    found = rental_repo.find_by_id(created.id)
    assert found == created
    assert (found.reference, found.days, found.premium) == ("R-100", 4, True)
    assert (found.total, found.late_fee, found.status) == (12000, 0, RentalStatus.ACTIVE)
    assert (found.item.equipment_id, found.item.quantity, found.item.daily_rate) == (eq.id, 3, 1000)


def test_rental_find_by_id_returns_none_for_missing(rental_repo):
    assert rental_repo.find_by_id(777) is None


def test_rental_find_rentals_applies_both_filters_ordered_by_id(rental_repo, make_equipment, make_rental):
    eq_a, eq_b = make_equipment(code="A"), make_equipment(code="B")
    r1 = make_rental(eq_a, reference="R-1")
    r2 = make_rental(eq_a, reference="R-2")
    r3 = make_rental(eq_a, reference="R-3")
    make_rental(eq_b, reference="R-4")
    rental_repo.mark_returned(r2.id, 0)

    active_a = rental_repo.find_rentals(eq_a.id, RentalStatus.ACTIVE)
    returned_a = rental_repo.find_rentals(eq_a.id, RentalStatus.RETURNED)

    assert [r.id for r in active_a] == [r1.id, r3.id]
    assert [r.id for r in returned_a] == [r2.id]
    assert rental_repo.find_rentals(eq_b.id, RentalStatus.RETURNED) == []


def test_rental_mark_returned_updates_status_and_late_fee(rental_repo, make_equipment, make_rental):
    rental = make_rental(make_equipment(), total=10000)

    rental_repo.mark_returned(rental.id, 600)

    stored = rental_repo.find_by_id(rental.id)
    assert stored.status == RentalStatus.RETURNED
    assert stored.late_fee == 600
    assert stored.total == 10000  # початкова сума не змінюється


def test_rental_mark_returned_twice_or_missing_is_rejected(rental_repo, make_equipment, make_rental):
    rental = make_rental(make_equipment())
    rental_repo.mark_returned(rental.id, 100)

    with pytest.raises(RentalRejectedError):
        rental_repo.mark_returned(rental.id, 999)
    with pytest.raises(RentalRejectedError):
        rental_repo.mark_returned(4040, 0)

    assert rental_repo.find_by_id(rental.id).late_fee == 100  # стан не змінено


def test_rental_delete_cascades_to_item(rental_repo, make_equipment, make_rental, count_rows):
    rental = make_rental(make_equipment())
    assert count_rows("rental_items") == 1

    rental_repo.delete(rental.id)

    assert rental_repo.find_by_id(rental.id) is None
    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0
    rental_repo.delete(rental.id)  # відсутнє видалення нічого не змінює


def test_rental_duplicate_reference_is_atomic(rental_repo, make_equipment, make_rental, count_rows):
    eq = make_equipment()
    make_rental(eq, reference="SAME")

    with pytest.raises(psycopg.errors.UniqueViolation):
        make_rental(eq, reference="SAME")

    assert count_rows("rentals") == 1
    assert count_rows("rental_items") == 1
