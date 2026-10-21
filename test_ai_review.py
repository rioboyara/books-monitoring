"""Проверки ответов ИИ без платных запросов: python test_ai_review.py."""
import os
from threading import BoundedSemaphore
from unittest.mock import patch

import requests

from ai_review import review, verified_answer, validated_suggestions

BOOK = {"book_id": "B1", "author": "Алекс Рудин", "title": "Егерь-3", "tkey": "егерь3",
        "twords": ["eger"], "awords": ["aleks", "rudin"]}
EVIDENCE = "Егерь-3 Алекс Рудин Скачать FB2"
ANSWER = {"status": 1, "book_id": "B1", "confidence": .98, "reason": "Книга и автор совпали, есть скачивание.",
          "title_quote": "Егерь-3", "author_quote": "Алекс Рудин", "content_quote": "Скачать FB2"}


def test_validation():
    assert verified_answer(ANSWER, [BOOK], EVIDENCE)[0] == 1
    for changes in ({"book_id": "invented"}, {"author_quote": "Другой автор"},
                    {"content_quote": "полная книга бесплатно"}, {"author_quote": ""},
                    {"status": True}, {"confidence": float("nan")}):
        try:
            verified_answer({**ANSWER, **changes}, [BOOK], EVIDENCE)
        except ValueError:
            pass
        else:
            raise AssertionError(changes)
    try:
        verified_answer({**ANSWER, "status": 0, "content_quote": "Не удалось загрузить фрагмент"},
                        [BOOK], EVIDENCE + " Не удалось загрузить фрагмент")
    except ValueError:
        pass
    else:
        raise AssertionError("Ошибка загрузки не доказывает отсутствие размещения")


def test_unavailable_api():
    out = {"status": 2, "page_state": "ok", "reason": "Нужна проверка", "match": None}
    with patch.dict(os.environ, {"API429_KEY": "test-not-a-real-key"}), patch(
            "requests.Session.post", side_effect=requests.Timeout):
        answer = review(out, "Егерь-3", EVIDENCE, [], [BOOK], BoundedSemaphore(1))
        assert answer["status"] == 2 and not answer["ai"]["accepted"]
        assert "test-not-a-real-key" not in str(answer)
    # Недоступная страница и уверенное решение правил не отправляются модели.
    with patch.dict(os.environ, {"API429_KEY": "test-not-a-real-key"}), patch("requests.Session.post") as post:
        assert review({**out, "page_state": "нет содержимого"}, "", "", [], [], BoundedSemaphore(1))["status"] == 2
        assert review({**out, "status": 1}, "", "", [], [], BoundedSemaphore(1))["status"] == 1
        post.assert_not_called()


def test_data_recommendations():
    sample = [{"row": 7, "author": "Автор", "title": "Книга", "doubts": "псевдоним"}]
    good = {"suggestions": [{"row": 7, "quote": "Книга", "advice": "Уточнить автора", "needed": "Список псевдонимов"}]}
    assert validated_suggestions(good, sample)[0][0] == 7
    for item in ({**good["suggestions"][0], "row": 99}, {**good["suggestions"][0], "quote": "Выдуманная книга"}):
        try:
            validated_suggestions({"suggestions": [item]}, sample)
        except ValueError:
            pass
        else:
            raise AssertionError(item)


def test_existing_book_is_preserved():
    out = {"status": 2, "page_state": "ok", "reason": "Нужна проверка", "match": BOOK}
    answer = {**ANSWER, "status": 0, "book_id": None, "content_quote": "На Литрес"}
    with patch.dict(os.environ, {"API429_KEY": "test-not-a-real-key"}), patch('ai_review.complete', return_value=answer):
        result = review(out, "Егерь-3", EVIDENCE + " На Литрес", [], [BOOK], BoundedSemaphore(1))
        assert result['status'] == 0 and result['match']['book_id'] == 'B1'


if __name__ == "__main__":
    test_validation()
    test_unavailable_api()
    test_data_recommendations()
    test_existing_book_is_preserved()
    print("ai review checks passed")
