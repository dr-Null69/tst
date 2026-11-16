"""Lab 5: Service -> Repository -> PostgreSQL (Testcontainers) + HttpRentalPolicy -> WireMock (Testcontainers).

Зовнішня залежність — НЕ Mock/Stub рівня Python: SUT виконує реальний HTTP POST /rental-policy.
Кожен сценарій перевіряє (1) фактичний стан PostgreSQL та (2) фактичні запити в журналі WireMock.
"""
import pytest

from rental.http_rental_policy import HttpRentalPolicy, RentalPolicyError
from rental.models import RentalRequest, RentalStatus
from rental.service import RentalRejectedError, RentalService


def assert_single_policy_call(wiremock, equipment_id, quantity, days):
    """Рівно один POST /rental-policy з очікуваними тілом і Content-Type."""
    calls = wiremock.policy_requests()
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "/rental-policy"
    assert wiremock.header_of(call, "Content-Type") == "application/json"
    assert wiremock.body_of(call) == {"equipmentId": equipment_id, "quantity": quantity, "days": days}


def assert_database_empty(count_rows):
    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0


# =====================================================================
#  3.5  Позитивний сценарій
# =====================================================================

def test_rent_allowed_persists_rental_and_calls_external_policy(
        http_service, wiremock, connection, rental_repo, equipment_repo, make_equipment):
    eq = make_equipment(code="CAM-1", daily_rate=1000, stock=5)
    wiremock.stub_allowed(True)

    result = http_service.rent(RentalRequest("R-1", eq.id, quantity=2, days=5, premium=True))

    # фактичний стан PostgreSQL — прямим SQL, незалежно від Repository
    assert connection.execute(
        "SELECT id, reference, premium, days, total, late_fee, status FROM rentals").fetchall() \
        == [(result.id, "R-1", True, 5, 10000, 0, "Active")]
    assert connection.execute(
        "SELECT rental_id, equipment_id, quantity, daily_rate FROM rental_items").fetchall() \
        == [(result.id, eq.id, 2, 1000)]
    # і повторне читання через persistence layer
    assert rental_repo.find_by_id(result.id) == result
    assert result.status == RentalStatus.ACTIVE
    assert equipment_repo.find_by_id(eq.id) == eq          # залишок не змінюється
    # персональне правило варіанта 12: знижки немає, total = ціна × кількість × дні
    assert result.total == 1000 * 2 * 5
    # зовнішній сервіс викликано з правильними параметрами
    assert_single_policy_call(wiremock, eq.id, 2, 5)


@pytest.mark.parametrize("daily_rate, quantity, days, overdue, expected_fee", [
    (1000, 2, 5, 0, 0),          # 0 днів -> 0
    (1000, 2, 5, 3, 600),        # межа: 3 дні по 10%
    (1000, 2, 5, 5, 1400),       # 3×10% + 2×20% = 70% від 2000
    (105, 1, 1, 1, 10),          # 10.5 -> округлення донизу
    (1000, 1, 1, 7, 1000),       # 110% -> обмежено початковою сумою
])
def test_return_applies_personal_late_fee_rule_without_http_call(
        http_service, wiremock, connection, make_equipment,
        daily_rate, quantity, days, overdue, expected_fee):
    eq = make_equipment(daily_rate=daily_rate, stock=20)
    wiremock.stub_allowed(True)
    rental = http_service.rent(RentalRequest("R-1", eq.id, quantity, days))
    total = daily_rate * quantity * days

    http_service.return_rental(rental.id, overdue)

    assert connection.execute(
        "SELECT status, total, late_fee FROM rentals WHERE id=%s", (rental.id,)
    ).fetchone() == ("Returned", total, expected_fee)
    assert len(wiremock.policy_requests()) == 1       # лише від rent; return політику не викликає


# =====================================================================
#  3.6  Зовнішня бізнес-відмова (HTTP 200, allowed=false)  -> HTTP request = 1
# =====================================================================

def test_external_rejection_creates_nothing_but_calls_policy(
        http_service, wiremock, make_equipment, equipment_repo, count_rows):
    eq = make_equipment(stock=5)
    wiremock.stub_allowed(False)

    with pytest.raises(RentalRejectedError):
        http_service.rent(RentalRequest("R-1", eq.id, quantity=3, days=7))

    assert_database_empty(count_rows)
    assert equipment_repo.find_by_id(eq.id) == eq
    assert_single_policy_call(wiremock, eq.id, 3, 7)


