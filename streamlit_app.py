"""Точка входа: streamlit run streamlit_app.py

Streamlit перезапускает этот файл на КАЖДОЕ взаимодействие (отправка сообщения,
клик, rerun). Поэтому здесь не `import app.ui`: импортированный модуль остаётся
в sys.modules, при повторном прогоне его код не выполняется, и страница
отрисовывается пустой — ровно один раз, при первом заходе.

Вместо импорта — явный вызов render(), который отрабатывает каждый прогон.
"""

import sys
from pathlib import Path

# Streamlit кладёт в sys.path директорию скрипта; добавляем корень явно, чтобы
# пакет `app` находился и при запуске из другого места (тесты, AppTest, отладка).
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui import render  # noqa: E402

render()
