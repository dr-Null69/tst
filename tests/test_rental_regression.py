import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from rental.models import Equipment, RentalRequest, RentalStatus as S
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.service import RentalService, RentalRejectedError

try:
    from testcontainers.community.postgres import PostgresContainer
    HAS_TESTCONTAINERS = True
except ImportError:
    try:
        from testcontainers.postgres import PostgresContainer
        HAS_TESTCONTAINERS = True
    except ImportError:
        HAS_TESTCONTAINERS = False
        PostgresContainer = None


class Policy:
    def is_allowed(self, equipment, quantity, days):
        return True


@pytest.fixture(scope="module")
def sut():
    use_container = False
    container = None
    if HAS_TESTCONTAINERS:
        try:
            container = PostgresContainer("postgres:17.6-alpine")
            container.start()
            use_container = True
        except Exception:
            use_container = False

    if use_container and container is not None:
        try:
            options = dict(
                host=container.get_container_host_ip(),
                port=container.get_exposed_port(5432),
                user=container.username,
                password=container.password,
                dbname=container.dbname,
                autocommit=True
            )
            with psycopg.connect(**options) as db:
                migration = Path(__file__).resolve().parents[1] / "migrations/V1__create_tables.sql"
                db.execute(migration.read_text(encoding="utf-8"))
                equipment = PostgresEquipmentRepository(db)
                rentals = PostgresRentalRepository(db)
                yield db, equipment, rentals, RentalService(equipment, rentals, Policy()), options
        finally:
            container.stop()
    else:
        # Fallback: використання локальної тестової БД проєкту (PostgreSQL 17)
        options = dict(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "rental_variant12_test"),
            user=os.getenv("DB_USER", "rental_student"),
            password=os.getenv("DB_PASSWORD", ""),
            autocommit=True
        )
        with psycopg.connect(**options) as db:
            migration = Path(__file__).resolve().parents[1] / "migrations/V1__create_tables.sql"
            try:
                db.execute(migration.read_text(encoding="utf-8"))
            except Exception:
                pass  # Таблиці вже створені міграцією
            db.execute("TRUNCATE rental_items, rentals, equipment RESTART IDENTITY CASCADE")
            equipment = PostgresEquipmentRepository(db)
            rentals = PostgresRentalRepository(db)
            yield db, equipment, rentals, RentalService(equipment, rentals, Policy()), options


def item(sut):
    return sut[1].create(Equipment(0, str(uuid4()), 10000, 5))


def request(equipment):
    return RentalRequest(str(uuid4()), equipment.id, 1, 2, False)


@pytest.mark.smoke
def test_create_rental(sut):
    saved = sut[3].rent(request(item(sut)))
    assert saved.status == S.ACTIVE
    assert sut[2].find_by_id(saved.id) == saved


@pytest.mark.smoke
def test_return_rental(sut):
    eq = item(sut)
    saved = sut[3].rent(request(eq))
    returned = sut[3].return_rental(saved.id, 0)
    assert returned.status == S.RETURNED
    assert returned.late_fee == 0
    assert sut[2].find_by_id(saved.id) == returned


@pytest.mark.smoke
def test_rental_amount(sut):
    equipment = item(sut)
    saved = sut[3].rent(request(equipment))
    stored = sut[2].find_by_id(saved.id)
    assert stored.total == 20000
    assert stored.item.equipment_id == equipment.id
    assert stored.item.quantity == 1
    assert stored.item.daily_rate == 10000




def test_equipment_stock(sut):

    unique_code = f"STK-{uuid4().hex[:16]}"
    saved = sut[1].create(Equipment(0, unique_code, 10000, 5))
    assert sut[1].find_by_id(saved.id).stock == 5


def test_equipment_rate(sut):

    unique_code = f"RAT-{uuid4().hex[:16]}"
    saved = sut[1].create(Equipment(0, unique_code, 15000, 3))
    assert sut[1].find_by_id(saved.id).daily_rate == 15000


def test_rental_after_policy_check(sut):

    class DelayedPolicy:
        def is_allowed(self, equipment, quantity, days):
            time.sleep(float(os.getenv("REGRESSION_POLICY_DELAY_MS", "200")) / 1000)
            return True

    equipment = item(sut)
    data = request(equipment)

    def rent():
        with psycopg.connect(**sut[4]) as db:
            service = RentalService(
                PostgresEquipmentRepository(db),
                PostgresRentalRepository(db),
                DelayedPolicy()
            )
            return service.rent(data)

    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(rent)
        operation.result(timeout=2.0)
        
        saved = sut[2].find_rentals(equipment.id, S.ACTIVE)
        assert len(saved) == 1
        assert saved[0].reference == data.reference
        assert saved[0].total == 20000


def test_rental_selection(sut):

    equipment = item(sut)
    prefix = str(uuid4())[:8]
    references = [prefix + "-A", prefix + "-B"]
    fixture_order = list(reversed(references)) if os.getenv("REGRESSION_REVERSE_FIXTURE") == "true" else references
    for reference in fixture_order:
        sut[3].rent(RentalRequest(reference, equipment.id, 1, 2, False))
    
    actual = [
        row[0]
        for row in sut[0].execute(
            "SELECT r.reference FROM rentals r "
            "JOIN rental_items i ON i.rental_id=r.id "
            "WHERE i.equipment_id=%s ORDER BY r.reference",
            (equipment.id,)
        )
    ]
    assert actual == sorted(references)


def test_stock_rejection(sut):
    equipment = item(sut)
    with pytest.raises(RentalRejectedError):
        sut[3].rent(RentalRequest(str(uuid4()), equipment.id, 6, 2, False))
    assert sut[2].find_rentals(equipment.id, S.ACTIVE) == []


def test_policy_rejection(sut):
    class DeniedPolicy:
        def is_allowed(self, equipment, quantity, days):
            return False

    equipment = item(sut)
    denied = RentalService(sut[1], sut[2], DeniedPolicy())
    with pytest.raises(RentalRejectedError):
        denied.rent(request(equipment))
    assert sut[2].find_rentals(equipment.id, S.ACTIVE) == []


def test_repeated_return(sut):
    saved = sut[3].rent(request(item(sut)))
    returned = sut[3].return_rental(saved.id, 0)
    with pytest.raises(RentalRejectedError):
        sut[3].return_rental(saved.id, 0)
    assert sut[2].find_by_id(saved.id) == returned
