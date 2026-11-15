import pytest
from fastapi.testclient import TestClient

from main import Student, StudentRepository, app, get_repo

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
def client(repo):
    app.dependency_overrides[get_repo] = lambda: repo
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_repo_update_and_delete(repo):
    student = repo.update(2, grade=31)
    assert student.grade == 31
    assert repo.get(2).grade == 31
    assert repo.update(999, grade=1) is None

    assert repo.delete(1) is True
    assert repo.get(1) is None
    assert repo.delete(1) is False
    assert repo.count() == 2

    # id удаленной записи заново не выдается
    repo.delete(3)
    repo.insert([Student(**NEW)])
    assert repo.select()[-1].id == 4


def test_create_and_read(client):
    r = client.post("/students/", json=NEW)
    assert r.status_code == 201
    created = r.json()
    assert created["id"] == 4
    assert created["last_name"] == "Смит"

    r = client.get(f"/students/{created['id']}")
    assert r.status_code == 200
    assert r.json() == created

    assert len(client.get("/students/").json()) == 4
    math = client.get("/students/", params={"course": "Мат. Анализ"})
    assert [s["id"] for s in math.json()] == [2, 3]


def test_update(client):
    r = client.put("/students/1", json=NEW)
    assert r.status_code == 200
    assert r.json() == {"id": 1, **NEW}

    r = client.patch("/students/2", json={"grade": 30})
    assert r.status_code == 200
    assert r.json()["grade"] == 30
    assert r.json()["last_name"] == "Ким"


def test_delete(client):
    assert client.delete("/students/3").status_code == 204
    assert client.get("/students/3").status_code == 404
    assert client.delete("/students/3").status_code == 404


def test_errors(client):
    assert client.get("/students/999").status_code == 404
    assert client.put("/students/999", json=NEW).status_code == 404
    assert client.patch("/students/999", json={"grade": 1}).status_code == 404

    bad = [
        {**NEW, "grade": 101},
        {**NEW, "grade": -1},
        {**NEW, "last_name": ""},
        {k: v for k, v in NEW.items() if k != "course"},
        {**NEW, "faculty": "   "},
        {**NEW, "grade": True},
    ]
    for data in bad:
        assert client.post("/students/", json=data).status_code == 422
    assert client.patch("/students/1", json={"grade": None}).status_code == 422
    assert client.get(f"/students/{10**20}").status_code == 422
