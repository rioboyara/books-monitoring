"""Контрольные сценарии парсинга и сопоставления. Ожидаемые ответы заданы независимо от правил."""
import os
from unittest.mock import patch

from requests import Response
from checker import check
from normalize import key, loose

CATALOG = [
    {"book_id": "B1", "author": "Алекс Рудин", "title": "Егерь-3"},
    {"book_id": "B2", "author": "Helga Duran", "title": "Босс. Будет жарко"},
    {"book_id": "B3", "author": "Мария Соколова", "title": "Тихий город"},
    {"book_id": "B4", "author": "Евгений Нетт", "title": "Директива: Сохранить!"},
]
for b in CATALOG:
    b.update(tkey=key(b["title"]), twords=loose(b["title"]).split(), awords=loose(b["author"]).split())


def html(title, content):
    return f'<html><head><title>{title}</title></head><body><h1>{title}</h1>{content}</body></html>'


CASES = [
    ("Книга, автор и FB2", html("Егерь-3", 'Автор: Алекс Рудин <a href="/download/fb2">Скачать FB2</a>'), 200, 1, "B1"),
    ("Нет автора", html("Егерь-3", '<a href="/download/fb2">Скачать FB2</a>'), 200, 2, None),
    ("Другой автор", html("Егерь-3", 'Автор: Иван Петров <a href="/download/fb2">Скачать FB2</a>'), 200, 2, None),
    ("Автор только в рекомендациях", html("Егерь-3", 'Автор: Иван Петров Рекомендуем: Алекс Рудин <a href="/download/fb2">Скачать FB2</a>'), 200, 2, None),
    ("Нет автора, имя в рекомендациях", html("Егерь-3", 'Рекомендуем: Алекс Рудин <a href="/download/fb2">Скачать FB2</a>'), 200, 2, None),
    ("Другой том", html("Егерь-4", 'Автор: Алекс Рудин <a href="/download/fb2">Скачать FB2</a>'), 200, 0, None),
    ("Похожие разные имена", html("Тихий город", 'Автор: Марина Соколова <a href="/download/fb2">Скачать FB2</a>'), 200, 2, None),
    ("Автор в микроразметке", html("Тихий город", '<span itemprop="author">Мария Соколова</span><a href="/download/fb2">Скачать FB2</a>'), 200, 1, "B3"),
    ("Транслитерация", html("Босс. Будет жарко", 'Автор: Хельга Дюран <a href="/download/fb2">Скачать FB2</a>'), 200, 1, "B2"),
    ("Книга в сборнике", html("Евгений Нетт: сборник произведений", 'Директива: Сохранить! <a href="/book.torrent">Скачать торрент</a>'), 200, 1, "B4"),
    ("Кнопка через data-href", html("Егерь-3", 'Автор: Алекс Рудин <div data-href="/download/fb2">Скачать FB2</div>'), 200, 1, "B1"),
    ("Только магазин", html("Егерь-3", 'Автор: Алекс Рудин <a href="https://litres.ru/book">Купить на Литрес</a>'), 200, 0, "B1"),
    ("Магазин и чтение", html("Егерь-3", 'Автор: Алекс Рудин <a href="/read/1">Читать онлайн</a><a href="https://litres.ru/book">Купить на Литрес</a>'), 200, 2, "B1"),
    ("Страница ошибки с кодом 200", html("Ошибка 404", 'Страница не найдена'), 200, 0, None),
    ("Запрет доступа", html("Access denied", 'Нет доступа'), 403, 2, None),
]


def run():
    failures = []
    for name, body, http, status, book in CASES:
        # Наполнение не несёт метаданных, оно отделяет короткую страницу от антибот-заглушки.
        body = body.replace('</body>', ('<p>Описание произведения.</p>' * 30) + '</body>')
        response = Response()
        response.status_code = http
        response.url = 'https://example.org/book'
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        with patch.dict(os.environ, {"API429_KEY": ""}), patch('checker.fetch', return_value=(response, body.encode())):
            result = check(response.url, CATALOG)
        actual = (result['status'], (result['match'] or {}).get('book_id'))
        if actual != (status, book):
            failures.append((name, (status, book), actual))
    assert not failures, failures
    print(f'Контрольные сценарии: {len(CASES)}/{len(CASES)}')


if __name__ == '__main__':
    run()
