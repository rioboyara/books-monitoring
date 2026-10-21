"""Веб-обёртка: записка на главной, проверка ссылок на /check, API на /api/check."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from checker import check_many, load_catalog

HERE = Path(__file__).parent
MAX_URLS = 30

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


@app.post("/api/check")
def api_check(data: Urls):
    urls = list(dict.fromkeys(u.strip() for u in data.urls if u.strip()))
    if not urls or len(urls) > MAX_URLS or not all(u.startswith(("http://", "https://")) for u in urls):
        raise HTTPException(400, f"Нужен список от 1 до {MAX_URLS} адресов, каждый с http:// или https://")
    return check_many(urls, catalog)
