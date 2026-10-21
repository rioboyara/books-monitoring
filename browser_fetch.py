"""Ограниченный браузерный повтор для известных зеркал Библибобы."""
import ipaddress
import socket
import time
import logging
from threading import BoundedSemaphore
from urllib.parse import urlsplit

busy_browser = BoundedSemaphore(1)
HOSTS = ('yaa.bibliboba.org', 'biblo.iboba.club')


def browser_page(url):
    try:
        p = urlsplit(url)
        allowed = p.scheme == 'https' and p.hostname in HOSTS and p.port in (None, 443)
    except ValueError:
        return None
    if not allowed:
        return None
    if not busy_browser.acquire(blocking=False):
        return None
    try:
        from playwright.sync_api import sync_playwright
        rules = []
        for host in HOSTS:
            addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            if not addresses or not all(ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                return None
            rules.append('MAP ' + host + ' ' + addresses[0][4][0])
        rules.append('MAP * ~NOTFOUND')
        with sync_playwright() as engine:
            browser = engine.chromium.launch(headless=True, timeout=10000, args=[
                '--no-sandbox', '--no-proxy-server', '--disable-dev-shm-usage',
                '--renderer-process-limit=1', '--host-resolver-rules=' + ','.join(rules)])
            try:
                context = browser.new_context(locale='ru-RU', accept_downloads=False, service_workers='block')
                def route(request):
                    target = urlsplit(request.request.url)
                    if target.scheme == 'https' and target.hostname in HOSTS and target.port in (None, 443):
                        request.continue_()
                    else:
                        request.abort()
                context.route('**/*', route)
                context.route_web_socket('**/*', lambda ws: ws.close())
                page = context.new_page()
                response = page.goto(url, wait_until='domcontentloaded', timeout=12000)
                # CSP сайта запрещает eval в wait_for_function; локатор работает без него.
                until = time.monotonic() + 15
                while len(page.locator('body').inner_text(timeout=1000)) < 1500:
                    if time.monotonic() >= until:
                        return None
                    page.wait_for_timeout(300)
                content = page.content().encode('utf-8')
                if len(content) > 3000000:
                    return None
                return page.url, response.status if response else 200, content
            finally:
                browser.close()
    except Exception as error:
        logging.getLogger(__name__).warning('Browser fallback failed: %s', type(error).__name__)
        return None
    finally:
        busy_browser.release()
