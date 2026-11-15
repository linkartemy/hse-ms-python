import csv
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

HERE = Path(__file__).parent
DB_PATH = HERE / "students.db"
CSV_PATH = HERE / "students.csv"

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
    __tablename__ = "students"
    __table_args__ = {"sqlite_autoincrement": True}

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

    def get(self, student_id: int) -> Student | None:
        with self.Session() as session:
            return session.get(Student, student_id)

    def update(self, student_id: int, **fields) -> Student | None:
        # меняем только переданные поля, если записи нет - None
        with self.Session() as session:
            student = session.get(Student, student_id)
            if student is None:
                return None
            for name, value in fields.items():
                setattr(student, name, value)
            session.commit()
            return student

    def delete(self, student_id: int) -> bool:
        with self.Session() as session:
            student = session.get(Student, student_id)
            if student is None:
                return False
            session.delete(student)
            session.commit()
            return True

    def count(self) -> int:
        with self.Session() as session:
            return session.scalar(select(func.count(Student.id)))

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

    # методы из ДЗ 6 оставил как были
    def get_by_faculty(self, faculty: str) -> list[tuple[str, str]]:
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
        query = select(func.avg(Student.grade)).where(
            Student.faculty == faculty
        )
        with self.Session() as session:
            return session.scalar(query)


class StudentIn(BaseModel):
    # пробелы по краям срезаем, так что строка из пробелов не пройдет
    model_config = ConfigDict(str_strip_whitespace=True)

    last_name: str = Field(min_length=1)
    first_name: str = Field(min_length=1)
    faculty: str = Field(min_length=1)
    course: str = Field(min_length=1)
    # в csv оценки от 0 до 100
    # strict, чтобы true/false не превращались в 1/0
    grade: int = Field(ge=0, le=100, strict=True)


class StudentUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # для PATCH все поля необязательные, меняем только то, что прислали
    last_name: str | None = Field(default=None, min_length=1)
    first_name: str | None = Field(default=None, min_length=1)
    faculty: str | None = Field(default=None, min_length=1)
    course: str | None = Field(default=None, min_length=1)
    grade: int | None = Field(
        default=None, ge=0, le=100, strict=True
    )


class StudentOut(StudentIn):
    model_config = ConfigDict(from_attributes=True)

    id: int


# id больше 2^63 - 1 sqlite не хранит, без ограничения был бы 500
StudentId = Annotated[int, PathParam(ge=1, le=2**63 - 1)]


# репозиторий создается при первом обращении, а не при импорте, чтобы
# тесты не создавали лишний students.db
@lru_cache
def get_repo() -> StudentRepository:
    return StudentRepository()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # csv грузим только в пустую базу, иначе при каждом перезапуске
    # строки бы дублировались
    repo = get_repo()
    if repo.count() == 0:
        repo.load_csv()
    yield


app = FastAPI(title="ДЗ 7. CRUD для студентов", lifespan=lifespan)


def get_or_404(repo: StudentRepository, student_id: int) -> Student:
    student = repo.get(student_id)
    if student is None:
        raise HTTPException(404, f"Запись {student_id} не найдена")
    return student


@app.post("/students/", status_code=201, response_model=StudentOut)
def create_student(data: StudentIn, repo=Depends(get_repo)):
    student = Student(**data.model_dump())
    repo.insert([student])
    return student


@app.get("/students/", response_model=list[StudentOut])
def list_students(
    faculty: str | None = None,
    course: str | None = None,
    repo=Depends(get_repo),
):
    where = []
    if faculty is not None:
        where.append(Student.faculty == faculty)
    if course is not None:
        where.append(Student.course == course)
    return repo.select(*where)


@app.get("/students/{student_id}", response_model=StudentOut)
def read_student(student_id: StudentId, repo=Depends(get_repo)):
    return get_or_404(repo, student_id)


@app.put("/students/{student_id}", response_model=StudentOut)
def replace_student(
    student_id: StudentId, data: StudentIn, repo=Depends(get_repo)
):
    student = repo.update(student_id, **data.model_dump())
    if student is None:
        raise HTTPException(404, f"Запись {student_id} не найдена")
    return student


@app.patch("/students/{student_id}", response_model=StudentOut)
def update_student(
    student_id: StudentId, data: StudentUpdate, repo=Depends(get_repo)
):
    fields = data.model_dump(exclude_unset=True)
    # явный null для обязательного поля не пропускаем
    if any(value is None for value in fields.values()):
        raise HTTPException(422, "Поля не могут быть null")
    student = repo.update(student_id, **fields)
    if student is None:
        raise HTTPException(404, f"Запись {student_id} не найдена")
    return student


@app.delete("/students/{student_id}", status_code=204)
def delete_student(student_id: StudentId, repo=Depends(get_repo)):
    if not repo.delete(student_id):
        raise HTTPException(404, f"Запись {student_id} не найдена")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
