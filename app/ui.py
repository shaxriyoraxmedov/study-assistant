import html
import random
import re
from datetime import datetime

import streamlit as st
from streamlit.runtime.scriptrunner_utils.exceptions import RerunException, StopException

from app import rag
from app.agent import (stream_study_assistant, client, MEMORY_COLLECTION,
                       ensure_memory_collection, get_facts_with_ids, delete_fact)

# ============================================================
# ТЕМА
# ============================================================
# Тема только тёмная, и это осознанно. Раньше здесь был переключатель
# auto/light/dark поверх базовой темы Streamlit — он не работал: Streamlit
# красит свои контейнеры и виджеты СВОЕЙ темой, поэтому при переходе на светлую
# наш текст становился тёмным, а подложка под ним оставалась тёмной, и страница
# выглядела пустой. Перебивать это пришлось бы !important на каждом контейнере.
# Одна аккуратная тема надёжнее двух наполовину рабочих.
# Базовая тема Streamlit задана в .streamlit/config.toml и совпадает с этой палитрой.
DARK = {
    "bg": "#0d0f0d", "glow": "#1a3d1a", "accent": "#4ADE80",
    "card": "#161816", "sidebar": "#0a0b0a", "input": "#1c1e1c",
    "text": "#F0F0EC", "text-dim": "#8A8D8A", "border": "#2a2c2a",
    "shadow": "0 1px 3px rgba(0,0,0,.3)",
    # приподнятая поверхность для вложенных блоков (шаги trace внутри карточки)
    "raised": "#1f221f",
    "accent-soft": "rgba(74,222,128,.12)",
    # основная зона чуть светлее сайдбара: две панели должны читаться раздельно
    "bg-main": "#111311",
}


# ============================================================
# ПРИВЕТСТВИЕ
# ============================================================
# Генерировать заголовок моделью — это +10 секунд к открытию страницы и лишняя
# нагрузка на GPU ради одной строки. Берём готовые варианты: выглядит так же живо,
# стоит ноль. Имя подставляем из памяти, если оно там есть.
GREETINGS = [
    "Чем помочь с учёбой?",
    "С чего начнём?",
    "Над чем работаем сегодня?",
    "Что разбираем?",
    "Какой вопрос?",
    "Готов помочь — спрашивай",
]
GREETINGS_NAMED = [
    "Привет, {name}! Чем помочь?",
    "{name}, с чего начнём?",
    "Рад видеть, {name}. Что разбираем?",
    "Снова за учёбу, {name}?",
]


def _known_name() -> str | None:
    """Достаёт имя из фактов вида «Пользователя зовут X»"""
    try:
        for _, fact in get_facts_with_ids():
            m = re.search(r'зовут\s+([А-ЯЁA-Z][\wёЁ-]+)', str(fact))
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


# иконка под каждый инструмент — по ней шаг узнаётся быстрее, чем по имени функции
TOOL_ICONS = {
    "search_notes": "description",
    "search_wikipedia": "travel_explore",
    "calculate": "calculate",
    "recall_memory": "psychology",
}


def render_trace(trace: list[dict], key: str) -> None:
    """Рисует ход рассуждения: бейджи инструментов + разворачиваемые карточки шагов.

    Раньше здесь был плоский список внутри expander — по нему нельзя было понять,
    где кончается один вызов и начинается другой. Теперь каждый шаг — карточка
    с номером, именем инструмента, аргументами и результатом.
    """
    if not trace:
        return

    badges = "".join(
        f'<span class="tool-badge">'
        f'<span class="material-symbols-rounded">{TOOL_ICONS.get(t["tool"], "build")}</span>'
        f'{html.escape(str(t["tool"]))}</span>'
        for t in trace
    )
    st.markdown(badges, unsafe_allow_html=True)

    label = f"Ход рассуждения · {len(trace)} " + ("шаг" if len(trace) == 1 else "шага/ов")
    with st.expander(label, icon=":material/account_tree:"):
        steps = []
        for i, step in enumerate(trace, 1):
            icon = TOOL_ICONS.get(step["tool"], "build")
            # escape обязателен: и аргументы, и результат приходят из модели/интернета
            steps.append(
                f'<div class="trace-step">'
                f'<div class="trace-head">'
                f'<span class="step-num">{i}</span>'
                f'<span class="material-symbols-rounded" style="font-size:14px;color:var(--accent);">{icon}</span>'
                f'<span class="tool-name">{html.escape(str(step["tool"]))}</span>'
                f'<span class="tool-args">{html.escape(str(step["args"]))}</span>'
                f'</div>'
                f'<div class="trace-body">{html.escape(str(step["result"]))}</div>'
                f'</div>'
            )
        st.markdown("".join(steps), unsafe_allow_html=True)


