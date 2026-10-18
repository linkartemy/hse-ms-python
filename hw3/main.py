import asyncio
import time

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, model_validator


class CalculateRequest(BaseModel):
    numbers: list[int | float]
    delays: list[int | float]

    @model_validator(mode="after")
    def check_input(self):
        if len(self.numbers) == 0:
            raise ValueError("Список numbers пустой")
        if any(delay < 0 for delay in self.delays):
            raise ValueError("Задержка не может быть отрицательной")
        if len(self.numbers) != len(self.delays):
            raise ValueError("В numbers и delays должно быть одинаковое количество элементов")
        return self


class TaskResult(BaseModel):
    number: int | float
    square: int | float
    time: float
    delay: int | float


class CalculateResponse(BaseModel):
    total_time: float
    parallel_faster_than_sequential: bool
    results: list[TaskResult]


app = FastAPI(title="ДЗ 3 по питону")


async def calculate_square(number, delay) -> TaskResult:
    start = time.perf_counter()
    await asyncio.sleep(delay)

    elapsed = time.perf_counter() - start
    return TaskResult(number=number, square=number**2, delay=delay, time=round(elapsed, 2))


@app.post("/calculate/")
async def calculate(request: CalculateRequest) -> CalculateResponse:
    start = time.perf_counter()
    tasks = []
    for number, delay in zip(request.numbers, request.delays):
        tasks.append(calculate_square(number, delay))

    results = await asyncio.gather(*tasks)
    total_time = time.perf_counter() - start

    sequential_time = sum(result.time for result in results)

    return CalculateResponse(
        results=results,
        total_time=round(total_time, 2),
        parallel_faster_than_sequential=total_time < sequential_time,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
