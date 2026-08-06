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
        html, body, #root, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background-color: var(--bg);
        }}
        .stApp {{
            background: radial-gradient(circle at 15% 100%, var(--glow) 0%, transparent 45%), var(--bg);
            color: var(--text);
        }}
        section[data-testid="stSidebar"] {{
            background-color: var(--sidebar);
            border-right: 1px solid var(--border);
        }}
        /* Раньше здесь было сплошное `header {{visibility: hidden}}` — оно прятало и
           кнопку разворота сайдбара, поэтому закрытый сайдбар нельзя было вернуть.
           Прячем только меню и футер, шапку лишь делаем прозрачной. */
        #MainMenu, footer {{ visibility: hidden; }}
        header[data-testid="stHeader"] {{ background: transparent; }}

        [data-testid="stChatMessage"] {{
            background-color: var(--card); border-radius: 16px; padding: 12px 16px;
            border: 1px solid var(--border); box-shadow: var(--shadow);
        }}
        .stButton > button {{
            background-color: var(--input); color: var(--text);
            border-radius: 12px; border: 1px solid var(--border);
            transition: border-color .15s ease, color .15s ease;
        }}
        .stButton > button:hover {{ border-color: var(--accent); color: var(--accent); }}
        [data-testid="stChatInput"] {{
            border-radius: 28px !important; border: 1px solid var(--border) !important;
            background-color: var(--input) !important;
        }}
        [data-testid="stExpander"] {{
            background-color: var(--input); border-radius: 12px; border: 1px solid var(--border);
        }}
        [data-testid="stStatus"] {{
            background-color: var(--input); border: 1px solid var(--border); border-radius: 12px;
        }}

        .tool-badge {{
            display: inline-block; padding: 4px 10px; border-radius: 8px; font-size: 12px;
            margin-right: 6px; margin-bottom: 6px;
            border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
            background-color: color-mix(in srgb, var(--accent) 12%, transparent);
            color: var(--accent);
        }}
        .memory-fact {{
            background-color: var(--input); border-radius: 10px; padding: 8px 12px;
            margin-bottom: 6px; font-size: 13px; border-left: 3px solid var(--accent);
            color: var(--text); box-shadow: var(--shadow);
        }}
        .hero-card {{
            background-color: var(--card); border: 1px solid var(--border); border-radius: 20px;
            padding: 40px 30px; text-align: center; margin: 60px auto 30px auto; max-width: 500px;
            box-shadow: var(--shadow);
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
            background-color: var(--input) !important;
            border-color: var(--border) !important;
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
        st.markdown("""<div style="display:flex;align-items:center;gap:8px;">
            <span class="material-symbols-rounded" style="font-size:22px;color:var(--accent);">school</span>
            <span style="font-size:18px;font-weight:600;color:var(--text);">Study Assistant</span>
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
        st.markdown("""<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;">
            <span class="material-symbols-rounded" style="font-size:18px;color:var(--accent);">description</span>
            <b style="color:var(--text);">Конспекты</b>
        </div>""", unsafe_allow_html=True)

        try:
            status = rag.notes_status()
            if status["indexed"] and status["chunks"]:
                st.caption(f"Проиндексировано: {status['chunks']} фрагментов")
            else:
                st.caption("Конспект не загружен — агент ответит только из Википедии")
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
        st.markdown("""<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;">
            <span class="material-symbols-rounded" style="font-size:18px;color:var(--accent);">psychology</span>
            <b style="color:var(--text);">Что я о тебе знаю</b>
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

    if len(st.session_state.messages) == 0 and not question:
        st.markdown(f"""
        <div class="hero-card">
            <span class="material-symbols-rounded" style="font-size:40px;color:var(--accent);">school</span>
            <div style="font-size:26px;font-weight:600;margin:8px 0;color:var(--text);">{greeting_title()}</div>
            <div style="font-size:14px;color:var(--text-dim);">
                Спрашивай про конспекты, факты, считай, или просто общайся — я помню о тебе между сессиями
            </div>
        </div>
        """, unsafe_allow_html=True)

    for message in st.session_state.messages:
        avatar = ":material/person:" if message["role"] == "user" else ":material/smart_toy:"
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])
            if message.get("trace"):
                badges = "".join([f'<span class="tool-badge">{t["tool"]}</span>' for t in message["trace"]])
                st.markdown(badges, unsafe_allow_html=True)
                with st.expander("Ход рассуждения (Thought → Action → Observation)", icon=":material/psychology:"):
                    for step in message["trace"]:
                        st.markdown(f"**{step['tool']}**({step['args']})")
                        st.caption(step["result"])
            if message.get("new_facts"):
                st.caption(f":material/save: Запомнил: {', '.join(message['new_facts'])}")

    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(question)

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

                status = st.status("Думаю...", expanded=False)
                answer_box = st.empty()
                buf = []

                for ev in stream_study_assistant(question, history=history):
                    kind = ev["type"]
                    if kind == "tool":
                        status.update(label=TOOL_LABELS.get(ev["name"], ev["name"]))
                        status.write(f"**{ev['name']}**({ev['args']})")
                    elif kind == "result":
                        status.write(ev["result"])
                    elif kind == "token":
                        buf.append(ev["text"])
                        answer_box.markdown("".join(buf) + "▌")   # курсор как в настоящих чатах
                    elif kind == "done":
                        answer, trace, new_facts = ev["answer"], ev["trace"], ev["new_facts"]

                status.update(label="Готово", state="complete")
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
            if trace:
                badges = "".join([f'<span class="tool-badge">{t["tool"]}</span>' for t in trace])
                st.markdown(badges, unsafe_allow_html=True)
                with st.expander("Ход рассуждения (Thought → Action → Observation)", icon=":material/psychology:"):
                    for step in trace:
                        st.markdown(f"**{step['tool']}**({step['args']})")
                        st.caption(step["result"])
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