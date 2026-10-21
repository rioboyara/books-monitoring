"""Веб-обёртка: записка на главной, проверка ссылок на /check, обработка своей таблицы на /table."""
import io
import json
import zipfile
from pathlib import Path
from threading import BoundedSemaphore, Thread
from queue import Queue, Empty

import openpyxl
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from checker import check_many, load_catalog
from normalize import build
from ai_review import suggest_rows

HERE = Path(__file__).parent
MAX_URLS = 30
MAX_TABLE_BYTES = 5_000_000
MAX_TABLE_ROWS = 3000
MAX_UNPACKED_BYTES = 50_000_000
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
busy = BoundedSemaphore(2)

app = FastAPI(docs_url=None, redoc_url=None)
catalog = load_catalog(next((HERE / "input").glob("*.xlsx")))
app.mount("/files", StaticFiles(directory=HERE / "out"), name="files")
app.mount("/assets", StaticFiles(directory=HERE / "web"), name="assets")


class Urls(BaseModel):
    urls: list[str]


@app.get("/")
def note():
    return FileResponse(HERE / "web" / "index.html")


@app.get("/check")
def check_page():
    return FileResponse(HERE / "web" / "check.html")


@app.get("/table")
def table_page():
    return FileResponse(HERE / "web" / "table.html")


@app.post("/api/check")
def api_check(data: Urls, stream: bool = False):
    urls = list(dict.fromkeys(u.strip() for u in data.urls if u.strip()))
    if not urls or len(urls) > MAX_URLS or not all(u.startswith(("http://", "https://")) for u in urls):
        raise HTTPException(400, f"Нужен список от 1 до {MAX_URLS} адресов, каждый с http:// или https://")
    # доступ к сайту закрыт логином в nginx, здесь только ограничение одновременных проверок
    if not busy.acquire(blocking=False):
        raise HTTPException(429, "Сервер занят другой проверкой, попробуйте через минуту")
    if stream:
        events = Queue()
        def work():
            try:
                check_many(urls, catalog, on_result=lambda result: events.put({'result': result}))
            except Exception:
                events.put({'error': 'Проверка прервалась. Уже полученные результаты сохранены.'})
            finally:
                events.put({'done': True})
                busy.release()
        try:
            Thread(target=work, daemon=True).start()
        except Exception:
            busy.release()
            raise
        def output():
            yield json.dumps({'total': len(urls)}) + '\n'
            while True:
                try:
                    event = events.get(timeout=2)
                except Empty:
                    yield '\n'
                    continue
                yield json.dumps(event, ensure_ascii=False) + '\n'
                if event.get('done'):
                    break
        return StreamingResponse(output(), media_type='application/x-ndjson',
                                 headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})
    try:
        return check_many(urls, catalog)
    finally:
        busy.release()


@app.post("/api/table")
async def api_table(request: Request):
    """Своя версия исходной таблицы, например с новыми строками: возвращает обработанную таблицу.
    Для адресов, которые уже проверялись, подставляется результат проверки страницы."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_TABLE_BYTES:
            raise HTTPException(400, "Нужен файл xlsx до 5 МБ")
    if not body or len(body) > MAX_TABLE_BYTES:
        raise HTTPException(400, "Нужен файл xlsx до 5 МБ")
    # разбор xlsx занимает процессор, поэтому в отдельном потоке и под тем же ограничением, что и проверка ссылок
    return await run_in_threadpool(make_table, body, request.headers.get("X-AI-Review") == "1")


def make_table(body, use_ai=False):
    bad_file = HTTPException(400, 'Не удалось прочитать файл. Нужен xlsx с листом "Задание 1" в формате исходной таблицы')
    try:
        # xlsx это zip: маленький файл может распаковаться в гигабайты, поэтому смотрим размер до разбора
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            unpacked = sum(i.file_size for i in archive.infolist())
            if len(archive.infolist()) > 200:
                raise HTTPException(400, "Слишком сложная структура файла")
    except zipfile.BadZipFile:
        raise bad_file
    if unpacked > MAX_UNPACKED_BYTES:
        raise HTTPException(400, "Файл слишком большой после распаковки")
    if not busy.acquire(blocking=False):
        raise HTTPException(429, "Сервер занят другой задачей, попробуйте через минуту")
    try:
        try:
            source = openpyxl.load_workbook(io.BytesIO(body), read_only=True, keep_links=False)
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            try:
                for name in ("Задание 1", "Задание 2"):
                    if name not in source.sheetnames:
                        continue
                    ws = source[name]
                    if (ws.max_row or 0) > MAX_TABLE_ROWS or (ws.max_column or 0) > 30:
                        raise bad_file
                    ws.reset_dimensions()
                    dest = wb.create_sheet(name)
                    for number, row in enumerate(ws.iter_rows(max_col=31, values_only=True), 1):
                        if number > MAX_TABLE_ROWS or row[30] is not None:
                            raise bad_file
                        dest.append(row[:30])
            finally:
                source.close()
            sheets = wb.worksheets
        except Exception:  # чужой файл может быть любым, отвечаем понятной ошибкой
            raise bad_file
        if "Задание 1" not in wb.sheetnames:
            raise bad_file
        if any(ws.max_row > MAX_TABLE_ROWS or ws.max_column > 30 for ws in sheets):
            raise HTTPException(400, f"Не больше {MAX_TABLE_ROWS} строк и 30 колонок на листе")
        pages_file = HERE / "out" / "pages.json"
        pages = {p["url"]: p for p in json.loads(pages_file.read_text(encoding="utf-8"))} if pages_file.exists() else {}
        out = io.BytesIO()
        try:
            build(wb, out, pages, advise=suggest_rows if use_ai else None)
        except Exception:
            raise bad_file
    finally:
        busy.release()
    return Response(out.getvalue(), media_type=XLSX, headers={"Content-Disposition": 'attachment; filename="result.xlsx"'})
