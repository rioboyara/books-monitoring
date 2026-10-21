"""Проверка правил на примерах из задания. Запуск: python test_rules.py (или pytest)."""
from checker import PRIVATE, decide, find_book, js_cookie, public_connection
from normalize import author_key, key, loose, process, split_author, title_vs_url, url_key

CATALOG = [
    {"book_id": "B1", "author": "Джейн Симс", "title": "Раз. Два. Призраки идут"},
    {"book_id": "B2", "author": "Helga Duran", "title": "Босс. Будет жарко"},
    {"book_id": "B3", "author": "Евгений Нетт", "title": "Директива: Сохранить!"},
    {"book_id": "B4", "author": "Ася Исай", "title": "Измена. Хочу твоего мужа"},
    {"book_id": "B5", "author": "Алекс Рудин", "title": "Егерь-3: Назад в СССР"},
]
for b in CATALOG:
    b.update(tkey=key(b["title"]), twords=[w for w in loose(b["title"]).split() if len(w) > 2],
             awords=[w for w in loose(b["author"]).split() if len(w) > 2])


def test_normalize():
    assert key("Ценадоверия") == key("Цена доверия")
    assert key(";100 ГОТОВЫХ РЕШЕНИЙ С ИИ") == key("100 готовых решений с ИИ")
    assert author_key("Рудин Алекс") == author_key("Алекс Рудин")
    assert split_author("(JustHope) Егорова Надежда;") == ("Егорова Надежда", "JustHope")
    assert url_key("https://s4.skladchik.tv/threads/x.1/") == url_key("https://skladchik.tv/threads/x.1")
    assert url_key("https://librain.ru/index.php/detektivy/a") == url_key("https://librain.ru/detektivy/a")
    assert title_vs_url("Ценадоверия", "https://free-kniga.ru/romany/tsena-proscheniya-147884/")[0] == 0
    assert title_vs_url("Позывной Хоттабыч#6", "https://free-kniga.ru/p/pozyvnoy-hottabych-7-ch-2/")[1] == ["6"]
    assert title_vs_url("Ложь напоказ", "https://storipad.ru/story/168175")[0] is None


def test_new_rows_keep_ids():
    row = {"row": 2, "Правообладатель": "Литрес", "Автор": "Рудин Алекс", "Название": "Тайновидец. Том 9",
           "Тип контента": "ebook", "url": "https://a.ru/taynovidets-tom-9/", "Сайт": "a.ru"}
    before = process([dict(row)])
    after = process([dict(row), {**row, "row": 3, "Автор": "Алекс Рудин", "url": "https://s4.a.ru/taynovidets-tom-9"}])
    assert before[0]["book"] == after[0]["book"] == after[1]["book"]
    assert after[1]["dup_of"] == 2


def test_js_cookie():
    beget = "<script>document.cookie='beget=begetok'+'; expires='+now.toGMTString()+'; path=/';</script>"
    assert js_cookie(beget) == ("beget", "begetok")
    xbook = "a='anti-21-';b='bot';document.cookie='antibot-21='+a+b+';expires='+d.toUTCString()+';path=/';"
    assert js_cookie(xbook) == ("antibot-21", "anti-21-bot")


def test_matching():
    # PDF с другой книгой: совпадают "Джейн" и начало "Раз", но это не наша книга
    match, _ = find_book("Разреши любить. Позволь мне быть рядом. Книга 2 Анна Джейн", "", CATALOG)
    assert match is None
    # автор латиницей в каталоге, кириллицей на сайте
    match, _ = find_book("Босс. Будет жарко Хельга Дюран - читать онлайн", "", CATALOG)
    assert match["book_id"] == "B2"
    # книга внутри сборника: в заголовке автор, название в тексте
    match, _ = find_book("Евгений Нетт - Сборник произведений", "6. Директива: Сохранить! 7. Директива: Расширить!", CATALOG)
    assert match["book_id"] == "B3" and match["where"] == "текст"
    # то же название, другой автор: не совпадение, а вопрос человеку
    match, title_only = find_book("Измена. Хочу твоего мужа Оксана Алексаева", "", CATALOG)
    assert match is None and title_only[0]["book_id"] == "B4"
    # другой том той же серии
    match, _ = find_book("Егерь: назад в СССР Рудин Алекс", "", CATALOG)
    assert match is None


def test_no_internal_addresses():
    for host in ("localhost", "127.0.0.1", "10.0.0.1", "192.168.1.1"):
        try:
            public_connection((host, 80))
        except OSError as e:
            assert PRIVATE in str(e)
        else:
            raise AssertionError(host)


def test_decide():
    assert decide({"formats": "скачивание fb2", "store": "ссылки на Литрес"})[0] == 1
    assert decide({"reader": "чтение онлайн", "store": "ссылки на Литрес"})[0] == 2
    assert decide({"text": "текст книги прямо на странице"})[0] == 1
    assert decide({})[0] == 0


def test_review_regressions():
    from normalize import book_id
    from checker import page_author
    assert page_author("Примечания автора: Вторая часть романа") == ""
    assert book_id("А. Иванов", "Книга") != book_id("Б. Иванов", "Книга")
    assert decide({"file": "PDF на 4 стр."})[0] == 1
    assert decide({"text": "текст книги", "store": "магазин"})[0] == 1
    both = "Босс. Будет жарко Хельга Дюран. Измена. Хочу твоего мужа Ася Исай"
    match, candidates = find_book(both, "", CATALOG)
    assert match is None and len(candidates) == 2
    match, _ = find_book("Измена. Хочу твоего мужа", "Автор: Оксана Алексаева. Рекомендуем: Ася Исай", CATALOG)
    assert match is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
