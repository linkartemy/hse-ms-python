import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, EmailStr, field_validator, model_validator

APPEALS_DIR = Path("appeals")

Reason = Literal["нет доступа к сети", "не работает телефон", "не приходят письма"]


class Appeal(BaseModel):
    last_name: str
    first_name: str
    birth_date: date
    phone: str
    email: EmailStr
    reasons: list[Reason]
    problem_detected_at: datetime

    @field_validator("last_name", "first_name")
    @classmethod
    def check_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Пустое значение")
        if not v[0].isupper() or v[1:] != v[1:].lower():
            raise ValueError("Должно начинаться с заглавной буквы, дальше строчные")
        # дефис тоже спецсимвол, поэтому двойные фамилии не проходят
        if not re.fullmatch(r"[А-Яа-яЁё]+", v):
            raise ValueError("Только русские буквы, без цифр, пробелов и спецсимволов")
        return v

    @field_validator("birth_date")
    @classmethod
    def check_birth_date(cls, v):
        if v > date.today():
            raise ValueError("Дата рождения не может быть в будущем")
        if v.year < 1900:
            raise ValueError("Слишком старая дата рождения")
        return v

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v):
        # убираем пробелы, скобки и дефисы, чтобы можно было писать условно 8 (912) 345-67-89
        phone = re.sub(r"[\s()-]", "", v)
        if phone.startswith("8"):
            phone = "+7" + phone[1:]
        # [0-9], а не \d: \d пропускает любые цифры из юникода, например ９
        if not re.fullmatch(r"\+7[0-9]{10}", phone):
            raise ValueError("Нужен российский номер: +7XXXXXXXXXX или 8XXXXXXXXXX")
        return phone

    @field_validator("reasons")
    @classmethod
    def check_reasons(cls, v):
        if not v:
            raise ValueError("Нужна хотя бы одна причина обращения")
        if len(v) != len(set(v)):
            raise ValueError("Причины повторяются")
        return v

    @field_validator("problem_detected_at")
    @classmethod
    def check_detected_at(cls, v: datetime):
        # если время пришло с таймзоной, now() берем в ней же, иначе их нельзя сравнить
        if v > datetime.now(v.tzinfo):
            raise ValueError("Проблема не может быть обнаружена в будущем")
        return v

    @model_validator(mode="after")
    def check_dates(self):
        if self.problem_detected_at.date() < self.birth_date:
            raise ValueError("Проблема обнаружена раньше, чем абонент родился")
        return self


app = FastAPI(title="ДЗ 4. Обращения абонентов")


@app.post("/appeals/")
def create_appeal(appeal: Appeal):
    APPEALS_DIR.mkdir(exist_ok=True)
    path = APPEALS_DIR / f"{appeal.last_name}_{uuid.uuid4().hex[:8]}.json"
    path.write_text(appeal.model_dump_json(indent=2), encoding="utf-8")
    return {"status": "ok", "file": str(path)}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
