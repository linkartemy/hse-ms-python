import csv
import hashlib
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
import uvicorn
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
)
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import ForeignKey, create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

HERE = Path(__file__).parent
DB_PATH = HERE / "students.db"
CSV_PATH = HERE / "students.csv"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# сколько секунд ответ живет в кеше
CACHE_TTL = 60

log = logging.getLogger("hw10")

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

    def delete_many(self, ids: list[int]) -> int:
        # возвращает, сколько записей реально удалили
        with self.Session() as session:
            result = session.execute(
                delete(Student).where(Student.id.in_(ids))
            )
            session.commit()
            return result.rowcount

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
                # те же проверки, что и в api: оценка 0-100, строки
                # не пустые. Если строка плохая, не грузим весь файл
                checked = StudentIn(**data).model_dump()
                students.append(Student(**checked))
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


class Cache:
    # обертка над redis: если redis недоступен, приложение работает
    # дальше без кеша, просто каждый раз идет в базу
    PREFIX = "students:"
    # после ошибки столько секунд не трогаем redis, чтобы запросы
    # не ждали таймаут каждый раз
    # столько же, сколько живет кеш: пока redis лежал, сбросить кеш
    # не могли, и старые ключи как раз успеют истечь
    PAUSE = CACHE_TTL

    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self.down_until = 0.0

    def call(self, method: str, *args, **kwargs):
        if time.monotonic() < self.down_until:
            return None
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except redis.RedisError as e:
            log.warning("redis недоступен, работаем без кеша: %s", e)
            self.down_until = time.monotonic() + self.PAUSE
            return None

    def get(self, key: str):
        data = self.call("get", self.PREFIX + key)
        return None if data is None else json.loads(data)

    def set(self, key: str, value) -> None:
        data = json.dumps(value, ensure_ascii=False)
        self.call("set", self.PREFIX + key, data, ex=CACHE_TTL)

    def clear(self) -> None:
        # после любого изменения данных старые ответы уже неверные,
        # проще сбросить весь кеш студентов
        if time.monotonic() < self.down_until:
            return
        try:
            # scan, а не keys: keys на большой базе блокирует redis
            keys = list(self.client.scan_iter(self.PREFIX + "*"))
            if keys:
                self.client.delete(*keys)
        except redis.RedisError as e:
            log.warning("redis недоступен, работаем без кеша: %s", e)
            self.down_until = time.monotonic() + self.PAUSE


def check_csv(path: Path) -> None:
    # до запуска фоновой задачи проверяем, что файл есть и колонки те,
    # иначе пользователь получил бы 202, а загрузка тихо упала бы
    if not path.is_file():
        raise HTTPException(404, f"Файл {path} не найден")
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
    except (OSError, UnicodeDecodeError):
        raise HTTPException(422, "Не получилось прочитать файл как csv")
    missing = set(COLUMNS) - {h.strip() for h in header}
    if missing:
        raise HTTPException(
            422, f"В csv нет колонок: {', '.join(sorted(missing))}"
        )


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


class ImportIn(BaseModel):
    # путь к csv на машине, где запущен сервер
    path: str = Field(min_length=1)


# id больше 2^63 - 1 sqlite не хранит, без ограничения был бы 500
MAX_ID = 2**63 - 1


class BulkDeleteIn(BaseModel):
    ids: list[Annotated[int, Field(ge=1, le=MAX_ID, strict=True)]] = Field(
        min_length=1, max_length=1000
    )


class TaskOut(BaseModel):
    detail: str


StudentId = Annotated[int, PathParam(ge=1, le=MAX_ID)]


# репозитории создаются при первом обращении, а не при импорте, чтобы
# тесты не создавали лишний students.db
@lru_cache
def get_repo() -> StudentRepository:
    return StudentRepository()


@lru_cache
def get_auth() -> AuthRepository:
    return AuthRepository()


@lru_cache
def get_cache() -> Cache:
    # короткий таймаут, чтобы без redis запросы не висели
    client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
        # без повторных попыток, иначе каждый запрос ждал бы дольше
        retry=Retry(NoBackoff(), 0),
    )
    return Cache(client)


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
def create_student(
    data: StudentIn, repo=Depends(get_repo), cache=Depends(get_cache)
):
    student = Student(**data.model_dump())
    repo.insert([student])
    cache.clear()
    return student


