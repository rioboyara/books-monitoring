"""Проверка ограничений браузерной загрузки без сетевых запросов."""
import sys
from unittest.mock import Mock, MagicMock, patch
from browser_fetch import browser_page

for url in ('http://yaa.bibliboba.org/book', 'https://127.0.0.1/',
            'https://example.org/', 'https://yaa.bibliboba.org:8443/book'):
    assert browser_page(url) is None

api = Mock()
with patch.dict(sys.modules, {'playwright': Mock(), 'playwright.sync_api': api}), \
        patch('browser_fetch.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]) as dns:
    assert browser_page('https://yaa.bibliboba.org/book') is None
    dns.assert_called_once()
    api.sync_playwright.assert_not_called()
print('browser boundary checks passed')

api = MagicMock()
page = api.sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value.new_context.return_value.new_page.return_value
page.goto.return_value.status = 200
page.url = 'https://biblo.iboba.club/book'
page.locator.return_value.inner_text.return_value = 'Текст книги ' * 200
page.content.return_value = '<html>книга</html>'
with patch.dict(sys.modules, {'playwright': Mock(), 'playwright.sync_api': api}), \
        patch('browser_fetch.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('1.1.1.1', 443))]):
    result = browser_page('https://yaa.bibliboba.org/book')
    assert result == (page.url, 200, '<html>книга</html>'.encode())
    page.wait_for_function.assert_not_called()
    page.locator.return_value.inner_text.return_value = 'Загрузка'
    with patch('browser_fetch.time.monotonic', side_effect=[0, 16]):
        assert browser_page('https://yaa.bibliboba.org/book') is None
print('browser loading and timeout checks passed')
