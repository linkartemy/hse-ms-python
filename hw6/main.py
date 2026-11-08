import csv
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# пути от папки со скриптом, чтобы можно было запускать откуда угодно
HERE = Path(__file__).parent
DB_PATH = HERE / "students.db"
CSV_PATH = HERE / "students.csv"

# заголовок в csv -> поле модели. Если в файле другие названия колонок,
# поправить только тут
COLUMNS = {
    "Фамилия": "last_name",
    "Имя": "first_name",
    "Факультет": "faculty",
    "Курс": "course",
    "Оценка": "grade",
}


class Base(DeclarativeBase):
    pass


class Student(Base):
    # одна строка таблицы = одна оценка студента по одному курсу
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_name: Mapped[str]
    first_name: Mapped[str]
    faculty: Mapped[str]
    course: Mapped[str]
    grade: Mapped[int]

    def __repr__(self):
        return (
            f"{self.last_name} {self.first_name}, {self.faculty}, "
            f"{self.course}: {self.grade}"
        )


class StudentRepository:
    def __init__(self, url: str = f"sqlite:///{DB_PATH}") -> None:
        self.engine = create_engine(url)
        # без expire_on_commit=False объекты после закрытия сессии
        # нельзя было бы читать
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def insert(self, students: list[Student]) -> None:
        with self.Session() as session:
            session.add_all(students)
            session.commit()

    def select(self, *where) -> list[Student]:
        with self.Session() as session:
            query = select(Student).where(*where).order_by(Student.id)
            return list(session.scalars(query))

    # utf-8-sig читает и обычный utf-8, и файл с BOM (так сохраняет excel)
    def load_csv(self, path=CSV_PATH, delimiter=",", encoding="utf-8-sig"):
        students = []
        with open(path, encoding=encoding, newline="") as f:
            for row in csv.DictReader(f, delimiter=delimiter):
                data = {
                    COLUMNS[k.strip()]: v.strip()
                    for k, v in row.items()
                    if k and k.strip() in COLUMNS
                }
                data["grade"] = int(data["grade"])
                students.append(Student(**data))
        self.insert(students)
        return len(students)

    def get_by_faculty(self, faculty: str) -> list[tuple[str, str]]:
        # в таблице один человек встречается несколько раз (по разу на
        # курс), поэтому берем уникальные пары фамилия + имя
        query = (
            select(Student.last_name, Student.first_name)
            .where(Student.faculty == faculty)
            .distinct()
            .order_by(Student.last_name, Student.first_name)
        )
        with self.Session() as session:
            return [tuple(row) for row in session.execute(query)]

    def get_courses(self) -> list[str]:
        query = select(Student.course).distinct().order_by(Student.course)
        with self.Session() as session:
            return list(session.scalars(query))

    def get_low_grades(self, course: str, limit: int = 30) -> list[Student]:
        return self.select(Student.course == course, Student.grade < limit)

    def get_avg_grade(self, faculty: str) -> float | None:
        # если такого факультета нет, avg вернет None
        query = select(func.avg(Student.grade)).where(
            Student.faculty == faculty
        )
        with self.Session() as session:
            return session.scalar(query)


if __name__ == "__main__":
    # каждый запуск с чистой базы, иначе строки из csv задвоятся
    DB_PATH.unlink(missing_ok=True)
    repo = StudentRepository()
    count = repo.load_csv()
    print(f"Загружено строк: {count}")

    faculties = sorted({s.faculty for s in repo.select()})

    print("\nСтуденты по факультетам:")
    for faculty in faculties:
        names = repo.get_by_faculty(faculty)
        names = ", ".join(f"{last} {first}" for last, first in names)
        print(f"  {faculty}: {names}")

    print("\nУникальные курсы:")
    for course in repo.get_courses():
        print(f"  {course}")

    course = "Мат. Анализ"
    print(f"\nОценка ниже 30 по курсу {course}:")
    for s in repo.get_low_grades(course):
        print(f"  {s}")

    print("\nСредний балл по факультетам:")
    for faculty in faculties:
        print(f"  {faculty}: {repo.get_avg_grade(faculty):.2f}")
