import csv
import hashlib
import secrets
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import ForeignKey, create_engine, func, select
from sqlalchemy.exc import IntegrityError
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    salt: Mapped[str]
    password_hash: Mapped[str]
    read_only: Mapped[bool] = mapped_column(default=False)


class Token(Base):
    __tablename__ = "tokens"

    token: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


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


def hash_password(password: str, salt: str) -> str:
    # pbkdf2 - функция формирования ключа, медленная специально,
    # чтобы перебирать пароли по украденной базе было долго
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    )
    return digest.hex()


class AuthRepository:
    def __init__(self, url: str = f"sqlite:///{DB_PATH}") -> None:
        self.engine = create_engine(url)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def register(
        self, username: str, password: str, read_only: bool = False
    ) -> User | None:
        # None, если такое имя уже занято
        with self.Session() as session:
            taken = session.scalar(
                select(User).where(User.username == username)
            )
            if taken is not None:
                return None
            salt = secrets.token_hex(16)
            user = User(
                username=username,
                salt=salt,
                password_hash=hash_password(password, salt),
                read_only=read_only,
            )
            session.add(user)
            try:
                session.commit()
            except IntegrityError:
                # два одновременных запроса с одним именем
                return None
            return user

    def login(self, username: str, password: str) -> str | None:
        # выдаем новый токен, если логин и пароль верные
        with self.Session() as session:
            user = session.scalar(
                select(User).where(User.username == username)
            )
            if user is None:
                return None
            # compare_digest сравнивает за одинаковое время,
            # по скорости ответа нельзя угадать часть хеша
            if not secrets.compare_digest(
                hash_password(password, user.salt), user.password_hash
            ):
                return None
            token = secrets.token_urlsafe(32)
            session.add(Token(token=token, user_id=user.id))
            session.commit()
            return token

    def user_by_token(self, token: str) -> User | None:
        with self.Session() as session:
            query = select(User).join(Token).where(Token.token == token)
            return session.scalar(query)

    def logout(self, token: str) -> None:
        with self.Session() as session:
            row = session.get(Token, token)
            if row is not None:
                session.delete(row)
                session.commit()


class StudentIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    last_name: str = Field(min_length=1)
    first_name: str = Field(min_length=1)
    faculty: str = Field(min_length=1)
    course: str = Field(min_length=1)
    grade: int = Field(ge=0, le=100, strict=True)


class StudentUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

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


class Credentials(BaseModel):
    username: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=3, max_length=50),
    ]
    password: str = Field(min_length=6, max_length=100)


class RegisterIn(Credentials):
    read_only: bool = False


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    read_only: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


# id больше 2^63 - 1 sqlite не хранит, без ограничения был бы 500
StudentId = Annotated[int, PathParam(ge=1, le=2**63 - 1)]


# репозитории создаются при первом обращении, а не при импорте, чтобы
# тесты не создавали лишний students.db
@lru_cache
def get_repo() -> StudentRepository:
    return StudentRepository()


@lru_cache
def get_auth() -> AuthRepository:
    return AuthRepository()


# auto_error=False, чтобы на запрос без токена отвечать 401 самим
bearer = HTTPBearer(auto_error=False)


def get_token(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    if creds is None:
        raise HTTPException(
            401,
            "Нужно войти: /auth/login",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return creds.credentials


def current_user(
    token: str = Depends(get_token), auth=Depends(get_auth)
) -> User:
    user = auth.user_by_token(token)
    if user is None:
        raise HTTPException(
            401,
            "Токен неверный или сессия завершена",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def editor(user: User = Depends(current_user)) -> User:
    if user.read_only:
        raise HTTPException(403, "У пользователя права только на чтение")
    return user


@asynccontextmanager
async def lifespan(app: FastAPI):
    # csv грузим только в пустую базу, иначе при каждом перезапуске
    # строки бы дублировались
    repo = get_repo()
    if repo.count() == 0:
        repo.load_csv()
    yield


auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/register", status_code=201, response_model=UserOut)
def register(data: RegisterIn, auth=Depends(get_auth)):
    user = auth.register(data.username, data.password, data.read_only)
    if user is None:
        raise HTTPException(409, "Такой пользователь уже есть")
    return user


@auth_router.post("/login", response_model=TokenOut)
def login(data: Credentials, auth=Depends(get_auth)):
    token = auth.login(data.username, data.password)
    if token is None:
        raise HTTPException(401, "Неверный логин или пароль")
    return TokenOut(access_token=token)


@auth_router.post("/logout", status_code=204)
def logout(
    token: str = Depends(get_token),
    user: User = Depends(current_user),
    auth=Depends(get_auth),
):
    # current_user тут, чтобы с чужим или старым токеном был 401
    auth.logout(token)


students_router = APIRouter(
    prefix="/students",
    tags=["students"],
    dependencies=[Depends(current_user)],
)


def get_or_404(repo: StudentRepository, student_id: int) -> Student:
    student = repo.get(student_id)
    if student is None:
        raise HTTPException(404, f"Запись {student_id} не найдена")
    return student


@students_router.post(
    "/",
    status_code=201,
    response_model=StudentOut,
    dependencies=[Depends(editor)],
)
def create_student(data: StudentIn, repo=Depends(get_repo)):
    student = Student(**data.model_dump())
    repo.insert([student])
    return student


@students_router.get("/", response_model=list[StudentOut])
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


@students_router.get("/{student_id}", response_model=StudentOut)
def read_student(student_id: StudentId, repo=Depends(get_repo)):
    return get_or_404(repo, student_id)


@students_router.put(
    "/{student_id}",
    response_model=StudentOut,
    dependencies=[Depends(editor)],
)
def replace_student(
    student_id: StudentId, data: StudentIn, repo=Depends(get_repo)
):
    student = repo.update(student_id, **data.model_dump())
    if student is None:
        raise HTTPException(404, f"Запись {student_id} не найдена")
    return student


@students_router.patch(
    "/{student_id}",
    response_model=StudentOut,
    dependencies=[Depends(editor)],
)
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


@students_router.delete(
    "/{student_id}", status_code=204, dependencies=[Depends(editor)]
)
def delete_student(student_id: StudentId, repo=Depends(get_repo)):
    if not repo.delete(student_id):
        raise HTTPException(404, f"Запись {student_id} не найдена")


app = FastAPI(title="ДЗ 9. Аутентификация", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(students_router)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