def run_import(repo: StudentRepository, cache: Cache, path: Path) -> None:
    # фоновая задача: выполняется уже после того, как клиент получил 202
    try:
        n = repo.load_csv(path)
    except Exception:
        # ответ уже ушел, так что ошибку можно только залогировать
        log.exception("Загрузка %s не удалась", path)
        return
    cache.clear()
    log.info("Из %s загружено записей: %s", path, n)


def run_bulk_delete(
    repo: StudentRepository, cache: Cache, ids: list[int]
) -> None:
    try:
        n = repo.delete_many(ids)
    except Exception:
        log.exception("Удаление %s записей не удалось", len(ids))
        return
    cache.clear()
    log.info("Удалено записей: %s из %s", n, len(ids))


@students_router.post(
    "/import",
    status_code=202,
    response_model=TaskOut,
    dependencies=[Depends(editor)],
)
def import_csv(
    data: ImportIn,
    tasks: BackgroundTasks,
    repo=Depends(get_repo),
    cache=Depends(get_cache),
):
    path = Path(data.path)
    check_csv(path)
    tasks.add_task(run_import, repo, cache, path)
    return TaskOut(detail=f"Загрузка {path} запущена в фоне")


@students_router.post(
    "/bulk-delete",
    status_code=202,
    response_model=TaskOut,
    dependencies=[Depends(editor)],
)
def bulk_delete(
    data: BulkDeleteIn,
    tasks: BackgroundTasks,
    repo=Depends(get_repo),
    cache=Depends(get_cache),
):
    # дубли убираем, порядок не важен
    ids = sorted(set(data.ids))
    tasks.add_task(run_bulk_delete, repo, cache, ids)
    return TaskOut(detail=f"Удаление {len(ids)} записей запущено в фоне")


@students_router.get("/", response_model=list[StudentOut])
def list_students(
    faculty: str | None = None,
    course: str | None = None,
    repo=Depends(get_repo),
    cache=Depends(get_cache),
):
    # в ключ кладем параметры фильтра, у каждого фильтра свой кеш
    key = "list:" + json.dumps([faculty, course], ensure_ascii=False)
    cached = cache.get(key)
    if cached is not None:
        return cached
    where = []
    if faculty is not None:
        where.append(Student.faculty == faculty)
    if course is not None:
        where.append(Student.course == course)
    result = [
        StudentOut.model_validate(s).model_dump()
        for s in repo.select(*where)
    ]
    cache.set(key, result)
    return result


@students_router.get("/{student_id}", response_model=StudentOut)
def read_student(
    student_id: StudentId, repo=Depends(get_repo), cache=Depends(get_cache)
):
    key = f"one:{student_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    # 404 не кешируем, запись может появиться
    result = StudentOut.model_validate(
        get_or_404(repo, student_id)
    ).model_dump()
    cache.set(key, result)
    return result


@students_router.put(
    "/{student_id}",
    response_model=StudentOut,
    dependencies=[Depends(editor)],
)
def replace_student(
    student_id: StudentId,
    data: StudentIn,
    repo=Depends(get_repo),
    cache=Depends(get_cache),
):
    student = repo.update(student_id, **data.model_dump())
    if student is None:
        raise HTTPException(404, f"Запись {student_id} не найдена")
    cache.clear()
    return student


@students_router.patch(
    "/{student_id}",
    response_model=StudentOut,
    dependencies=[Depends(editor)],
)
def update_student(
    student_id: StudentId,
    data: StudentUpdate,
    repo=Depends(get_repo),
    cache=Depends(get_cache),
):
    fields = data.model_dump(exclude_unset=True)
    # явный null для обязательного поля не пропускаем
    if any(value is None for value in fields.values()):
        raise HTTPException(422, "Поля не могут быть null")
    student = repo.update(student_id, **fields)
    if student is None:
        raise HTTPException(404, f"Запись {student_id} не найдена")
    cache.clear()
    return student


@students_router.delete(
    "/{student_id}", status_code=204, dependencies=[Depends(editor)]
)
def delete_student(
    student_id: StudentId, repo=Depends(get_repo), cache=Depends(get_cache)
):
    if not repo.delete(student_id):
        raise HTTPException(404, f"Запись {student_id} не найдена")
    cache.clear()


app = FastAPI(title="ДЗ 10. Фоновые задачи и кеш", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(students_router)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8000)
