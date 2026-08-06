"""Smoke-тест страницы через официальный движок Streamlit.

Ловит класс ошибок, который не виден ни линтеру, ни обычным тестам: страница
рендерится один раз, а на втором прогоне оказывается пустой. Так и было —
точка входа делала `import app.ui`, и при перезапуске скрипта Python отдавал
модуль из sys.modules, не выполняя код. Внешне: UI исчезал при отправке
сообщения, без ошибок и без строчки в логах.

Модель здесь не вызывается: тест проверяет, что страница строится и переживает
перерисовку, а не качество ответов.
"""

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def app():
    at = AppTest.from_file("streamlit_app.py", default_timeout=60)
    at.run()
    return at


def test_page_renders(app):
    assert not app.exception
    assert len(app.markdown) > 0, "страница пустая на первом прогоне"


def test_sidebar_present(app):
    assert len(app.sidebar.markdown) > 0, "сайдбар не отрисовался"


def test_chat_input_present(app):
    assert len(app.chat_input) == 1


def test_survives_rerun(app):
    """Ключевой тест: после перезапуска скрипта страница должна остаться живой"""
    app.run()
    assert not app.exception
    assert len(app.markdown) > 0, "страница опустела после rerun"
    assert len(app.sidebar.markdown) > 0, "сайдбар пропал после rerun"
