"""Часть 2. Проверка страниц.

Для каждого URL: ответ сайта, данные со страницы, книга из каталога, статус 0/1/2 и причина.
Каталог собирается из обоих листов исходной таблицы.

Запуск: python checker.py input/<таблица>.xlsx urls.txt out/pages.json
        python checker.py input/<таблица>.xlsx --all out/pages.json   (все ссылки из таблицы)
"""
import io
import ipaddress
import itertools
import json
import re
import random
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from urllib3.util import connection as urllib3_connection

from normalize import author_matches, book_id, clean, key, load_rows, loose, split_author
from ai_review import review

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_BYTES = 20_000_000
TIMEOUT = (10, 25)
DEADLINE = 40  # секунд на один адрес вместе с редиректами и повторами
_host_lock = Lock()
_next_request = {}


def pace(url, until):
    host = re.sub(r"^(www|s\d+)\.", "", urlsplit(url).hostname or "")
    while True:
        with _host_lock:
            now = time.monotonic()
            wait = max(0, _next_request.get(host, 0) - now)
            if now + wait >= until:
                raise requests.Timeout()
            if not wait:
                _next_request[host] = now + random.uniform(2, 2.5)
                return
        time.sleep(min(wait, max(0, until - time.monotonic())))


def retry_delay(value, attempt):
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return 2 ** (attempt + 1)


def defer(url, delay):
    host = re.sub(r"^(www|s\d+)\.", "", urlsplit(url).hostname or "")
    with _host_lock:
        _next_request[host] = max(_next_request.get(host, 0), time.monotonic() + delay)

FORMATS = re.compile(r"\b(fb2|epub|pdf|txt|rtf|mobi|djvu|zip|rar|mp3|m4b)\b", re.I)
DOWNLOAD = re.compile(r"скача|download|/dl/|/get", re.I)
READER = re.compile(r"читать (онлайн|книгу|полностью)|read online|/read|reader|chitat", re.I)
AUDIO = re.compile(r"слушать онлайн|\.mp3\b|\.m4b\b|\.m3u8\b", re.I)
TORRENT = re.compile(r"torrent|торрент|magnet:", re.I)
FILE_HOSTS = re.compile(r"turbobit|trbt\.cc|hitfile|htfl\.net|nitroflare|rapidgator|katfile|mega\.nz|"
                        r"disk\.yandex|cloud\.mail\.ru|drive\.google|dropbox|depositfiles", re.I)
HIDDEN = re.compile(r"зарегистрируйтесь\s*,?\s*чтобы (посмотреть|увидеть)|скрытое содержимое", re.I)
STORE = re.compile(r"лит\s?рес|litres\.ru", re.I)
# "Правообладателям" есть в подвале почти каждого сайта, поэтому ищем только явные фразы об удалении
REMOVED = re.compile(r"удален[аоы]? по (требованию|жалобе)|доступ к (книге|произведению) (закрыт|ограничен)", re.I)
# читалка прямо на странице: выбор шрифта, ночной режим, листание
READER_PAGE = re.compile(r"шрифт|times new roman|ночной режим|страница \d+ из \d+|читать (далее|следующую)|глава (1|i|первая)\b", re.I)
# "Автор: Оксана Алексаева Серия: ..." берём до трёх слов с заглавной, служебные подписи отсекаем
AUTHOR_LABEL = re.compile(r"(?i:автор|author)\s*:\s*((?:[A-ZА-ЯЁ][\w.'-]*,?\s*){1,3})")
LABEL_WORDS = {"Серия", "Серии", "Жанр", "Жанры", "Год", "Читает", "Чтец", "Размер", "Язык", "Возраст", "Страниц",
               "Формат", "Добавлено", "Издательство", "Другие", "Рейтинг", "Перевод", "Переводчик", "Цикл",
               "Читать", "Скачать", "Слушать", "Купить", "Описание", "Аннотация", "Название", "Статус",
               "Возрастное", "Ограничение", "Издатель", "Исполнитель", "Озвучка", "Опубликовано"}
NOT_FOUND = re.compile(r"страниц[аы] не найден|ошибка 404|404 not found|page not found", re.I)


def load_catalog(path):
    books = {}
    for sheet in ("Задание 1", "Задание 2"):
        for r in load_rows(path, sheet):
            bid = book_id(r["Автор"], r["Название"])
            if bid in books:
                continue
            author, title = split_author(r["Автор"])[0], clean(r["Название"])
            books[bid] = {"book_id": bid, "author": author, "title": title, "tkey": key(title),
                          "twords": [w for w in loose(title).split() if len(w) > 2],
                          "awords": [w for w in loose(author).split() if len(w) > 2]}
    return list(books.values())


