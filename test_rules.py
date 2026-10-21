"""Проверка правил на примерах из задания. Запуск: python test_rules.py (или pytest)."""
from checker import decide, find_book, js_cookie
from normalize import author_key, key, loose, split_author, title_vs_url, url_key

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


def test_decide():
    assert decide({"formats": "скачивание fb2", "store": "ссылки на Литрес"})[0] == 1
    assert decide({"reader": "чтение онлайн", "store": "ссылки на Литрес"})[0] == 0
    assert decide({"text": "текст книги прямо на странице"})[0] == 1
    assert decide({})[0] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
