"""Спільне тестове середовище Integration Test Suite (SUT B, варіант 12).

Integration boundary:  RentalService -> Postgres*Repository -> реальна test database.
Test Double (Mock) використовується лише для RentalPolicy — зовнішньої залежності,
яка не входить до integration boundary.
"""
from unittest.mock import Mock

import pytest

from rental.database import connect
from rental.models import Equipment, RentalItem, RentalOrder, RentalStatus
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.service import RentalService


@pytest.fixture
def connection():
    """Нове реальне з'єднання (autocommit=True) з контрольованим початковим станом БД.

    Перед кожним тестом таблиці очищаються, а лічильники id скидаються, тому тести
    не залежать від порядку запуску й від даних попередніх тестів, а suite можна
    запускати повторно без ручного втручання.
    """
    with connect() as conn:
        db_name = conn.execute("SELECT current_database()").fetchone()[0]
        # Захист: ніколи не чистимо БД, яка не є тестовою.
        assert db_name.endswith("_test"), f"Небезпечно очищати не тестову БД: {db_name}"
        conn.execute("TRUNCATE rental_items, rentals, equipment RESTART IDENTITY CASCADE")
        yield conn
        conn.execute("TRUNCATE rental_items, rentals, equipment RESTART IDENTITY CASCADE")


@pytest.fixture
def equipment_repo(connection):
    return PostgresEquipmentRepository(connection)


@pytest.fixture
def rental_repo(connection):
    return PostgresRentalRepository(connection)


@pytest.fixture
def policy():
    """Mock зовнішньої RentalPolicy: за замовчуванням дозволяє оренду."""
    mock = Mock(name="RentalPolicy")
    mock.is_allowed.return_value = True
    return mock


@pytest.fixture
def service(equipment_repo, rental_repo, policy):
    """Реальний Service + реальні Repository + Mock-політика."""
    return RentalService(equipment_repo, rental_repo, policy)


@pytest.fixture
def make_equipment(equipment_repo):
    def factory(code="CAM-1", daily_rate=1000, stock=5):
        return equipment_repo.create(Equipment(0, code, daily_rate, stock))
    return factory


@pytest.fixture
def make_rental(rental_repo):
    def factory(equipment, reference="R-1", quantity=2, days=5, premium=False,
                total=None, late_fee=0, status=RentalStatus.ACTIVE):
        total = equipment.daily_rate * quantity * days if total is None else total
        item = RentalItem(0, equipment.id, quantity, equipment.daily_rate)
        return rental_repo.create(
            RentalOrder(0, reference, days, premium, total, late_fee, status, item))
    return factory


@pytest.fixture
def count_rows(connection):
    """Кількість рядків у таблиці — для перевірки, що небажаний запис не створено."""
    def counter(table):
        assert table in {"equipment", "rentals", "rental_items"}
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counter