def js_cookie(html):
    """Простая анти-бот заглушка: a='x';b='y';document.cookie='name='+a+b+';path=/';location.reload()."""
    m = re.search(r"document\.cookie\s*=\s*((?:'[^']*'|\"[^\"]*\"|[\w.]+(?:\(\))?|\s*\+\s*)+)", html)
    if not m:
        return None
    variables = dict(re.findall(r"\b(\w+)\s*=\s*'([^']*)'", html))
    parts = re.findall(r"'([^']*)'|\"([^\"]*)\"|([\w.]+(?:\(\))?)", m.group(1))
    name, _, value = "".join(a or b or variables.get(c, "") for a, b, c in parts).split(";")[0].partition("=")
    return (name.strip(), value.strip()) if name.strip() and value.strip() else None


PRIVATE = "адрес во внутренней сети, не проверяем"
_connect = urllib3_connection.create_connection


def public_connection(address, *args, **kwargs):
    """Проверка запускается на сервере, поэтому во внутреннюю сеть (localhost, 10.x, 192.168.x) не ходим.
    Адрес проверяется в момент соединения и соединяемся ровно с проверенным IP, так что ни редирект,
    ни повторный DNS-ответ (DNS rebinding) проверку не обходят. Если имя не резолвится, соединения нет."""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not all(ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise OSError(PRIVATE)
    return _connect((infos[0][4][0], port), *args, **kwargs)  # SNI и Host берутся из URL, не из IP


urllib3_connection.create_connection = public_connection


def get(s, url, until):
    """GET с ручными редиректами и общим лимитом времени на адрес."""
    for _ in range(8):
        pace(url, until)
        remaining = until - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout()
        with s.get(url, timeout=(min(TIMEOUT[0], remaining), min(TIMEOUT[1], remaining)), stream=True, allow_redirects=False) as resp:
            if not resp.is_redirect:
                body, size = [], 0
                for chunk in resp.iter_content(65536):
                    body.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise requests.RequestException("Ответ превышает допустимый размер")
                    if time.monotonic() > until:
                        raise requests.Timeout()
                return resp, b"".join(body)
            url = urljoin(url, resp.headers["location"])
        if time.monotonic() > until:
            raise requests.Timeout()
    raise requests.TooManyRedirects()


def fetch(url):
    s = requests.Session()
    s.trust_env = False  # прокси из окружения обошёл бы проверку адреса при соединении
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
                      "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"})
    until = time.monotonic() + DEADLINE
    try:
        for attempt in range(3):
            try:
                resp, body = get(s, url, until)
            except (requests.Timeout, requests.ConnectionError) as error:
                if attempt == 2 or PRIVATE in str(error) or time.monotonic() + 2 ** (attempt + 1) >= until:
                    raise
                defer(url, 2 ** (attempt + 1))
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < 2:
                delay = retry_delay(resp.headers.get("Retry-After"), attempt)
                defer(resp.url, delay)
                if time.monotonic() + delay < until:
                    url = resp.url
                    continue
            cookie = js_cookie(body.decode("utf-8", "replace")) if len(body) < 5000 and attempt < 2 else None
            if not cookie:
                return resp, body
            s.cookies.set(*cookie, domain=urlsplit(resp.url).hostname)
    finally:
        s.close()


def words_in(words, page_words):
    """Слово есть на странице точно или с небольшой разницей в транслитерации (frai/fry, diuran/duran)."""
    def found(w):
        return w in page_words or any(p[0] == w[0] and SequenceMatcher(None, w, p).ratio() >= 0.8
                                      for p in page_words)
    return sum(found(w) for w in words)


