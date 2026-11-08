import json

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)

good = {
    "last_name": "Иванов",
    "first_name": "Иван",
    "birth_date": "1990-05-17",
    "phone": "8 (912) 345-67-89",
    "email": "ivanov@example.com",
    "reasons": ["нет доступа к сети", "не приходят письма"],
    "problem_detected_at": "2026-10-30T18:45:00",
}


def check_bad(field, values):
    for value in values:
        r = client.post("/appeals/", json={**good, field: value})
        assert r.status_code == 422, f"{field}={value!r} прошло, а не должно"


def test_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "APPEALS_DIR", tmp_path)

    r = client.post("/appeals/", json=good)
    assert r.status_code == 200

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["last_name"] == "Иванов"
    assert data["phone"] == "+79123456789"
    assert data["reasons"] == ["нет доступа к сети", "не приходят письма"]


def test_bad_names():
    check_bad("last_name", ["иванов", "ИВАНОВ", "Ivanov", "Иванов2", "Иванов-Петров", ""])
    check_bad("first_name", ["Ив@н", "Иван Иван"])


def test_bad_contacts():
    check_bad("phone", ["12345", "+1 912 345 67 89", "8912345678", "+7９１２３４５６７８９"])
    check_bad("email", ["not-an-email", "ivanov@"])


def test_bad_dates():
    check_bad("birth_date", ["2090-01-01", "1800-01-01", "17.05.1990"])
    # будущее и раньше даты рождения
    check_bad("problem_detected_at", ["2099-01-01T00:00:00", "1980-01-01T00:00:00"])


def test_bad_reasons():
    check_bad("reasons", [[], ["сломался роутер"], ["не работает телефон", "не работает телефон"]])
