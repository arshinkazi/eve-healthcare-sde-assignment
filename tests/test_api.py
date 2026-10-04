from datetime import datetime, timedelta, timezone

from .conftest import auth_headers


def setup_catalog(client, headers):
    centre = client.post(
        "/centres", headers=headers,
        json={"name": "EVE Diagnostics", "location": "Jaipur"},
    ).json()
    test = client.post(
        "/tests", headers=headers,
        json={"name": "CBC", "description": "Complete Blood Count"},
    ).json()
    mapping = client.post(
        f"/centres/{centre['id']}/tests",
        headers=headers,
        json={"test_id": test["id"], "price": "499.00"},
    )
    assert mapping.status_code == 201
    return centre, test


def make_booking(client, headers, centre, test):
    appointment = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    response = client.post(
        "/bookings",
        headers=headers,
        json={
            "centre_id": centre["id"],
            "test_id": test["id"],
            "appointment_at": appointment,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_signup_login_and_booking(client):
    headers = auth_headers(client)
    centre, test = setup_catalog(client, headers)
    booking = make_booking(client, headers, centre, test)
    assert booking["status"] == "PENDING"
    assert booking["amount"] == "499.00"


def test_payment_success_confirms_booking(client):
    headers = auth_headers(client)
    centre, test = setup_catalog(client, headers)
    booking = make_booking(client, headers, centre, test)

    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": booking["id"], "outcome": "SUCCESS"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "SUCCESS"

    updated = client.get(f"/bookings/{booking['id']}", headers=headers).json()
    assert updated["status"] == "CONFIRMED"


def test_failed_payment_fails_booking(client):
    headers = auth_headers(client)
    centre, test = setup_catalog(client, headers)
    booking = make_booking(client, headers, centre, test)

    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": booking["id"], "outcome": "FAILED"},
    )
    assert response.status_code == 201
    updated = client.get(f"/bookings/{booking['id']}", headers=headers).json()
    assert updated["status"] == "FAILED"


def test_webhook_is_idempotent(client):
    headers = auth_headers(client)
    centre, test = setup_catalog(client, headers)
    booking = make_booking(client, headers, centre, test)

    payload = {"event_id": "evt_123", "booking_id": booking["id"], "status": "SUCCESS"}
    first = client.post("/payments/webhook", json=payload)
    second = client.post("/payments/webhook", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    updated = client.get(f"/bookings/{booking['id']}", headers=headers).json()
    assert updated["status"] == "CONFIRMED"


def test_webhook_rejects_reused_event_with_different_data(client):
    headers = auth_headers(client)
    centre, test = setup_catalog(client, headers)
    booking = make_booking(client, headers, centre, test)

    payload = {"event_id": "evt_same", "booking_id": booking["id"], "status": "SUCCESS"}
    assert client.post("/payments/webhook", json=payload).status_code == 200

    altered = {"event_id": "evt_same", "booking_id": booking["id"], "status": "FAILED"}
    assert client.post("/payments/webhook", json=altered).status_code == 409


def test_user_cannot_access_another_users_booking(client):
    owner_headers = auth_headers(client)
    centre, test = setup_catalog(client, owner_headers)
    booking = make_booking(client, owner_headers, centre, test)

    client.post(
        "/auth/signup",
        json={"email": "other@example.com", "password": "password123", "full_name": "Other User"},
    )
    token = client.post(
        "/auth/login",
        json={"email": "other@example.com", "password": "password123"},
    ).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/bookings/{booking['id']}", headers=other_headers).status_code == 403


def test_duplicate_booking_is_rejected(client):
    headers = auth_headers(client)
    centre, test = setup_catalog(client, headers)
    booking = make_booking(client, headers, centre, test)

    appointment = booking["appointment_at"] + "+00:00"
response = client.post(
    "/bookings",
    headers=headers,
    json={
        "centre_id": centre["id"],
        "test_id": test["id"],
        "appointment_at": appointment,
    },
)
    assert response.status_code == 409


def test_cancelled_booking_cannot_be_paid(client):
    headers = auth_headers(client)
    centre, test = setup_catalog(client, headers)
    booking = make_booking(client, headers, centre, test)

    assert client.post(f"/bookings/{booking['id']}/cancel", headers=headers).status_code == 200
    response = client.post(
        "/payments/",
        headers=headers,
        json={"booking_id": booking["id"], "outcome": "SUCCESS"},
    )
    assert response.status_code == 409
