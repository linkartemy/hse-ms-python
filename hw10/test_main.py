import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient
from redis.backoff import NoBackoff
from redis.retry import Retry

from main import (
    AuthRepository,
    Cache,
    StudentRepository,
    app,
    get_auth,
    get_cache,
    get_repo,
)

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
def repo(tmp_path):
    path = tmp_path / "students.csv"
    path.write_text(CSV, encoding="utf-8")
    # отдельная база во временной папке, чтобы не трогать students.db
    repo = StudentRepository(f"sqlite:///{tmp_path / 'test.db'}")
    repo.load_csv(path)
    return repo


@pytest.fixture
def fake():
    # fakeredis вместо настоящего redis, ведет себя так же
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(tmp_path, repo, fake):
    auth = AuthRepository(f"sqlite:///{tmp_path / 'test.db'}")
    app.dependency_overrides[get_repo] = lambda: repo
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_cache] = lambda: Cache(fake)
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


def test_import_csv(client, repo, tmp_path):
    headers = login(client)
    path = tmp_path / "more.csv"
    path.write_text(
        "Фамилия,Имя,Факультет,Курс,Оценка\n"
        "Сидоров,Олег,ФТФ,Физика,90\n"
        "Попова,Анна,ФТФ,Физика,67\n",
        encoding="utf-8",
    )
    r = client.post(
        "/students/import", json={"path": str(path)}, headers=headers
    )
    assert r.status_code == 202
    # TestClient выполняет фоновую задачу сразу после ответа
    assert repo.count() == 5
    r = client.get("/students/", params={"course": "Физика"}, headers=headers)
    assert [s["last_name"] for s in r.json()] == ["Сидоров", "Попова"]


def test_import_errors(client, repo, tmp_path):
    headers = login(client)
    missing = {"path": str(tmp_path / "nope.csv")}
    r = client.post("/students/import", json=missing, headers=headers)
    assert r.status_code == 404

    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    r = client.post(
        "/students/import", json={"path": str(bad)}, headers=headers
    )
    assert r.status_code == 422

    # колонки верные, но оценка не число: задача падает, база не меняется
    broken = tmp_path / "broken.csv"
    broken.write_text(
        "Фамилия,Имя,Факультет,Курс,Оценка\nА,Б,В,Г,много\n",
        encoding="utf-8",
    )
    r = client.post(
        "/students/import", json={"path": str(broken)}, headers=headers
    )
    assert r.status_code == 202
    assert repo.count() == 3

    # оценка вне 0-100 тоже не проходит, как и через api
    broken.write_text(
        "Фамилия,Имя,Факультет,Курс,Оценка\nА,Б,В,Г,50\nА,Б,В,Г,150\n",
        encoding="utf-8",
    )
    r = client.post(
        "/students/import", json={"path": str(broken)}, headers=headers
    )
    assert r.status_code == 202
    assert repo.count() == 3

    reader = login(client, "reader", read_only=True)
    r = client.post(
        "/students/import", json={"path": str(bad)}, headers=reader
    )
    assert r.status_code == 403


def test_bulk_delete(client, repo):
    headers = login(client)
    r = client.post(
        "/students/bulk-delete", json={"ids": [1, 3, 3, 999]},
        headers=headers,
    )
    assert r.status_code == 202
    # 999 нет, его просто пропускаем
    assert [s.id for s in repo.select()] == [2]

    for data in [{"ids": []}, {"ids": [0]}, {"ids": ["1"]}, {}]:
        r = client.post("/students/bulk-delete", json=data, headers=headers)
        assert r.status_code == 422


def test_cache(client, repo, fake):
    headers = login(client)
    assert client.get("/students/1", headers=headers).json()["grade"] == 52
    assert len(client.get("/students/", headers=headers).json()) == 3
    assert len(fake.keys("students:*")) == 2

    # меняем базу в обход api: в ответе пока старые данные из кеша
    repo.update(1, grade=99)
    assert client.get("/students/1", headers=headers).json()["grade"] == 52

    # изменение через api сбрасывает кеш
    client.patch("/students/2", json={"grade": 30}, headers=headers)
    assert fake.keys("students:*") == []
    assert client.get("/students/1", headers=headers).json()["grade"] == 99

    # фоновое удаление тоже сбрасывает кеш
    client.get("/students/", headers=headers)
    client.post("/students/bulk-delete", json={"ids": [1]}, headers=headers)
    assert len(client.get("/students/", headers=headers).json()) == 2


def test_without_redis(client):
    # redis на закрытом порту: все работает, просто без кеша
    dead = redis.Redis(
        port=1, socket_connect_timeout=0.1, retry=Retry(NoBackoff(), 0)
    )
    app.dependency_overrides[get_cache] = lambda: Cache(dead)
    headers = login(client)
    assert client.get("/students/1", headers=headers).status_code == 200
    assert client.delete("/students/1", headers=headers).status_code == 204