def greeting_title() -> str:
    # фиксируем на сессию: без этого строка прыгала бы при каждой перерисовке
    if "greeting" not in st.session_state:
        name = _known_name()
        hour = datetime.now().hour
        if name and 5 <= hour < 12:
            st.session_state.greeting = f"Доброе утро, {name}!"
        elif name:
            st.session_state.greeting = random.choice(GREETINGS_NAMED).format(name=name)
        else:
            st.session_state.greeting = random.choice(GREETINGS)
    return st.session_state.greeting


# ============================================================
# SIDEBAR — память о пользователе (живая, обновляется каждый раз)
# ============================================================


def render():
    """Рисует страницу. Вызывается на каждый прогон Streamlit."""
    st.set_page_config(page_title="Study Assistant", page_icon=":material/school:", layout="centered")

    theme_css = ":root {\n" + "\n".join(f"    --{k}: {v};" for k, v in DARK.items()) + "\n}"

    st.markdown(f"""
    <style>
        {theme_css}

        .material-symbols-rounded {{
            font-family: 'Material Symbols Rounded', 'Material Symbols Outlined', sans-serif;
            vertical-align: middle; font-weight: normal; font-style: normal;
            letter-spacing: normal; text-transform: none; white-space: nowrap;
            direction: ltr; -webkit-font-feature-settings: 'liga'; font-feature-settings: 'liga';
        }}

        /* Фон снимаем с корневых контейнеров, но БЕЗ !important на color: иначе он
           перебивает цвет внутри Streamlit-компонентов, и текст в сайдбаре тонет в фоне. */
        html, body, #root {{ background-color: var(--bg); }}
        /* прозрачные — иначе закрывают градиентный слой .stApp::before */
        [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background-color: transparent;
        }}
        /* ГРАДИЕНТ живёт на .stApp — это самый верхний контейнер, ниже его
           перекрывают собственные фоны stMain/stAppViewContainer, и пятна тонут.
           ::before с fixed — слой поверх заливки, но под контентом. */
        .stApp {{
            background-color: var(--bg);
            color: var(--text);
        }}
        .stApp::before {{
            content: ""; position: fixed; inset: 0; z-index: 0; pointer-events: none;
            background:
                radial-gradient(ellipse 85% 55% at 50% -12%,
                    rgba(74,222,128,.38), transparent 60%),
                radial-gradient(ellipse 60% 50% at 100% 8%,
                    rgba(56,189,248,.22), transparent 58%),
                radial-gradient(ellipse 70% 60% at 0% 100%,
                    rgba(74,222,128,.20), transparent 60%),
                radial-gradient(ellipse 48% 38% at 88% 92%,
                    rgba(168,85,247,.16), transparent 58%),
                /* пятно под сайдбаром: без него стекло слева размывает пустоту */
                radial-gradient(ellipse 30% 55% at 4% 30%,
                    rgba(56,189,248,.20), transparent 62%);
        }}
        /* Контент поверх градиентного слоя. Сайдбар СЮДА НЕ ВХОДИТ намеренно:
           если поднять его над ::before, градиент окажется под ним и размывать
           будет нечего — стекло выглядело бы как простая серая заливка. */
        [data-testid="stAppViewContainer"] {{
            position: relative; z-index: 1; background: transparent;
        }}
        /* СТЕКЛО САЙДБАРА: фон снимаем с внешней секции (иначе она глухая и
           перекрывает градиент), а размытие вешаем и на неё, и на внутренний div —
           Streamlit красит фон то на одном, то на другом в зависимости от версии. */
        section[data-testid="stSidebar"] {{
            background-color: rgba(10, 11, 10, .5) !important;
            backdrop-filter: blur(30px) saturate(150%);
            -webkit-backdrop-filter: blur(30px) saturate(150%);
            border-right: 1px solid rgba(255,255,255,.07);
        }}
        /* внутренние обёртки прозрачные — фон и стекло уже на самой секции */
        section[data-testid="stSidebar"] > div,
        section[data-testid="stSidebar"] [data-testid="stSidebarContent"],
        section[data-testid="stSidebarUserContent"] {{
            background-color: transparent !important;
        }}

        /* Раньше здесь было сплошное `header {{visibility: hidden}}` — оно прятало и
           кнопку разворота сайдбара, поэтому закрытый сайдбар нельзя было вернуть.
           Прячем только меню и футер, шапку лишь делаем прозрачной. */
        #MainMenu, footer {{ visibility: hidden; }}
        header[data-testid="stHeader"] {{ background: transparent; }}

        /* ПЛОТНОСТЬ: дефолтные отступы Streamlit рассчитаны на дашборды с широкими
           графиками. Для чата они дают много пустоты — ужимаем контейнер и сайдбар. */
        /* Лента прижата к низу: короткий диалог не висит вверху с пустотой под ним,
           а стоит над полем ввода, как в мессенджерах. Когда сообщений много,
           min-height перестаёт действовать и работает обычная прокрутка. */
        /* Запас снизу под закреплённое поле ввода. Ставим на ОБА контейнера:
           stMain — тот, что реально скроллится, а stMainBlockContainer его
           внутренняя обёртка; padding только на второй последние сообщения
           всё равно загонял под поле и обрезал кнопки действий. */
        [data-testid="stMainBlockContainer"] {{
            padding-top: 3rem; padding-bottom: 10rem;
            max-width: 780px;
        }}
        [data-testid="stMain"] {{ scroll-padding-bottom: 10rem; }}
        section[data-testid="stSidebar"] {{ width: 280px !important; }}
        section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
            padding-top: 1.2rem;
        }}
        /* вертикальные промежутки между блоками: 1rem по умолчанию — слишком воздушно */
        [data-testid="stVerticalBlock"] {{ gap: .55rem; }}
        section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: .4rem; }}
        hr {{ margin: .7rem 0; border-color: var(--border); }}

        /* СТЕКЛО: заливка сильно прозрачная (иначе размывать нечего) + saturate,
           который делает цвет под стеклом насыщеннее. Светлая полоса по верхней
           кромке — блик, из-за него панель читается как стекло, а не как плёнка. */
        [data-testid="stChatMessage"] {{
            background-color: rgba(22, 24, 22, .45);
            backdrop-filter: blur(22px) saturate(150%);
            -webkit-backdrop-filter: blur(22px) saturate(150%);
            border-radius: 16px; padding: 12px 15px;
            border: 1px solid rgba(255,255,255,.07);
            box-shadow: 0 8px 32px rgba(0,0,0,.34),
                        inset 0 1px 0 rgba(255,255,255,.06);
            font-size: 15px; line-height: 1.6;
        }}

        /* РЕПЛИКА ПОЛЬЗОВАТЕЛЯ — справа пузырём, как в мессенджере.
           st.chat_message рисует все сообщения одинаково слева во всю ширину,
           поэтому реплику юзера собираем своей разметкой. */
        .user-row {{ display: flex; justify-content: flex-end; margin: 2px 0 10px 0; }}
        .user-bubble {{
            max-width: 76%; padding: 9px 14px; border-radius: 16px 16px 4px 16px;
            background-color: var(--accent); color: #08210f;
            font-size: 14.5px; line-height: 1.5; font-weight: 500;
            box-shadow: 0 2px 10px rgba(74,222,128,.18);
            word-break: break-word; white-space: pre-wrap;
        }}
        .stButton > button {{
            background-color: rgba(28, 30, 28, .5); color: var(--text);
            backdrop-filter: blur(12px);
            border-radius: 12px; border: 1px solid rgba(255,255,255,.07);
            transition: border-color .15s ease, color .15s ease, background-color .15s ease;
        }}
        .stButton > button:hover {{
            border-color: color-mix(in srgb, var(--accent) 45%, transparent);
            color: var(--accent);
            background-color: rgba(74,222,128,.09);
        }}
        [data-testid="stChatInput"] {{
            border-radius: 24px !important;
            border: 1px solid rgba(255,255,255,.09) !important;
            background-color: rgba(28, 30, 28, .55) !important;
            backdrop-filter: blur(26px) saturate(150%);
            -webkit-backdrop-filter: blur(26px) saturate(150%);
            box-shadow: 0 8px 32px rgba(0,0,0,.5),
                        inset 0 1px 0 rgba(255,255,255,.07);
            transition: border-color .15s ease, box-shadow .15s ease;
        }}
        [data-testid="stChatInput"]:focus-within {{
            box-shadow: 0 8px 32px rgba(0,0,0,.5),
                        inset 0 1px 0 rgba(255,255,255,.07),
                        0 0 0 3px rgba(74,222,128,.13);
        }}
        [data-testid="stChatInput"]:focus-within {{
            border-color: color-mix(in srgb, var(--accent) 55%, transparent) !important;
        }}
        /* Нижняя зона прозрачная: сплошная подложка резала градиент чёрной полосой.
           Само поле ввода непрозрачное с размытием, поэтому текст под ним не мешает. */
        [data-testid="stBottom"], [data-testid="stBottom"] > div,
        [data-testid="stBottomBlockContainer"] {{
            background: transparent !important;
        }}
        /* поле ввода не перехватывает клики мимо себя — иначе оно накрывает
           кнопки действий под последним ответом невидимой областью */
        [data-testid="stBottom"] {{ pointer-events: none; }}
        [data-testid="stBottom"] [data-testid="stChatInput"] {{ pointer-events: auto; }}
        [data-testid="stBottomBlockContainer"] {{ padding-bottom: 1.1rem; }}
        /* поле ввода тянется во всю ширину контейнера — сужаем до ширины контента */
        [data-testid="stBottomBlockContainer"] [data-testid="stVerticalBlock"] {{
            max-width: 780px; margin: 0 auto;
        }}

        /* Аватар-иконка убран: вместо робота — пульсирующая точка, как в Claude.
           Сам элемент оставляем в потоке (Streamlit на него завязывает отступы),
           но прячем содержимое и рисуем точку через ::after. */
        [data-testid="stChatMessageAvatarAssistant"] {{
            background: transparent !important;
            border: none !important;
            color: transparent !important;
            position: relative;
            width: 26px !important; height: 26px !important;
            min-width: 26px !important;
        }}
        [data-testid="stChatMessageAvatarAssistant"] > * {{ display: none; }}
        [data-testid="stChatMessageAvatarAssistant"]::after {{
            content: ""; position: absolute; top: 50%; left: 50%;
            width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px;
            border-radius: 50%; background: var(--accent);
            box-shadow: 0 0 0 0 rgba(74,222,128,.55);
            animation: dot-breathe 2.4s ease-in-out infinite;
        }}
        @keyframes dot-breathe {{
            0%, 100% {{ transform: scale(.82); opacity: .65;
                        box-shadow: 0 0 0 0 rgba(74,222,128,.5); }}
            50%       {{ transform: scale(1.08); opacity: 1;
                        box-shadow: 0 0 0 7px rgba(74,222,128,0); }}
        }}

        /* «Думаю...» — три бегущие точки вместо спиннера Streamlit */
        .thinking {{
            display: flex; align-items: center; gap: 9px;
            padding: 3px 2px; font-size: 14px; color: var(--text-dim);
        }}
        .thinking .dots {{ display: inline-flex; gap: 4px; }}
        .thinking .dots span {{
            width: 6px; height: 6px; border-radius: 50%;
            background: var(--accent); opacity: .35;
            animation: dot-wave 1.3s ease-in-out infinite;
        }}
        .thinking .dots span:nth-child(2) {{ animation-delay: .18s; }}
        .thinking .dots span:nth-child(3) {{ animation-delay: .36s; }}
        @keyframes dot-wave {{
            0%, 60%, 100% {{ opacity: .3; transform: translateY(0) scale(.85); }}
            30%           {{ opacity: 1;  transform: translateY(-4px) scale(1); }}
        }}
        /* мягкое проявление ответа вместо резкой подстановки текста */
        [data-testid="stChatMessage"] {{ animation: rise .28s ease-out; }}
        @keyframes rise {{
            from {{ opacity: 0; transform: translateY(6px); }}
            to   {{ opacity: 1; transform: translateY(0); }}
        }}
        [data-testid="stExpander"] {{
            background-color: rgba(28, 30, 28, .45);
            backdrop-filter: blur(16px);
            border-radius: 12px; border: 1px solid rgba(255,255,255,.06);
        }}
        [data-testid="stStatus"] {{
            background-color: var(--input); border: 1px solid var(--border); border-radius: 12px;
        }}

        .tool-badge {{
            display: inline-flex; align-items: center; gap: 5px;
            padding: 3px 9px; border-radius: 7px; font-size: 12px; font-weight: 500;
            margin-right: 5px; margin-bottom: 5px;
            border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
            background-color: var(--accent-soft);
            color: var(--accent);
        }}
        .tool-badge .material-symbols-rounded {{ font-size: 14px; }}

        /* ШАГ TRACE: каждый вызов инструмента — отдельная карточка с шапкой и телом,
           вместо плоского списка внутри expander. Так видно структуру рассуждения. */
        .trace-step {{
            background-color: rgba(31, 34, 31, .5);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255,255,255,.06);
            border-radius: 10px; margin-bottom: 7px; overflow: hidden;
        }}
        .trace-head {{
            display: flex; align-items: center; gap: 7px;
            padding: 7px 11px; background-color: rgba(255,255,255,.03);
            border-bottom: 1px solid var(--border);
            font-size: 12.5px; font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
        }}
        .trace-head .step-num {{
            display: inline-flex; align-items: center; justify-content: center;
            width: 17px; height: 17px; border-radius: 5px; flex-shrink: 0;
            background-color: var(--accent-soft); color: var(--accent);
            font-size: 10.5px; font-weight: 600;
        }}
        .trace-head .tool-name {{ color: var(--accent); font-weight: 600; }}
        .trace-head .tool-args {{
            color: var(--text-dim); overflow: hidden;
            text-overflow: ellipsis; white-space: nowrap;
        }}
        .trace-body {{
            padding: 8px 11px; font-size: 12.5px; line-height: 1.55;
            color: var(--text-dim); white-space: pre-wrap; word-break: break-word;
            max-height: 160px; overflow-y: auto;
        }}

        /* ПАНЕЛЬ ДЕЙСТВИЙ под ответом: кнопки-иконки без рамок, проявляются при наведении */
        .msg-actions .stButton > button {{
            background: transparent; border: none; color: var(--text-dim);
            padding: 2px 7px; min-height: 0; font-size: 13px;
            backdrop-filter: none;
        }}
        .msg-actions .stButton > button:hover {{
            background-color: var(--raised); color: var(--accent);
        }}
        .memory-fact {{
            background-color: rgba(28, 30, 28, .5);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,.05);
            border-left: 2px solid var(--accent);
            border-radius: 8px; padding: 6px 10px;
            margin-bottom: 4px; font-size: 12.5px; line-height: 1.45;
            color: var(--text);
        }}
        .hero-card {{
            background-color: rgba(22, 24, 22, .42);
            backdrop-filter: blur(24px) saturate(150%);
            -webkit-backdrop-filter: blur(24px) saturate(150%);
            border: 1px solid rgba(255,255,255,.08); border-radius: 20px;
            padding: 34px 28px; text-align: center; margin: 40px auto 24px auto; max-width: 520px;
            box-shadow: 0 10px 40px rgba(0,0,0,.35),
                        inset 0 1px 0 rgba(255,255,255,.07);
        }}
        /* подсказки-примеры под приветствием */
        .hint-chip {{
            display: inline-block; padding: 5px 11px; margin: 3px;
            border-radius: 8px; font-size: 12.5px;
            background-color: var(--input); border: 1px solid var(--border);
            color: var(--text-dim);
        }}

        /* заголовок секции в сайдбаре */
        .side-head {{
            display: flex; align-items: center; gap: 6px;
            font-size: 11px; font-weight: 600; letter-spacing: .06em;
            text-transform: uppercase; color: var(--text-dim);
            margin: 2px 0 6px 0;
        }}
        .side-head .material-symbols-rounded {{ font-size: 15px; color: var(--accent); }}
        /* строка статуса: модель, число фактов */
        .status-row {{
            display: flex; align-items: center; justify-content: space-between;
            font-size: 11.5px; color: var(--text-dim);
            padding: 5px 9px; border-radius: 7px;
            background-color: rgba(28, 30, 28, .5);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,.06);
        }}
        .status-dot {{
            display: inline-block; width: 6px; height: 6px; border-radius: 50%;
            background-color: var(--accent); margin-right: 5px;
        }}

        /* встроенные компоненты Streamlit красятся ЕГО темой — принудительно
           подчиняем их нашим переменным, иначе при ручном выборе темы разъезжаются */
        [data-testid="stCaptionContainer"], .stCaption, [data-testid="stMarkdownContainer"] p {{
            color: var(--text);
        }}
        [data-testid="stCaptionContainer"] {{ color: var(--text-dim) !important; }}
        [data-testid="stToast"] {{
            background-color: var(--card) !important; color: var(--text) !important;
            border: 1px solid var(--border) !important;
        }}
        [data-testid="stExpander"] summary, [data-testid="stExpander"] p {{ color: var(--text); }}
        [data-testid="stChatInput"] textarea {{ color: var(--text) !important; }}
        [data-testid="stChatInput"] textarea::placeholder {{ color: var(--text-dim) !important; }}

        /* виджеты со своим фоном: без этого в тёмной теме остаются светлыми,
           потому что базовая тема в config.toml — light */
        [data-testid="stFileUploader"] section,
        [data-testid="stFileUploaderDropzone"] {{
            background-color: rgba(28, 30, 28, .45) !important;
            backdrop-filter: blur(12px);
            border-color: rgba(255,255,255,.07) !important;
            color: var(--text) !important;
        }}
        [data-testid="stFileUploader"] small {{ color: var(--text-dim) !important; }}
        /* кнопка Upload: без явного цвета она серая на сером фоне дропзоны */
        [data-testid="stFileUploader"] button {{
            background-color: var(--card) !important;
            color: var(--text) !important;
            border: 1px solid var(--border) !important;
        }}
        [data-testid="stFileUploader"] button:hover {{
            border-color: var(--accent) !important;
            color: var(--accent) !important;
        }}
        [data-testid="stAlert"] {{
            background-color: var(--card) !important;
            color: var(--text) !important;
            border: 1px solid var(--border) !important;
        }}
        [data-testid="stAlert"] p {{ color: var(--text) !important; }}
        [data-testid="stSpinner"] p {{ color: var(--text-dim) !important; }}
        [data-testid="stStatus"] p, [data-testid="stStatus"] summary {{ color: var(--text) !important; }}
        hr {{ border-color: var(--border) !important; }}
    </style>
    """, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("""<div style="display:flex;align-items:center;gap:9px;margin-bottom:14px;">
            <span class="material-symbols-rounded"
                  style="font-size:24px;color:var(--accent);">school</span>
            <div style="line-height:1.25;">
                <div style="font-size:16px;font-weight:600;color:var(--text);">Study Assistant</div>
                <div style="font-size:11px;color:var(--text-dim);">RAG + ReAct агент</div>
            </div>
        </div>""", unsafe_allow_html=True)

        # подтверждаем только если переписка непустая — иначе лишний клик на ровном месте
        if st.session_state.get("confirm_new_chat"):
            st.warning("Начать заново? Текущая переписка исчезнет.", icon=":material/warning:")
            c1, c2 = st.columns(2)
            if c1.button("Да, начать", use_container_width=True):
                st.session_state.messages = []
                st.session_state.confirm_new_chat = False
                st.session_state.pop("greeting", None)   # новый чат — новое приветствие
                st.rerun()
            if c2.button("Отмена", use_container_width=True, key="cancel_new_chat"):
                st.session_state.confirm_new_chat = False
                st.rerun()
        else:
            if st.button("Новый чат", icon=":material/add:", use_container_width=True):
                if st.session_state.get("messages"):
                    st.session_state.confirm_new_chat = True
                st.rerun()

        st.divider()
        st.markdown("""<div class="side-head">
            <span class="material-symbols-rounded">description</span>Конспекты
        </div>""", unsafe_allow_html=True)

        try:
            status = rag.notes_status()
            if status["indexed"] and status["chunks"]:
                st.markdown(
                    f"""<div class="status-row">
                        <span><span class="status-dot"></span>Проиндексировано</span>
                        <span>{status['chunks']} фрагм.</span>
                    </div>""", unsafe_allow_html=True)
            else:
                st.caption("Конспект не загружен — отвечу из Википедии")
        except Exception:
            st.caption("Qdrant недоступен")

        uploaded = st.file_uploader("Загрузить конспект", type=["txt", "md"],
                                    label_visibility="collapsed")
        # индексируем только новый файл: без этой проверки Streamlit перезапускал бы
        # индексацию на каждом действии в UI, пока файл висит в аплоадере
        if uploaded is not None and st.session_state.get("indexed_file") != uploaded.name:
            try:
                text = uploaded.getvalue().decode("utf-8")
            except UnicodeDecodeError:
                st.error("Не удалось прочитать файл — нужен UTF-8")
            else:
                with st.spinner("Индексирую конспект..."):
                    try:
                        n = rag.index_document(text)
                        st.session_state.indexed_file = uploaded.name
                        st.success(f"Готово: {n} фрагментов")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Ошибка индексации: {e}")

        st.divider()
        st.markdown("""<div class="side-head">
            <span class="material-symbols-rounded">psychology</span>Память
        </div>""", unsafe_allow_html=True)

        try:
            pairs = get_facts_with_ids()
            if pairs:
                for pid, fact in pairs:
                    col_fact, col_del = st.columns([6, 1])
                    # escape: текст факта сгенерирован моделью и попадает в HTML-блок
                    col_fact.markdown(
                        f'<div class="memory-fact">{html.escape(str(fact))}</div>',
                        unsafe_allow_html=True)
                    # key по id точки — иначе Streamlit спутает кнопки между перерисовками
                    if col_del.button(":material/close:", key=f"del_{pid}",
                                      help="Забыть этот факт"):
                        delete_fact(pid)
                        st.rerun()
            else:
                st.caption("Пока ничего не сохранено")
        except Exception:
            st.caption("Память недоступна (проверь Qdrant)")

        # двухшаговое подтверждение — один случайный клик не должен стирать всё накопленное
        if st.session_state.get("confirm_wipe"):
            st.warning("Удалить все факты о тебе?", icon=":material/warning:")
            col_yes, col_no = st.columns(2)
            if col_yes.button("Да, удалить", use_container_width=True):
                if client.collection_exists(MEMORY_COLLECTION):
                    client.delete_collection(MEMORY_COLLECTION)
                ensure_memory_collection()
                st.session_state.confirm_wipe = False
                st.rerun()
            if col_no.button("Отмена", use_container_width=True):
                st.session_state.confirm_wipe = False
                st.rerun()
        else:
            if st.button("Очистить память", icon=":material/delete:", use_container_width=True):
                st.session_state.confirm_wipe = True
                st.rerun()

    # ============================================================
    # ОСНОВНОЙ ЧАТ
    # ============================================================
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # chat_input читаем ДО отрисовки приветствия: иначе на прогоне с первым вопросом
    # карточка успевает нарисоваться и висит над диалогом до следующей перерисовки
    question = st.chat_input("Спроси что-нибудь...", max_chars=2000)

    # повтор ответа: вопрос уже снят с истории кнопкой «Ответить заново»
    if not question and st.session_state.get("regenerate"):
        question = st.session_state.pop("regenerate")

    if len(st.session_state.messages) == 0 and not question:
        hints = ["Что такое фишинг?", "Посчитай 17 * 23", "Что ты обо мне знаешь?"]
        chips = "".join(f'<span class="hint-chip">{h}</span>' for h in hints)
        st.markdown(f"""
        <div class="hero-card">
            <span class="material-symbols-rounded" style="font-size:38px;color:var(--accent);">school</span>
            <div style="font-size:24px;font-weight:600;margin:6px 0;color:var(--text);">{greeting_title()}</div>
            <div style="font-size:13.5px;color:var(--text-dim);margin-bottom:14px;">
                Спрашивай про конспекты, факты, считай, или просто общайся — я помню о тебе между сессиями
            </div>
            <div>{chips}</div>
        </div>
        """, unsafe_allow_html=True)

    for idx, message in enumerate(st.session_state.messages):
        if message["role"] == "user":
            st.markdown(
                f'<div class="user-row"><div class="user-bubble">'
                f'{html.escape(str(message["content"]))}</div></div>',
                unsafe_allow_html=True)
            continue

        with st.chat_message("assistant", avatar=":material/smart_toy:"):
            st.markdown(message["content"])
            render_trace(message.get("trace") or [], key=f"hist{idx}")
            if message.get("new_facts"):
                st.caption(f":material/save: Запомнил: {', '.join(message['new_facts'])}")

            # действия доступны только у последнего ответа: повторять запрос
            # из середины истории означало бы переписывать всё, что после него
            if idx == len(st.session_state.messages) - 1:
                st.markdown('<div class="msg-actions">', unsafe_allow_html=True)
                act_copy, act_again, _ = st.columns([1, 1, 6])
                if act_copy.button(":material/content_copy:", key=f"copy{idx}",
                                   help="Показать текст для копирования"):
                    st.session_state.show_copy = not st.session_state.get("show_copy", False)
                if act_again.button(":material/refresh:", key=f"again{idx}",
                                    help="Ответить заново"):
                    # снимаем ответ и вопрос — вопрос вернётся в обработку ниже
                    st.session_state.messages.pop()
                    prev = st.session_state.messages.pop()
                    st.session_state.regenerate = prev["content"]
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

                if st.session_state.get("show_copy"):
                    # st.code даёт родную кнопку копирования — своя на HTML не кликается
                    st.code(message["content"], language=None)

    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        st.markdown(
            f'<div class="user-row"><div class="user-bubble">'
            f'{html.escape(str(question))}</div></div>',
            unsafe_allow_html=True)

        with st.chat_message("assistant", avatar=":material/smart_toy:"):
            # человекочитаемые подписи для статуса — иначе видно голые имена функций
            TOOL_LABELS = {
                "search_notes": "Ищу в конспектах",
                "search_wikipedia": "Смотрю Википедию",
                "calculate": "Считаю",
                "recall_memory": "Вспоминаю, что знаю о тебе",
            }
            answer, trace, new_facts = "", [], []
            try:
                # [:-1] — текущий вопрос уже в messages, передаём только предыдущие реплики
                history = st.session_state.messages[:-1]

                # свой индикатор вместо st.status: у того рамка, спиннер и плашка
                # «Готово» после завершения — визуальный мусор. Здесь три бегущие
                # точки и подпись текущего шага, всё стирается по готовности.
                status_slot = st.empty()

                def thinking(label: str) -> None:
                    status_slot.markdown(
                        f'<div class="thinking"><span class="dots">'
                        f'<span></span><span></span><span></span></span>'
                        f'{html.escape(label)}</div>',
                        unsafe_allow_html=True)

                thinking("Думаю")
                answer_box = st.empty()
                buf = []

                for ev in stream_study_assistant(question, history=history):
                    kind = ev["type"]
                    if kind == "tool":
                        thinking(TOOL_LABELS.get(ev["name"], ev["name"]))
                    elif kind == "token":
                        buf.append(ev["text"])
                        # точки убираем, как только пошёл текст ответа
                        status_slot.empty()
                        answer_box.markdown("".join(buf) + "▌")   # курсор как в настоящих чатах
                    elif kind == "done":
                        answer, trace, new_facts = ev["answer"], ev["trace"], ev["new_facts"]

                status_slot.empty()
                answer_box.markdown(answer)   # финальный текст уже без курсора
            except (RerunException, StopException):
                # Streamlit реализует st.rerun()/st.stop() через исключения. Широкий
                # `except Exception` их проглатывал: сообщение стиралось из истории,
                # дерево элементов обрывалось на середине, и страница оказывалась пустой
                # без единой ошибки в логах. Пропускаем их дальше — это не сбои.
                raise
            except Exception as e:
                err = str(e)
                if "llama-server" in err or "CUDA" in err:
                    msg = ("Модель не смогла запуститься на видеокарте. "
                           "Перезапусти Ollama или снизь NUM_GPU_LAYERS.")
                elif "connect" in err.lower() or "refused" in err.lower():
                    msg = "Не могу подключиться к Ollama — проверь, что он запущен."
                else:
                    msg = "Что-то пошло не так при обработке запроса."

                st.error(msg, icon=":material/error:")
                with st.expander("Подробности ошибки"):
                    st.code(err[:1500])

                # вопрос уже в messages — убираем, чтобы история не осталась без ответа
                st.session_state.messages.pop()
                st.stop()

            # сам ответ уже отрисован в answer_box выше — здесь только метаданные
            render_trace(trace, key="live")
            if new_facts:
                st.caption(f":material/save: Запомнил: {', '.join(new_facts)}")

            st.session_state.messages.append({
                "role": "assistant", "content": answer, "trace": trace, "new_facts": new_facts
            })

            for fact in new_facts:
                st.toast(f"Запомнил: {fact}", icon=":material/psychology:")

        # ВНЕ блока `with st.chat_message(...)`: rerun изнутри вложенного контейнера
        # обрывает построение дерева элементов на середине — Streamlit перезапускает
        # скрипт, не закрыв контейнеры, и клиент получает поломанное дерево.
        # Выглядело это как «UI исчезает при отправке сообщения».
        # Сам rerun нужен: сайдбар рисуется раньше ответа, и без него новый факт
        # появился бы в памяти только после следующего сообщения.
        if new_facts:
            st.rerun()