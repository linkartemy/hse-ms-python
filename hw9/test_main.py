import pytest
from fastapi.testclient import TestClient

from main import AuthRepository, StudentRepository, app, get_auth, get_repo

CSV = """Фамилия,Имя,Факультет,Курс,Оценка
Ли,Иван,АВТФ,Теор. Механика,52
Ким,Петр,ФПМИ,Мат. Анализ,28
Джонс,Андрей,РЭФ,Мат. Анализ,4
"""

NEW = {
    "last_name": "Смит",
    "first_name": "Федор",
    "faculty": "ФТФ",
    "course": "Физика",
    "grade": 75,
}


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "students.csv"
    path.write_text(CSV, encoding="utf-8")
    # отдельная база во временной папке, чтобы не трогать students.db
    url = f"sqlite:///{tmp_path / 'test.db'}"
    repo = StudentRepository(url)
    repo.load_csv(path)
    auth = AuthRepository(url)
    app.dependency_overrides[get_repo] = lambda: repo
    app.dependency_overrides[get_auth] = lambda: auth
    yield TestClient(app)
    app.dependency_overrides.clear()


def login(client, username="ivan", password="secret1", read_only=False):
    # регистрируем и сразу входим, возвращаем заголовок с токеном
    client.post(
        "/auth/register",
        json={
            "username": username,
            "password": password,
            "read_only": read_only,
        },
    )
    r = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_register_and_login(client):
    data = {"username": "ivan", "password": "secret1"}
    r = client.post("/auth/register", json=data)
    assert r.status_code == 201
    assert r.json() == {"id": 1, "username": "ivan", "read_only": False}
    # пароль в ответе не возвращается
    assert "password" not in r.text

    assert client.post("/auth/register", json=data).status_code == 409

    r = client.post("/auth/login", json=data)
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"

    wrong = {"username": "ivan", "password": "secret2"}
    assert client.post("/auth/login", json=wrong).status_code == 401
    nobody = {"username": "petr", "password": "secret1"}
    assert client.post("/auth/login", json=nobody).status_code == 401

    short = {"username": "ab", "password": "1"}
    assert client.post("/auth/register", json=short).status_code == 422


def test_without_token(client):
    assert client.get("/students/").status_code == 401
    assert client.get("/students/1").status_code == 401
    assert client.post("/students/", json=NEW).status_code == 401
    assert client.delete("/students/1").status_code == 401

    bad = {"Authorization": "Bearer not-a-token"}
    assert client.get("/students/", headers=bad).status_code == 401


def test_logout(client):
    headers = login(client)
    assert client.get("/students/", headers=headers).status_code == 200

    assert client.post("/auth/logout", headers=headers).status_code == 204
    # после выхода токен больше не работает
    assert client.get("/students/", headers=headers).status_code == 401
    assert client.post("/auth/logout", headers=headers).status_code == 401


def test_crud_with_token(client):
    headers = login(client)

    r = client.post("/students/", json=NEW, headers=headers)
    assert r.status_code == 201
    assert r.json()["id"] == 4

    r = client.patch("/students/2", json={"grade": 30}, headers=headers)
    assert r.json()["grade"] == 30

    assert client.delete("/students/3", headers=headers).status_code == 204
    assert client.get("/students/3", headers=headers).status_code == 404
    assert len(client.get("/students/", headers=headers).json()) == 3


def test_read_only(client):
    headers = login(client, "reader", read_only=True)

    assert client.get("/students/", headers=headers).status_code == 200
    assert client.get("/students/1", headers=headers).status_code == 200

    r = client.post("/students/", json=NEW, headers=headers)
    assert r.status_code == 403
    r = client.put("/students/1", json=NEW, headers=headers)
    assert r.status_code == 403
    r = client.patch("/students/1", json={"grade": 1}, headers=headers)
    assert r.status_code == 403
    assert client.delete("/students/1", headers=headers).status_code == 403
    # запись не поменялась
    assert client.get("/students/1", headers=headers).json()["grade"] == 52