def title_in(words, page_words):
    """Все слова названия в заголовке; у длинных названий (6+ слов) сайты часто режут подзаголовок, хватает 3/4."""
    need = len(words) if len(words) < 6 else -(-len(words) * 3 // 4)
    return bool(words) and words_in(words, page_words) >= need


def find_book(heading, text, catalog, author=None):
    """Сначала ищем название (в заголовке, для сборников в тексте), потом обязательно автора."""
    hk, tk = key(heading), key(text)
    head_words = set(loose(heading).split())
    found, title_only = [], []
    for b in catalog:
        # номер тома обязателен: "Егерь-3" не должен совпасть со страницей "Егерь: назад в СССР"
        same_numbers = set(re.findall(r"\d+", b["title"])) <= set(re.findall(r"\d+", heading))
        if b["tkey"] and same_numbers and (b["tkey"] in hk or title_in(b["twords"], head_words)):
            where = "заголовок"
        elif len(b["tkey"]) >= 12 and b["tkey"] in tk and re.search(r"сборник|собрание|collection", heading, re.I):
            where = "текст"
        else:
            continue
        # книга только в тексте (сборник, список) засчитывается, если автор есть в заголовке
        named = author or page_author(text)
        core = re.split(r"похожие книги|рекомендуем|вам может понравиться|другие книги автора", text, flags=re.I)[0]
        observed = heading if where == "текст" else named or heading + " " + core
        if author_matches(b["author"], observed):
            found.append((where != "заголовок", -len(b["tkey"]), b, where))
        elif where == "заголовок":
            title_only.append(b)
    if found:
        if len({f[2]["book_id"] for f in found}) > 1:
            return None, [{**f[2], "ambiguous": True} for f in found]
        _, _, b, where = min(found, key=lambda f: f[:2])
        return {**{k: b[k] for k in ("book_id", "author", "title")}, "where": where}, []
    return None, title_only


def decode(body, ctype):
    """Кодировка из заголовка, иначе utf-8, иначе из meta. Автоопределение bs4 путает кириллицу с macroman,
    если в utf-8 странице попался один битый байт."""
    header = re.search(r"charset=([\w-]+)", ctype)
    meta = re.search(rb"charset=[\"']?([\w-]+)", body[:3000])
    for enc in filter(None, [header and header.group(1), "utf-8"]):
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return body.decode(meta.group(1).decode() if meta else "cp1251", "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def read_page(resp, body, details=None):
    """Заголовок, видимый текст и признаки размещения."""
    signs = {}
    ctype = resp.headers.get("content-type", "").lower()
    if "pdf" in ctype or body[:5] == b"%PDF-":
        pdf = PdfReader(io.BytesIO(body))
        meta = pdf.metadata or {}
        if details is not None:
            details['author'] = clean(meta.get('/Author', ''))[:200]
        text = " ".join(p.extract_text() or "" for p in pdf.pages[:3])
        heading = clean(f"{meta.get('/Title', '')} {meta.get('/Author', '')}") or clean(text)[:200]
        signs["file"] = f"файл PDF на {len(pdf.pages)} стр."
        return heading, clean(text), signs
    if "html" not in ctype:
        return "", "", {"file": f"файл {ctype or 'без типа'}, {len(body) // 1024} КБ"}

    soup = BeautifulSoup(decode(body, ctype), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    og = soup.find("meta", property="og:title")
    parts = [clean(p) for p in (soup.title.get_text() if soup.title else "", soup.h1.get_text(" ") if soup.h1 else "",
                                og.get("content") if og else "") if clean(p)]
    # h1 обычно повторяет title, оставляем только то, что добавляет новое
    heading = " | ".join(p for p in dict.fromkeys(parts) if not any(p != q and p in q for q in parts))
    text = clean(soup.get_text(" "))
    if details is not None:
        author_tag = soup.select_one("meta[name='author'], [itemprop='author'], a[rel='author']")
        if author_tag:
            candidate = clean(author_tag.get("content") or author_tag.get_text(" "))[:200]
            if not re.search(r"\b(admin|administrator|редакция|администратор)\b|^запись от\b", candidate, re.I):
                details["author"] = candidate
    formats, hosts = set(), set()
    # кнопки бывают ссылками, <button> и div с data-href или onclick
    clickable = soup.find_all(["a", "button"]) + soup.find_all(attrs={"data-href": True}) + \
        soup.find_all(attrs={"onclick": True})
    for el in clickable:
        href = el.get("href") or el.get("data-href") or el.get("onclick") or ""
        label = clean(el.get_text(" "))
        both = href + " " + (label if len(label) <= 60 else "")
        if details is not None and href and len(details.setdefault("links", [])) < 30 and (
                DOWNLOAD.search(both) or READER.search(both) or STORE.search(both)):
            details["links"].append({"label": label[:120], "url": urljoin(resp.url, href)[:500]})
        if FILE_HOSTS.search(href):
            hosts.add(urlsplit(href).hostname)
        if TORRENT.search(both):
            signs["torrent"] = "торрент"
        if FORMATS.search(both) and (DOWNLOAD.search(both) or FORMATS.search(href)):
            formats.update(f.lower() for f in FORMATS.findall(both))
        elif DOWNLOAD.search(both):
            signs["download"] = "кнопка скачивания"
        if READER.search(both):
            signs["reader"] = "чтение онлайн"
        if AUDIO.search(href):
            signs["audio"] = "аудиофайлы"
        if STORE.search(both):
            signs["store"] = "ссылки на покупку в Литрес"
    if formats:
        signs["formats"] = "скачивание " + ", ".join(sorted(formats))
    if hosts:
        signs["hosts"] = "файлообменники " + ", ".join(sorted(hosts))
    if soup.find("audio") or re.search(r"слушать онлайн", text, re.I):
        signs["audio"] = "аудио, слушать онлайн"
    paragraphs = (clean(el.get_text(" ")) for el in soup.find_all(["p", "div"]) if not el.find(["p", "div"]))
    if sum(len(t) for t in paragraphs if len(t) >= 80) >= 3000 and READER_PAGE.search(text):
        signs["text"] = "текст книги прямо на странице"
    if re.search(r"аудиокниг", heading, re.I):
        signs["kind"] = "это аудиоверсия"
    if HIDDEN.search(text):
        signs["hidden"] = "ссылка скрыта до регистрации"
    if REMOVED.search(text):
        signs["removed"] = "на странице сказано, что книга удалена"
    return heading, text, signs


def page_author(text):
    for m in AUTHOR_LABEL.finditer(text):
        words = []
        for w in m.group(1).replace(",", " ").split():
            if w.rstrip(".") in LABEL_WORDS:
                break
            words.append(w)
        if words:
            return " ".join(words)
    return ""


def decide(signs):
    strong = [signs[k] for k in ("file", "formats", "hosts", "torrent", "audio") if signs.get(k)]
    weak = [signs[k] for k in ("text", "reader", "download", "hidden") if signs.get(k)]
    kind = [signs["kind"]] if signs.get("kind") else []
    if signs.get("removed"):
        return 0, signs["removed"]
    if strong:
        return 1, "; ".join(kind + strong + weak)
    if signs.get("store") and weak and not signs.get("text"):
        return 2, "есть ссылки на магазин и признаки чтения или скачивания; назначение ссылок нужно проверить"
    if signs.get("store") and not weak:
        return 0, "найдены только ссылки на магазин, размещение не подтверждено"
    if signs.get("fragment"):
        return 0, signs["fragment"] + ", похоже на фрагмент"
    if weak:
        return 1, "; ".join(kind + weak)
    return 0, "книга упомянута, но скачать, читать или слушать нельзя"


def check(url, catalog, ai_budget=None):
    out = {"url": url, "final_url": "", "http_status": None, "content_type": "", "page_state": "", "heading": "",
           "page_author": "", "signs": [], "match": None, "status": 2, "reason": ""}
    out["checked_at"] = datetime.now(timezone.utc).isoformat()
    try:
        resp, body = fetch(url)
    except requests.RequestException as e:
        why = PRIVATE if PRIVATE in str(e) else type(e).__name__
        return {**out, "page_state": "нет ответа", "reason": f"Сайт не ответил ({why})."}
    out.update(final_url=resp.url, http_status=resp.status_code,
               content_type=resp.headers.get("content-type", "").split(";")[0])
    try:
        details = {}
        heading, text, signs = read_page(resp, body, details)
    except Exception as e:  # битый PDF или HTML не должен ронять всю пачку
        return {**out, "page_state": "не разобрать", "reason": f"Не удалось разобрать ответ ({type(e).__name__})"}
    out.update(heading=heading[:300], page_author=details.get("author") or page_author(text), signs=[v for v in signs.values() if v])

    moved_home = urlsplit(resp.url).path in ("", "/") and urlsplit(url).path not in ("", "/")
    if resp.status_code in (404, 410) or moved_home or NOT_FOUND.search(heading):
        what = "редирект на главную" if moved_home else f"ответ {resp.status_code}, {heading[:60] or 'страница ошибки'}"
        return {**out, "page_state": "страницы нет", "status": 0, "reason": f"Страницы нет: {what}."}
    if resp.status_code >= 400:
        return {**out, "page_state": f"ошибка {resp.status_code}",
                "reason": f"Сайт ответил ошибкой {resp.status_code}, содержимое не получено."}
    if re.search(r"^ошибка\b|не удается получить доступ|access denied|temporarily unavailable", heading, re.I):
        return {**out, "page_state": "ошибка загрузки", "status": 2,
                "reason": "Получена страница ошибки вместо книги. Отсутствие книги не подтверждено, нужна повторная проверка."}
    if not signs.get("file") and len(text) < 400:
        from browser_fetch import browser_page, HOSTS
        out['browser_fallback'] = urlsplit(resp.url).hostname in HOSTS
        rendered = browser_page(resp.url)
        if rendered:
            final_url, http, content = rendered
            rendered_response = requests.Response()
            rendered_response.url = final_url
            rendered_response.status_code = http
            rendered_response.headers["Content-Type"] = "text/html; charset=utf-8"
            heading, text, signs = read_page(rendered_response, content, details)
            out.update(final_url=final_url, http_status=http, heading=heading[:300],
                       page_author=details.get("author") or page_author(text), signs=[v for v in signs.values() if v],
                       extraction_method="браузер Playwright")
    if not signs.get("file") and len(text) < 400:
        return {**out, "page_state": "нет содержимого",
                "reason": ("Получен экран загрузки вместо книги. Браузерный повтор также не получил содержимое. Нужна ручная проверка."
                           if out.get('browser_fallback') else
                           "Получен экран загрузки вместо содержимого. Нужна проверка в браузере.")}

    out["page_state"] = "ok"
    result = classify(out, heading, text, signs, catalog)
    return review(result, heading, text, details.get("links", []), catalog,
                  ai_budget if ai_budget is not None else BoundedSemaphore(1), inspect_link)


def inspect_link(url):
    try:
        response, body = fetch(url)
        if response.status_code == 200:
            heading, text, _ = read_page(response, body)
            return {"url": url, "heading": heading[:500], "text": text[:6000]}
    except Exception:  # повреждённая промежуточная страница не должна прерывать проверку списка
        pass
    return None


def classify(out, heading, text, signs, catalog):
    match, title_only = find_book(heading, text, catalog, out.get("page_author"))
    out["match"] = match
    if not match and title_only:
        if any(b.get("ambiguous") for b in title_only):
            return {**out, "reason": "Найдено несколько книг: " + "; ".join(
                f'{b["author"]}. {b["title"]}' for b in title_only) + ". Нужна проверка человеком."}
        b = max(title_only, key=lambda b: len(b["tkey"]))
        authors = ", ".join(x["author"] for x in title_only if x["tkey"] == b["tkey"])
        shown = f' На странице указан автор: {out["page_author"]}.' if out["page_author"] else ""
        return {**out, "reason": f'Название "{b["title"]}" совпало, но автора из каталога ({authors}) на странице нет.'
                                 f"{shown} Нужна проверка человеком."}
    if not match:
        if signs.get('file') and not heading and not text:
            return {**out, "status": 2, "reason": "Файл доступен, но название и текст извлечь не удалось. Нужна ручная проверка."}
        found = "PDF найден и прочитан" if 'PDF' in signs.get('file', '') else "Страница прочитана"
        return {**out, "status": 0, "reason": f"{found}, но совпадение с каталогом не найдено. Обнаружено: {heading[:180]}"}
    status, why = decide(signs)
    if match["where"] == "текст" and status == 1 and not any(signs.get(k) for k in ("file", "formats", "hosts", "torrent", "audio")):
        # страница автора или тега: чтение онлайн там относится к другим книгам
        return {**out, "reason": "Книга есть только в списке на странице автора или подборки, "
                                 "файлов к ней не видно. Нужна проверка человеком."}
    where = "в заголовке" if match["where"] == "заголовок" else "в тексте страницы (сборник или список)"
    why = f"Признаки размещения: {why}." if status == 1 else why[:1].upper() + why[1:] + "."
    return {**out, "status": status, "reason": f"Книга найдена {where}, автор совпал. {why}"}


def check_many(urls, catalog, workers=8):
    ai_budget = BoundedSemaphore(4)  # не больше четырёх платных разборов на один список
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda u: check(u, catalog, ai_budget), urls))


def main(xlsx, source, dst):
    catalog = load_catalog(xlsx)
    if source == "--all":
        urls = [clean(r["url"]) for s in ("Задание 1", "Задание 2") for r in load_rows(xlsx, s)]
    else:
        with open(source, encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip()]
    results = check_many(list(dict.fromkeys(urls)), catalog)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    if source == "--all":
        part2_urls = {clean(r["url"]) for r in load_rows(xlsx, "Задание 2")}
        Path(dst).with_name("part2.json").write_text(json.dumps(
            [r for r in results if r["url"] in part2_urls], ensure_ascii=False, indent=1), encoding="utf-8")
    print("статусы:", {s: sum(r["status"] == s for r in results) for s in (0, 1, 2)})


if __name__ == "__main__":
    main(*sys.argv[1:4])