# =====================================================================
#  3.7  Недоступність / некоректна відповідь зовнішнього service
# =====================================================================

def test_policy_unavailable_503_is_not_treated_as_success(
        http_service, wiremock, make_equipment, equipment_repo, count_rows):
    eq = make_equipment()
    wiremock.stub_policy(status=503, raw_body="Service Unavailable")

    with pytest.raises(RentalPolicyError):
        http_service.rent(RentalRequest("R-1", eq.id, 1, 1))

    assert_database_empty(count_rows)
    assert equipment_repo.find_by_id(eq.id) == eq
    assert_single_policy_call(wiremock, eq.id, 1, 1)


@pytest.mark.parametrize("stub", [
    dict(status=500, raw_body="boom"),
    dict(status=404, raw_body="no mapping"),
    dict(status=200, raw_body="this is not json"),
    dict(status=200, json_body={}),                         # відсутнє поле allowed
    dict(status=200, json_body={"allowed": "yes"}),         # нелогічне значення allowed
])
def test_policy_invalid_responses_raise_policy_error_and_save_nothing(
        http_service, wiremock, make_equipment, count_rows, stub):
    eq = make_equipment()
    wiremock.stub_policy(**stub)

    with pytest.raises(RentalPolicyError):
        http_service.rent(RentalRequest("R-1", eq.id, 1, 1))

    assert_database_empty(count_rows)
    assert len(wiremock.policy_requests()) == 1


def test_policy_timeout_raises_policy_error_and_saves_nothing(
        wiremock, wiremock_url, equipment_repo, rental_repo, make_equipment, count_rows):
    eq = make_equipment()
    wiremock.stub_policy(status=200, json_body={"allowed": True}, delay_ms=3000)
    slow_service = RentalService(equipment_repo, rental_repo, HttpRentalPolicy(wiremock_url, timeout=0.5))

    with pytest.raises(RentalPolicyError):
        slow_service.rent(RentalRequest("R-1", eq.id, 1, 1))

    assert_database_empty(count_rows)
    assert len(wiremock.policy_requests()) == 1


# =====================================================================
#  3.8  Локальна бізнес-відмова  -> HTTP request = 0
# =====================================================================

def test_local_rejection_insufficient_stock_makes_no_http_request(
        http_service, wiremock, make_equipment, count_rows):
    eq = make_equipment(stock=2)
    wiremock.stub_allowed(True)          # навіть якщо зовнішній сервіс дозволив би — до нього не дійде

    with pytest.raises(RentalRejectedError):
        http_service.rent(RentalRequest("R-1", eq.id, quantity=3, days=1))

    assert_database_empty(count_rows)
    assert wiremock.policy_requests() == []
    assert wiremock.total_requests() == 0


@pytest.mark.parametrize("quantity, days", [(0, 1), (21, 1), (1, 0), (1, 31)])
def test_invalid_request_makes_no_http_request(
        http_service, wiremock, make_equipment, count_rows, quantity, days):
    eq = make_equipment(stock=50)
    wiremock.stub_allowed(True)

    with pytest.raises(ValueError):
        http_service.rent(RentalRequest("R-1", eq.id, quantity, days))

    assert_database_empty(count_rows)
    assert wiremock.total_requests() == 0


def test_unknown_equipment_makes_no_http_request(http_service, wiremock, count_rows):
    wiremock.stub_allowed(True)

    with pytest.raises(ValueError):
        http_service.rent(RentalRequest("R-1", 4040, 1, 1))

    assert_database_empty(count_rows)
    assert wiremock.total_requests() == 0


# =====================================================================
#  3.11  Ізоляція тестів: два тести з однаковими даними в будь-якому порядку
# =====================================================================

@pytest.mark.parametrize("run", ["first", "second"])
def test_isolation_each_test_starts_from_clean_state(
        http_service, wiremock, make_equipment, count_rows, run):
    # Якщо стан просочується між тестами — впаде UniqueViolation (code/reference),
    # id не буде 1, а журнал WireMock міститиме запит попереднього тесту.
    assert count_rows("equipment") == 0 and count_rows("rentals") == 0
    assert wiremock.total_requests() == 0

    eq = make_equipment(code="SAME-CODE")
    wiremock.stub_allowed(True)
    rental = http_service.rent(RentalRequest("SAME-REF", eq.id, 1, 1))

    assert (eq.id, rental.id) == (1, 1)                  # TRUNCATE ... RESTART IDENTITY
    assert count_rows("rentals") == 1
    assert len(wiremock.policy_requests()) == 1
