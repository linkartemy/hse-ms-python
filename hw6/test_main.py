import pytest

from main import Student, StudentRepository

CSV = """Фамилия,Имя,Факультет,Курс,Оценка
Ли,Иван,АВТФ,Теор. Механика,52
Ким,Петр,ФПМИ,Мат. Анализ,28
Райт,Вероника,ФЛА,Теор. Механика,7
Джонс,Андрей,РЭФ,Мат. Анализ,4
Джонс,Андрей,РЭФ,Информатика,40
"""


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "students.csv"
    path.write_text(CSV, encoding="utf-8")
    repo = StudentRepository("sqlite://")
    repo.load_csv(path)
    return repo


def test_load_and_select(repo):
    students = repo.select()
    assert len(students) == 5
    assert students[0].last_name == "Ли"
    assert students[0].grade == 52


def test_insert(repo):
    repo.insert([Student(last_name="Ву", first_name="Дмитрий",
                         faculty="АВТФ", course="Физика", grade=77)])
    found = repo.select(Student.last_name == "Ву")
    assert len(found) == 1
    assert found[0].course == "Физика"


def test_by_faculty(repo):
    # Джонс два раза в таблице, но в списке один раз
    assert repo.get_by_faculty("РЭФ") == [("Джонс", "Андрей")]
    assert repo.get_by_faculty("НЕТ") == []


def test_courses(repo):
    assert repo.get_courses() == ["Информатика", "Мат. Анализ",
                                  "Теор. Механика"]


def test_low_grades(repo):
    low = repo.get_low_grades("Мат. Анализ")
    assert [(s.last_name, s.grade) for s in low] == [("Ким", 28),
                                                     ("Джонс", 4)]
    # 52 не ниже 30, 7 ниже
    low = repo.get_low_grades("Теор. Механика")
    assert [s.last_name for s in low] == ["Райт"]


def test_avg_grade(repo):
    assert repo.get_avg_grade("РЭФ") == 22
    assert repo.get_avg_grade("НЕТ") is None


def test_csv_with_semicolon_and_bom(tmp_path):
    path = tmp_path / "students.csv"
    path.write_text(CSV.replace(",", ";"), encoding="utf-8-sig")
    repo = StudentRepository("sqlite://")
    assert repo.load_csv(path, delimiter=";") == 5
    assert repo.get_avg_grade("АВТФ") == 52
