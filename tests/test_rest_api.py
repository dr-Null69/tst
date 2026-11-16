from dotenv import load_dotenv
load_dotenv()

import os
import pytest
import requests
import psycopg

BASE_URL = "http://127.0.0.1:8000"
RENTALS_URL = f"{BASE_URL}/rentals"

@pytest.fixture
def equipment_id():
    conn = psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "rental_variant12_test"),
        user=os.getenv("DB_USER", "rental_student"),
        password=os.getenv("DB_PASSWORD", ""),
        autocommit=True
    )

    conn.execute("TRUNCATE rental_items, rentals, equipment RESTART IDENTITY CASCADE")
    
    with conn.cursor() as cur:
        cur.execute("INSERT INTO equipment(code, daily_rate, stock) VALUES ('API-TEST', 1000, 5) RETURNING id")
        eid = cur.fetchone()[0]
    yield eid
    

    conn.execute("TRUNCATE rental_items, rentals, equipment RESTART IDENTITY CASCADE")
    conn.close()

@pytest.fixture
def valid_payload(equipment_id):
    return {
        "reference": "DOC-123",
        "equipment_id": equipment_id,
        "quantity": 2,
        "days": 5,
        "premium": False
    }


def test_post_create_rental(valid_payload):
    response = requests.post(RENTALS_URL, json=valid_payload)
    assert response.status_code == 201
    
    data = response.json()
    assert "id" in data and data["id"] is not None
    assert data["reference"] == valid_payload["reference"]
    assert data["status"] == "Active"


def test_get_rental_by_id(valid_payload):
    create_resp = requests.post(RENTALS_URL, json=valid_payload)
    rental_id = create_resp.json()["id"]

    get_resp = requests.get(f"{RENTALS_URL}/{rental_id}")
    assert get_resp.status_code == 200
    
    data = get_resp.json()
    assert data["id"] == rental_id
    assert data["status"] == "Active"

def test_get_non_existent_rental():
    response = requests.get(f"{RENTALS_URL}/9999999")
    assert response.status_code == 404
    
    error_data = response.json()
    assert "code" in error_data
    assert "message" in error_data

їdef test_return_rental_action(valid_payload):
    rental_id = requests.post(RENTALS_URL, json=valid_payload).json()["id"]

    action_resp = requests.post(f"{RENTALS_URL}/{rental_id}/return", json={"overdue_days": 1})
    assert action_resp.status_code == 200
    
    data = action_resp.json()
    assert data["id"] == rental_id
    assert data["status"] == "Returned"
    assert data["late_fee"] > 0

def test_repeated_return_action_conflict(valid_payload):
    rental_id = requests.post(RENTALS_URL, json=valid_payload).json()["id"]
    requests.post(f"{RENTALS_URL}/{rental_id}/return", json={"overdue_days": 0})

    conflict_resp = requests.post(f"{RENTALS_URL}/{rental_id}/return", json={"overdue_days": 0})
    assert conflict_resp.status_code == 409
    
    error_data = conflict_resp.json()
    assert "code" in error_data
    assert "message" in error_data

def test_immutability_after_conflict(valid_payload):
    rental_id = requests.post(RENTALS_URL, json=valid_payload).json()["id"]
    requests.post(f"{RENTALS_URL}/{rental_id}/return", json={"overdue_days": 0})
    
    state_before = requests.get(f"{RENTALS_URL}/{rental_id}").json()

    conflict_resp = requests.post(f"{RENTALS_URL}/{rental_id}/return", json={"overdue_days": 0})
    assert conflict_resp.status_code == 409

    state_after = requests.get(f"{RENTALS_URL}/{rental_id}").json()
    assert state_after == state_before
    assert state_after["status"] == "Returned"

@pytest.mark.parametrize("test_qty, expected_status, expected_result", [
    (5, 201, "Active"),
    (25, 400, "BAD_REQUEST"),
])
def test_business_rule_rental_quantity(valid_payload, test_qty, expected_status, expected_result):
    valid_payload["quantity"] = test_qty
    response = requests.post(RENTALS_URL, json=valid_payload)
    
    assert response.status_code == expected_status
    body = response.json()
    
    if expected_status == 201:
        assert body["status"] == expected_result
    else:
        assert body["code"] == expected_result

def test_full_lifecycle_workflow(valid_payload):
    post_resp = requests.post(RENTALS_URL, json=valid_payload)
    assert post_resp.status_code == 201
    rental_id = post_resp.json()["id"]

    get_initial = requests.get(f"{RENTALS_URL}/{rental_id}")
    assert get_initial.status_code == 200
    assert get_initial.json()["status"] == "Active"

    action_resp = requests.post(f"{RENTALS_URL}/{rental_id}/return", json={"overdue_days": 2})
    assert action_resp.status_code == 200

    get_final = requests.get(f"{RENTALS_URL}/{rental_id}")
    assert get_final.status_code == 200
    assert get_final.json()["status"] == "Returned"