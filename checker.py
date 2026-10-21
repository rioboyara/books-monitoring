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
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from normalize import book_id, clean, key, load_rows, loose, split_author

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_BYTES = 20_000_000
TIMEOUT = (10, 25)

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
NOT_FOUND = re.compile(r"не найден|ошибка 404|404 not found|page not found|^ошибка\b", re.I)


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


def is_public(url):
    """Проверка запускается на сервере, поэтому не ходим во внутреннюю сеть (localhost, 10.x, 192.168.x)."""
    try:
        infos = socket.getaddrinfo(urlsplit(url).hostname, None)
    except (socket.gaierror, UnicodeError):
        return True  # несуществующий домен requests сам вернёт как ошибку соединения
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def get(s, url):
    """GET с ручными редиректами, чтобы проверить каждый адрес в цепочке."""
    for _ in range(8):
        if not is_public(url):
            raise requests.RequestException("адрес во внутренней сети, не проверяем")
        with s.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False) as resp:
            if not resp.is_redirect:
                return resp, b"".join(itertools.islice(resp.iter_content(65536), MAX_BYTES // 65536))
            url = urljoin(url, resp.headers["location"])
    raise requests.TooManyRedirects()


def fetch(url):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"})
    for attempt in range(3):
        resp, body = get(s, url)
        cookie = js_cookie(body.decode("utf-8", "replace")) if len(body) < 5000 and attempt < 2 else None
        if not cookie:
            return resp, body
        s.cookies.set(*cookie, domain=urlsplit(resp.url).hostname)


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


def find_book(heading, text, catalog):
    """Сначала ищем название (в заголовке, для сборников в тексте), потом обязательно автора."""
    hk, tk = key(heading), key(text)
    head_words = set(loose(heading).split())
    all_words = head_words | set(loose(text).split())
    found, title_only = [], []
    for b in catalog:
        # номер тома обязателен: "Егерь-3" не должен совпасть со страницей "Егерь: назад в СССР"
        same_numbers = set(re.findall(r"\d+", b["title"])) <= set(re.findall(r"\d+", heading))
        if b["tkey"] and (b["tkey"] in hk or same_numbers and title_in(b["twords"], head_words)):
            where = "заголовок"
        elif len(b["tkey"]) >= 12 and b["tkey"] in tk:
            where = "текст"
        else:
            continue
        need = min(2, len(b["awords"]))
        # книга только в тексте (сборник, список) засчитывается, если автор есть в заголовке
        if words_in(b["awords"], head_words if where == "текст" else all_words) >= need:
            found.append((where != "заголовок", -len(b["tkey"]), b, where))
        elif where == "заголовок":
            title_only.append(b)
    if found:
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


def read_page(resp, body):
    """Заголовок, видимый текст и признаки размещения."""
    signs = {}
    ctype = resp.headers.get("content-type", "").lower()
    if "pdf" in ctype or body[:5] == b"%PDF-":
        pdf = PdfReader(io.BytesIO(body))
        meta = pdf.metadata or {}
        text = " ".join(p.extract_text() or "" for p in pdf.pages[:3])
        heading = clean(f"{meta.get('/Title', '')} {meta.get('/Author', '')}") or clean(text)[:200]
        signs["file"] = f"файл PDF на {len(pdf.pages)} стр." if len(pdf.pages) >= 20 else ""
        signs["fragment"] = f"PDF всего на {len(pdf.pages)} стр." if len(pdf.pages) < 20 else ""
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
    formats, hosts = set(), set()
    # кнопки бывают ссылками, <button> и div с data-href или onclick
    clickable = soup.find_all(["a", "button"]) + soup.find_all(attrs={"data-href": True}) + \
        soup.find_all(attrs={"onclick": True})
    for el in clickable:
        href = el.get("href") or el.get("data-href") or el.get("onclick") or ""
        label = clean(el.get_text(" "))
        both = href + " " + (label if len(label) <= 60 else "")
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


def decide(signs):
    strong = [signs[k] for k in ("file", "formats", "hosts", "torrent", "audio") if signs.get(k)]
    weak = [signs[k] for k in ("text", "reader", "download", "hidden") if signs.get(k)]
    kind = [signs["kind"]] if signs.get("kind") else []
    if signs.get("removed"):
        return 0, signs["removed"]
    if strong:
        return 1, "; ".join(kind + strong + weak)
    if signs.get("store"):
        return 0, "своих файлов нет, скачивание и чтение ведут на покупку в Литрес"
    if signs.get("fragment"):
        return 0, signs["fragment"] + ", похоже на фрагмент"
    if weak:
        return 1, "; ".join(kind + weak)
    return 0, "книга упомянута, но скачать, читать или слушать нельзя"


def check(url, catalog):
    out = {"url": url, "final_url": "", "http_status": None, "page_state": "", "heading": "",
           "signs": [], "match": None, "status": 2, "reason": ""}
    try:
        resp, body = fetch(url)
    except requests.RequestException as e:
        why = str(e) if type(e) is requests.RequestException else type(e).__name__
        return {**out, "page_state": "нет ответа", "reason": f"Сайт не ответил ({why})."}
    out.update(final_url=resp.url, http_status=resp.status_code)
    try:
        heading, text, signs = read_page(resp, body)
    except Exception as e:  # битый PDF или HTML не должен ронять всю пачку
        return {**out, "page_state": "не разобрать", "reason": f"Не удалось разобрать ответ ({type(e).__name__})"}
    out.update(heading=heading[:300], signs=[v for v in signs.values() if v])

    moved_home = urlsplit(resp.url).path in ("", "/") and urlsplit(url).path not in ("", "/")
    if resp.status_code in (404, 410) or moved_home or NOT_FOUND.search(heading):
        what = "редирект на главную" if moved_home else f"ответ {resp.status_code}, {heading[:60] or 'страница ошибки'}"
        return {**out, "page_state": "страницы нет", "status": 0, "reason": f"Страницы нет: {what}."}
    if resp.status_code >= 400:
        return {**out, "page_state": f"ошибка {resp.status_code}",
                "reason": f"Сайт ответил ошибкой {resp.status_code}, содержимое не получено."}
    if not signs.get("file") and len(text) < 400:
        return {**out, "page_state": "нет содержимого",
                "reason": "Сайт не отдал содержимое без браузера: проверка на бота или страница собирается скриптом."}

    out["page_state"] = "ok"
    match, title_only = find_book(heading, text, catalog)
    out["match"] = match
    if not match and title_only:
        b = max(title_only, key=lambda b: len(b["tkey"]))
        authors = ", ".join(x["author"] for x in title_only if x["tkey"] == b["tkey"])
        return {**out, "reason": f'Название "{b["title"]}" совпало, но автора из каталога ({authors}) на странице нет. '
                                 "Нужна проверка человеком."}
    if not match:
        return {**out, "status": 0, "reason": f"Книг из каталога на странице нет. Страница про: {heading[:100]}"}
    status, why = decide(signs)
    if match["where"] == "текст" and status == 1 and not any(signs.get(k) for k in ("file", "formats", "hosts", "torrent", "audio")):
        # страница автора или тега: чтение онлайн там относится к другим книгам
        return {**out, "reason": "Книга есть только в списке на странице автора или подборки, "
                                 "файлов к ней не видно. Нужна проверка человеком."}
    where = "в заголовке" if match["where"] == "заголовок" else "в тексте страницы (сборник или список)"
    why = f"Признаки размещения: {why}." if status == 1 else why[:1].upper() + why[1:] + "."
    return {**out, "status": status, "reason": f"Книга найдена {where}, автор совпал. {why}"}


def check_many(urls, catalog, workers=8):
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda u: check(u, catalog), urls))


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
    print("статусы:", {s: sum(r["status"] == s for r in results) for s in (0, 1, 2)})


if __name__ == "__main__":
    main(*sys.argv[1:4])
