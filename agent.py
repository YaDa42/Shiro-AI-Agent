import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixel coordinates at 125%/150% scaling
except Exception:
    pass

import asyncio, base64, io, json, logging, os, re, threading, time, urllib.parse, webbrowser, difflib, uuid
from pathlib import Path

import numpy as np
import pyautogui
import pyperclip
import requests
import uiautomation as auto
import yaml
import os
import sys
import time
import threading
from datetime import datetime
from flask import Flask, jsonify, request, Response
from PIL import ImageDraw
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BASE = Path(__file__).parent
cfg = yaml.safe_load(open(BASE / "config.yaml", encoding="utf-8"))
LLM = cfg["llm"]
AG = cfg.get("agent", {})
TG = cfg.get("telegram", {})
MAX_STEPS = AG.get("max_steps", 25)
WEB_HOST = AG.get("web_host", "127.0.0.1")
WEB_PORT = AG.get("web_port", 8000)
DESKTOP = (Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop").resolve()
HEADERS = {"User-Agent": "Mozilla/5.0"}

TG_TOKEN = TG.get("token", "") or os.environ.get("TELEGRAM_TOKEN", "")
TG_OWNER = int(TG.get("owner_id", 0) or 0)

LANG = {"current": "en"}  # English by default, switch with: lang ru / lang en

MESSAGES = {
    "en": {
        "help": """Commands:
  any phrase         run a task, e.g.: open word
  models             show available models
  provider ollama|groq|openai|gemini
  model <name>       choose model for the current provider
  lang en|ru         switch interface language
  stop               stop the agent
  exit               close the program""",
        "busy": "Agent is busy. Wait for it to finish or type 'stop'.",
        "stopping": "Stopping...",
        "stopped": "Stopped.",
        "done": "Done: ",
        "done_rejected": "Done is not accepted: the last action was not confirmed. Continuing.",
        "step_limit": "Step limit reached.",
        "repeat": "The same action repeats, stopping.",
        "ask_hint": "(answer in the console, the web panel or Telegram)",
        "web": "Web panel",
        "current": "Current",
        "lang_set": "Language: English",
        "lang_bad": "Use: lang en  or  lang ru",
        "provider_bad": "Use: provider ollama, groq, openai or gemini",
        "model_set": "Model",
        "task": "Task",
        "not_found": "text not found on screen",
        "clicked": "clicked",
        "no_api_key": "OpenAI API key is not set. Set the OPENAI_API_KEY environment variable.",
        "no_gemini_key": "Gemini API key is not set. Set the GEMINI_API_KEY environment variable or gemini_api_key in config.yaml.",
        "song_ask": "Which song should I play?",
        "song_open": "Opened YouTube Music. Pick the track manually.",
        "search_open": "Search opened.",
        "risky": "Risky action. Allow it? (yes/no)",
        "cancelled": "user cancelled",
        "error": "Error",
        "none_models": "(none, or Ollama is not running)",
        "clicks_stop": "Four clicks in a row did nothing. Stopping. Check whether the input field is visible, or try a task that uses type without clicks.",
        "screenshot_failed": "screenshot not sent: ",
        "retry": "Gemini is overloaded, waiting {wait} s and retrying ({i}/{tries})",
        "verify_ok": "expectation met",
        "verify_fail": "expectation not met",
        "verify_none": "expectation not checked",
        "guard_block": "action blocked by guard",
        "safety_block": "action blocked by safety check",
        "no_precondition": "precondition is missing, action not performed; ask the model to add it",
        "no_screenshot_for_safety": "safety check needs a screenshot, action blocked",
        "invalid_action": "invalid action, not performed",
        "path_bad": "path is outside the Desktop folder or has a bad name",
        "tg_ready": "Telegram bot is running.",
        "tg_disabled": "Telegram bot is off: set telegram.token and telegram.owner_id in config.yaml.",
        "tg_denied": "Access denied: this bot only takes commands from its owner.",
        "tg_welcome": "Shiro is ready. Send a task as a normal message, e.g.: open notepad.\nCommands: stop, models, lang en|ru",
    },
    "ru": {
        "help": """Команды:
  любая фраза        выполнить задачу, например: открой ворд
  модели             показать доступные модели
  провайдер ollama|groq|openai|gemini
  модель <имя>       выбрать модель для текущего провайдера
  lang en|ru         сменить язык интерфейса
  стоп               остановить агента
  выход              закрыть программу""",
        "busy": "Агент уже занят. Дождись окончания или напиши «стоп».",
        "stopping": "Останавливаю...",
        "stopped": "Остановлено.",
        "done": "Готово: ",
        "done_rejected": "Завершение не принято: последнее действие не подтверждено. Продолжаю.",
        "step_limit": "Лимит шагов исчерпан.",
        "repeat": "Одно и то же действие повторяется, останавливаюсь.",
        "ask_hint": "(ответь в консоли, в веб-панели или в Telegram)",
        "web": "Веб-панель",
        "current": "Сейчас",
        "lang_set": "Язык: русский",
        "lang_bad": "Пиши: lang en  или  lang ru",
        "provider_bad": "Пиши: провайдер ollama, groq, openai или gemini",
        "model_set": "Модель",
        "task": "Задача",
        "not_found": "текст на экране не найден",
        "clicked": "клик по",
        "no_api_key": "Не задан API-ключ OpenAI. Установи переменную OPENAI_API_KEY.",
        "no_gemini_key": "Не задан ключ Gemini. Установи переменную GEMINI_API_KEY или gemini_api_key в config.yaml.",
        "song_ask": "Какую песню включить?",
        "song_open": "Открыл YouTube Music. Выбери трек вручную.",
        "search_open": "Открыл поиск.",
        "risky": "Опасное действие. Выполнить? (да/нет)",
        "cancelled": "пользователь отменил",
        "error": "Ошибка",
        "none_models": "(нет или Ollama не запущена)",
        "clicks_stop": "Четыре клика подряд ничего не дали. Останавливаюсь. Проверь, видно ли поле ввода, или попробуй задачу с type без кликов.",
        "screenshot_failed": "скриншот не отправлен: ",
        "retry": "Gemini перегружен, жду {wait} сек и пробую снова ({i}/{tries})",
        "verify_ok": "ожидание выполнено",
        "verify_fail": "ожидание не выполнено",
        "verify_none": "ожидание не проверено",
        "guard_block": "действие заблокировано guard",
        "safety_block": "действие заблокировано проверкой безопасности",
        "no_precondition": "не указано условие (precondition), действие не выполнено; модели нужно его добавить",
        "no_screenshot_for_safety": "для проверки безопасности нужен скриншот, действие заблокировано",
        "invalid_action": "некорректное действие, не выполнено",
        "path_bad": "путь вне папки Desktop или недопустимое имя файла",
        "tg_ready": "Telegram-бот запущен.",
        "tg_disabled": "Telegram-бот выключен: задай telegram.token и telegram.owner_id в config.yaml.",
        "tg_denied": "Доступ запрещён: бот принимает команды только от владельца.",
        "tg_welcome": "Shiro готов. Напиши задачу обычным сообщением, например: открой блокнот.\nКоманды: стоп, модели, lang en|ru",
    },
}


def t(key: str) -> str:
    return MESSAGES[LANG["current"]].get(key, key)


pyautogui.FAILSAFE = True  # mouse to top-left corner = emergency stop
pyautogui.PAUSE = 0.3
logging.getLogger("werkzeug").setLevel(logging.ERROR)

GROQ_PRESETS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
]

OPENAI_PRESETS = [
    "gpt-4.1-mini",
    "gpt-4o-mini",
]

GEMINI_PRESETS = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
]

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

SET = {
    "provider": LLM.get("provider", "ollama"),
    "ollama_model": LLM.get("model", "qwen2.5vl:3b"),
    "ollama_host": LLM.get("host", "http://localhost:11434"),
    "groq_model": LLM.get("groq_model") or GROQ_PRESETS[0],
    "groq_key": LLM.get("groq_api_key", ""),
    "openai_model": LLM.get("openai_model", "gpt-4.1-mini"),
    "openai_key": os.environ.get("OPENAI_API_KEY", LLM.get("openai_api_key", "")),
    "gemini_model": LLM.get("gemini_model", GEMINI_PRESETS[0]),
    "gemini_key": os.environ.get("GEMINI_API_KEY", LLM.get("gemini_api_key", "")),
    "use_screenshot": LLM.get("use_screenshot", True),
    "verify": LLM.get("verify", True),
    "guard": LLM.get("guard", True),
    "safety_guard": LLM.get("safety_guard", False),
    # If true, done is accepted without a confirmed check. Off by default.
    "trust_done": LLM.get("trust_done", False),
}

SYSTEM = """Ты управляешь Windows-компьютером. Выполняй задачу сам, шаг за шагом.
Ответ — ОДИН JSON-объект, без пояснений и без текста после него. Допустимые действия:

{"action": "open_app", "name": "paint", "expect": "окно Paint открыто"}
{"action": "open_url", "url": "https://...", "expect": "страница загружена"}
{"action": "click_text", "text": "Сохранить", "precondition": "диалог открыт", "expect": "диалог сохранения открыт"}
{"action": "double_click_text", "text": "Файл", "precondition": "...", "expect": "..."}
{"action": "click", "x": 100, "y": 200, "precondition": "...", "expect": "..."}  координаты с сетки (для полей и иконок без подписи)
{"action": "type", "text": "...", "precondition": "поле ввода активно", "expect": "текст виден в поле"}
{"action": "hotkey", "keys": ["enter"], "precondition": "текст введён", "expect": "сообщение отправлено"}
{"action": "scroll", "amount": -500}
{"action": "wait", "seconds": 2}
{"action": "find_images", "query": "..."}
{"action": "download", "url": "...", "filename": "...", "expect": "файл скачан"}
{"action": "read_page", "url": "..."}
{"action": "save_screenshot", "filename": "shot.png", "expect": "файл сохранён"}
{"action": "write_file", "filename": "notes.txt", "content": "...", "expect": "файл записан"}
{"action": "ask_user", "question": "..."}
{"action": "done", "summary": "что сделано"}

Правила:
1. Не спрашивай, если задача понятна.
2. Сначала открой нужное приложение или сайт (open_app / open_url), потом действуй в нём.
3. Печатай в поле только текст, который пользователь попросил написать. Текст команды (например, «напиши в ChatGPT ...») в поле не вставляй.
4. Чтобы напечатать в поле ввода: если поле уже активно после открытия страницы, сразу используй type без клика. Иначе нажми на него (click_text по подписи поля, а если подписи нет, click по координатам с сетки), затем type.
5. Чтобы отправить сообщение: после ввода текста вызови hotkey ["enter"].
6. После каждого действия смотри следующий скриншот. Если поле не получило текст или страница не изменилась, не повторяй тот же клик больше двух раз.
7. Для кнопок, ссылок и пунктов меню используй click_text и точно копируй текст с экрана. Координаты для них не придумывай.
8. Если click_text ответил «не найден», попробуй другую формулировку или другое действие.
9. ask_user только если без ответа невозможно.
10. Вызывай done только тогда, когда на экране виден результат задачи.
11. Для каждого действия, кроме wait, ask_user и done, добавляй "expect": что должно быть видно на экране после действия. Для click_text, click, double_click_text, type, hotkey обязательно добавляй "precondition": что должно быть видно ДО действия.
12. Если в истории есть «ПРОВЕРКА НЕ ПРОШЛА», «ПРОВЕРКА НЕ ВЫПОЛНЕНА», «ОТМЕНЕНО GUARD», «ОТМЕНЕНО ПРОВЕРКОЙ БЕЗОПАСНОСТИ» или «не указано условие», не вызывай done. Повтори действие с исправлением, выполни условие или выбери другой путь.
"""

VERIFY_SYSTEM = """Ты проверяешь, совпадает ли экран с ожидаемым результатом.
Тебе дают ожидание и скриншот экрана ПОСЛЕ действия.
Ответь ОДНИМ JSON-объектом без пояснений:
{"ok": true, "reason": "коротко"}  — на экране видно то, что ожидалось
{"ok": false, "reason": "коротко"} — ожидаемого не видно
"""

GUARD_SYSTEM = """Ты проверяешь, готов ли экран к следующему действию.
Тебе дают условие и скриншот экрана ДО действия.
Ответь ОДНИМ JSON-объектом без пояснений:
{"ok": true, "reason": "коротко"}  — условие выполнено, действие можно делать
{"ok": false, "reason": "коротко"} — условие не выполнено
"""

SAFETY_SYSTEM = """Ты оцениваешь безопасность действия на компьютере перед его выполнением.
Тебе дают цель пользователя, предлагаемое действие и скриншот экрана ДО действия.
Ответь ОДНИМ JSON-объектом без пояснений:
{"ok": true, "reason": "коротко"}  — действие соответствует цели и не вредит (не удаляет данные, не отправляет чужим людям, не платит, не меняет системные настройки без необходимости)
{"ok": false, "reason": "коротко"} — действие опасно, не относится к цели или может навредить
"""

ALIASES = {
    "terminate": "done",
    "finish": "done",
    "complete": "done",
    "open": "open_app",
    "click_on_text": "click_text",
}
RISKY = ("удал", "delete", "отправ", "оплат", "перезапис", "format", "install", "установ")

# ---------- action allowlist: schema for every allowed action ----------
# Each entry: required fields and their types. Unknown actions and extra/missing/wrong-type fields are rejected.
ACTION_SCHEMA = {
    "open_app": {"name": str},
    "open_url": {"url": str},
    "click_text": {"text": str},
    "double_click_text": {"text": str},
    "click": {"x": int, "y": int},
    "type": {"text": str},
    "hotkey": {"keys": list},
    "scroll": {"amount": int},
    "wait": {"seconds": (int, float)},
    "find_images": {"query": str},
    "download": {"url": str, "filename": str},
    "read_page": {"url": str},
    "save_screenshot": {"filename": str},
    "write_file": {"filename": str, "content": str},
    "ask_user": {"question": str},
    "done": {"summary": str},
}
# Optional fields that may appear next to the required ones
OPTIONAL_FIELDS = {"expect", "precondition"}
URL_PREFIXES = ("http://", "https://")
MAX_TEXT = 5000


def validate_action(a: dict):
    """Returns (True, "") if the action is allowed, or (False, reason) if it must not run."""
    if not isinstance(a, dict):
        return False, "action is not an object"
    kind = a.get("action")
    if kind not in ACTION_SCHEMA:
        return False, f"unknown action: {kind!r}"
    schema = ACTION_SCHEMA[kind]
    allowed = set(schema) | {"action"} | OPTIONAL_FIELDS
    extra = set(a) - allowed
    if extra:
        return False, f"unexpected fields: {sorted(extra)}"
    for field, ftype in schema.items():
        if field not in a:
            return False, f"missing field: {field}"
        value = a[field]
        if isinstance(value, bool) or not isinstance(value, ftype):
            return False, f"wrong type for {field}"
        if isinstance(value, str) and len(value) > MAX_TEXT:
            return False, f"{field} is too long"
    if kind == "open_url" and not a["url"].startswith(URL_PREFIXES):
        return False, "url must start with http:// or https://"
    if kind == "hotkey" and not (1 <= len(a["keys"]) <= 4 and all(isinstance(k, str) for k in a["keys"])):
        return False, "keys must be a list of 1 to 4 strings"
    if kind == "scroll" and not (-5000 <= a["amount"] <= 5000):
        return False, "scroll amount out of range"
    if kind == "wait" and not (0 <= a["seconds"] <= 60):
        return False, "wait seconds out of range"
    if kind in ("click", "double_click_text") and "x" in a:
        if not (0 <= a["x"] <= 10000 and 0 <= a["y"] <= 10000):
            return False, "coordinates out of range"
    return True, ""


STATE = {"busy": False, "stop": False, "question": None, "last": ""}
ANSWER = {"text": ""}
ANSWER_EVENT = threading.Event()
TG_APP = {"app": None, "loop": None, "chat_id": None}


# ---------- scheduler ----------
SCHEDULE_FILE = BASE / "scheduled_tasks.json"
SCHEDULE_LOCK = threading.RLock()
SCHEDULES = []

def load_schedules():
    global SCHEDULES
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        SCHEDULES = data if isinstance(data, list) else []
    except FileNotFoundError:
        SCHEDULES = []
    except Exception as e:
        print(f"Scheduler: could not load schedule file: {e}", flush=True)
        SCHEDULES = []

def save_schedules():
    with SCHEDULE_LOCK:
        temp = SCHEDULE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(SCHEDULES, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(SCHEDULE_FILE)

def scheduler_loop():
    while True:
        now = datetime.now()
        due = None
        with SCHEDULE_LOCK:
            for item in SCHEDULES:
                if item.get("enabled", True):
                    try:
                        when = datetime.fromisoformat(item["run_at"])
                    except Exception:
                        item["enabled"] = False
                        continue
                    if when <= now:
                        due = item
                        break
            if due:
                if STATE["busy"]:
                    due["run_at"] = (now + timedelta(minutes=1)).isoformat(timespec="minutes")
                    due["last_result"] = "Отложено: Shiro был занят"
                    save_schedules()
                    due = None
                else:
                    due["enabled"] = False
                    due["last_result"] = "Запускается"
                    save_schedules()
        if due:
            goal = str(due.get("goal", "")).strip()
            if goal:
                started = start_goal(goal)
                with SCHEDULE_LOCK:
                    due["last_result"] = "Запущено" if started else "Не удалось запустить"
                    save_schedules()
        time.sleep(1)

def start_scheduler():
    load_schedules()
    threading.Thread(target=scheduler_loop, daemon=True, name="ShiroScheduler").start()

# Actions that change the screen and therefore need a precondition and a guard
CAN_GUARD = ("click", "click_text", "double_click_text", "type", "hotkey")
# Actions that never need a check
NO_CHECK = ("wait", "ask_user", "done")
# Actions with side effects outside the screen: need a safety check even if it is not a screen action
SIDE_EFFECT = ("download", "write_file", "save_screenshot", "open_app", "open_url")


class StopRun(Exception):
    pass


class UnsafePath(Exception):
    pass


def out(msg: str):
    """Shows a message in the console, the web panel and Telegram."""
    STATE["last"] = msg
    print(msg, flush=True)
    tg_send(msg)


def ask(question: str) -> str:
    """Asks the user. Answer can be given in the console, the web panel or Telegram."""
    STATE["question"] = question
    ANSWER_EVENT.clear()
    print(f"\n❓ {question}\n{t('ask_hint')}", flush=True)
    tg_send(f"❓ {question}\n{t('ask_hint')}")
    while not ANSWER_EVENT.wait(timeout=0.5):
        if STATE["stop"]:
            STATE["question"] = None
            raise StopRun()
    STATE["question"] = None
    if STATE["stop"]:
        raise StopRun()
    return ANSWER["text"]


# ---------- safe paths: only inside the Desktop folder ----------
def safe_desktop_path(filename: str) -> Path:
    """Returns a path inside the Desktop folder. Rejects any name with '..', separators or drive colons."""
    name = str(filename or "").strip()
    if not name or ".." in name or "/" in name or "\\" in name or ":" in name:
        raise UnsafePath(t("path_bad"))
    DESKTOP.mkdir(exist_ok=True)
    path = (DESKTOP / name).resolve()
    if path.parent != DESKTOP:
        raise UnsafePath(t("path_bad"))
    return path


# ---------- Telegram ----------
def tg_send(text: str):
    """Sends a text message to the owner. Safe to call from any thread."""
    app, loop, chat_id = TG_APP["app"], TG_APP["loop"], TG_APP["chat_id"]
    if not app or not loop or not chat_id:
        return
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id, text=text[:4000]), loop
    )


def tg_send_screenshot(caption: str = ""):
    """Takes a screenshot of the screen and sends it to the owner. Kept in memory only."""
    app, loop, chat_id = TG_APP["app"], TG_APP["loop"], TG_APP["chat_id"]
    if not app or not loop or not chat_id:
        return
    buf = io.BytesIO()
    pyautogui.screenshot().convert("RGB").save(buf, "PNG")
    buf.seek(0)

    async def _send():
        await app.bot.send_photo(chat_id=chat_id, photo=buf, caption=caption[:1000])

    asyncio.run_coroutine_threadsafe(_send(), loop)


async def tg_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.effective_user.id != TG_OWNER:
        await update.message.reply_text(t("tg_denied"))
        return
    TG_APP["chat_id"] = update.effective_chat.id
    await update.message.reply_text(t("tg_welcome"))


async def tg_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.effective_user.id != TG_OWNER:
        await update.message.reply_text(t("tg_denied"))
        return
    TG_APP["chat_id"] = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return
    if STATE["question"]:
        ANSWER["text"] = text
        ANSWER_EVENT.set()
        return
    handle_command(text)


def start_telegram():
    if not TG_TOKEN or not TG_OWNER:
        print(t("tg_disabled"), flush=True)
        return

    async def _main():
        app = Application.builder().token(TG_TOKEN).build()
        app.add_handler(CommandHandler("start", tg_start_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_on_message))
        TG_APP["app"] = app
        TG_APP["loop"] = asyncio.get_running_loop()
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        print(t("tg_ready"), flush=True)
        await asyncio.Event().wait()

    threading.Thread(target=lambda: asyncio.run(_main()), daemon=True).start()


# ---------- screenshot with grid (for the model) ----------
def grab_screen():
    img = pyautogui.screenshot().convert("RGB")
    real_w, real_h = img.size
    scale = 1280 / real_w if real_w > 1280 else 1.0
    small = img.resize((round(real_w * scale), round(real_h * scale)))
    draw = ImageDraw.Draw(small)
    step = 100
    for rx in range(0, real_w, step):
        x = round(rx * scale)
        draw.line([(x, 0), (x, small.height)], fill=(255, 0, 0), width=1)
        draw.text((x + 2, 2), str(rx), fill=(255, 0, 0))
    for ry in range(0, real_h, step):
        y = round(ry * scale)
        draw.line([(0, y), (small.width, y)], fill=(255, 0, 0), width=1)
        draw.text((2, y + 2), str(ry), fill=(255, 0, 0))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode(), real_w, real_h


# ---------- finding text on screen ----------
def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", (s or "").lower())


def _word_hit(w: str, words: set) -> bool:
    if w in words:
        return True
    return len(w) >= 3 and any(len(x) >= 3 and (w in x or x in w) for x in words)


def _score(target: str, name: str) -> float:
    tn, nn = _norm(target), _norm(name)
    if not tn or not nn:
        return 0.0
    if tn == nn:
        return 1.0
    if len(tn) >= 2 and tn in nn:
        return 0.95
    if len(nn) >= 3 and nn in tn:
        return 0.85
    t_words = [_norm(w) for w in re.split(r"\s+", target.lower()) if _norm(w)]
    n_words = {_norm(w) for w in re.split(r"\s+", name.lower()) if _norm(w)}
    word_score = 0.0
    if t_words and n_words:
        word_score = sum(_word_hit(w, n_words) for w in t_words) / len(t_words) * 0.9
    return max(word_score, difflib.SequenceMatcher(None, tn, nn).ratio())


def _key(score: float, name: str):
    return (round(score, 3), -len(name or ""))


def _line_point(target: str, line: str, box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    low = line.lower()
    frac = 0.5
    tl = target.lower().strip()
    if tl and tl in low:
        idx = low.find(tl)
        frac = (idx + len(tl) / 2) / max(len(low), 1)
    else:
        for w in re.split(r"\s+", tl):
            if w and w in low:
                idx = low.find(w)
                frac = (idx + len(w) / 2) / max(len(low), 1)
                break
    return int(x1 + (x2 - x1) * frac), int((y1 + y2) / 2)


def _uia_candidate(text: str, max_nodes: int = 6000):
    try:
        with auto.UIAutomationInitializerInThread():
            root = auto.GetForegroundControl()
            if root is None:
                return None
            best = None
            stack = [(root, 0)]
            visited = 0
            while stack and visited < max_nodes:
                ctrl, depth = stack.pop()
                visited += 1
                if depth > 0 and ctrl.Name:
                    s = _score(text, ctrl.Name)
                    if s >= 0.6:
                        rect = ctrl.BoundingRectangle
                        if rect.width() > 0 and rect.height() > 0:
                            if best is None or _key(s, ctrl.Name) > _key(best[0], best[3]):
                                best = (s, rect.xcenter(), rect.ycenter(), ctrl.Name)
                if depth < 30:
                    try:
                        for ch in ctrl.GetChildren():
                            stack.append((ch, depth + 1))
                    except Exception:
                        pass
            if best:
                return best[0], best[1], best[2], f"UIA «{best[3][:40]}»"
            return None
    except Exception as e:
        out(f"UIA unavailable: {e}")
        return None


_ocr_engine = None


def _ocr_candidate(text: str):
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    img = np.array(pyautogui.screenshot().convert("RGB"))
    result, _ = _ocr_engine(img)
    if not result:
        return None
    best = None
    for box, line, conf in result:
        s = _score(text, line)
        if s >= 0.6 and (best is None or _key(s, line) > _key(best[0], best[3])):
            x, y = _line_point(text, line, box)
            best = (s, x, y, line)
    if best:
        return best[0], best[1], best[2], f"OCR «{best[3][:40]}»"
    return None


def find_text(text: str):
    cands = []
    uia = _uia_candidate(text)
    if uia:
        cands.append(uia)
    if not uia or uia[0] < 0.95:
        try:
            ocr = _ocr_candidate(text)
            if ocr:
                cands.append(ocr)
        except Exception as e:
            out(f"OCR unavailable: {e}")
    if not cands:
        return None
    best = max(cands, key=lambda c: c[0])
    return best[1], best[2], f"{best[3]} ({best[0]:.2f})"


# ---------- model ----------
def current_model() -> str:
    provider = SET["provider"]
    if provider == "ollama":
        return SET["ollama_model"]
    if provider == "openai":
        return SET["openai_model"]
    if provider == "gemini":
        return SET["gemini_model"]
    return SET["groq_model"]


def list_ollama_models():
    try:
        import ollama
        res = ollama.Client(host=SET["ollama_host"]).list()
        items = res["models"] if isinstance(res, dict) else res.models
        return [m["model"] if isinstance(m, dict) else m.model for m in items]
    except Exception:
        return []


def build_prompt(goal, history, w, h):
    recent = "\n".join(history[-10:]) or "(nothing yet)"
    return (
        f"Задача: {goal}\nРазмер экрана: {w}x{h}.\n\n"
        f"История действий:\n{recent}\n\nКакое следующее действие? Ответь одним JSON."
    )


def parse_action(text: str) -> dict:
    start = text.find("{")
    if start == -1:
        raise ValueError(f"model returned no JSON: {text}")
    action, _ = json.JSONDecoder().raw_decode(text[start:])
    return action


def call_with_retry(fn, tries: int = 3, wait: int = 10):
    """Repeats a request when the provider is overloaded (HTTP 503)."""
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            if "503" not in str(e) or i == tries:
                raise
            out(t("retry").format(wait=wait, i=i, tries=tries))
            time.sleep(wait)


def ask_model(goal, history, shot_b64, w, h):
    prompt = build_prompt(goal, history, w, h)

    if SET["provider"] == "ollama":
        import ollama
        client = ollama.Client(host=SET["ollama_host"])
        msg = {"role": "user", "content": prompt}
        if shot_b64:
            msg["images"] = [shot_b64]
        resp = client.chat(
            model=SET["ollama_model"],
            messages=[{"role": "system", "content": SYSTEM}, msg],
            options={"temperature": 0.1},
        )
        text = resp["message"]["content"]

    elif SET["provider"] == "groq":
        from groq import Groq
        client = Groq(api_key=SET["groq_key"])
        content = [{"type": "text", "text": prompt}]
        if shot_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"},
            })
        resp = client.chat.completions.create(
            model=SET["groq_model"],
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": content},
            ],
            temperature=0.1,
            max_tokens=600,
        )
        text = resp.choices[0].message.content or ""

    elif SET["provider"] == "openai":
        from openai import OpenAI

        if not SET["openai_key"]:
            raise RuntimeError(t("no_api_key"))

        client = OpenAI(api_key=SET["openai_key"])
        content = [{"type": "text", "text": prompt}]
        if shot_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"},
            })
        resp = client.chat.completions.create(
            model=SET["openai_model"],
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": content},
            ],
            max_completion_tokens=2000,
        )
        text = resp.choices[0].message.content or ""
        if not text.strip():
            raise ValueError("empty model response, try again")

    elif SET["provider"] == "gemini":
        from openai import OpenAI

        if not SET["gemini_key"]:
            raise RuntimeError(t("no_gemini_key"))

        client = OpenAI(api_key=SET["gemini_key"], base_url=GEMINI_BASE_URL)
        content = [{"type": "text", "text": prompt}]
        if shot_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"},
            })
        resp = call_with_retry(lambda: client.chat.completions.create(
            model=SET["gemini_model"],
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": content},
            ],
            temperature=0.1,
            max_tokens=600,
        ))
        text = resp.choices[0].message.content or ""
        if not text.strip():
            raise ValueError("empty model response, try again")

    else:
        raise ValueError(f"Unknown provider: {SET['provider']}")

    action = parse_action(text)
    if not isinstance(action, dict):
        raise ValueError("model answer is not a JSON object")
    kind = action.get("action", "")
    if isinstance(kind, str):
        action["action"] = ALIASES.get(kind, kind)
    return action


def check_screen(system_prompt: str, condition: str, shot_b64: str, extra: str = ""):
    """Asks the model a yes/no question about the screenshot.
    Returns (True, reason), (False, reason), or (None, reason) if the check itself failed
    or the model's answer is not a real boolean. None is never treated as success."""
    prompt = f"{extra}Условие: {condition}\nОтветь JSON."
    try:
        if SET["provider"] == "ollama":
            import ollama
            client = ollama.Client(host=SET["ollama_host"])
            resp = client.chat(
                model=SET["ollama_model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt, "images": [shot_b64]},
                ],
                options={"temperature": 0},
            )
            text = resp["message"]["content"]
        else:
            from openai import OpenAI
            if SET["provider"] == "gemini":
                client = OpenAI(api_key=SET["gemini_key"], base_url=GEMINI_BASE_URL)
                model = SET["gemini_model"]
            elif SET["provider"] == "openai":
                client = OpenAI(api_key=SET["openai_key"])
                model = SET["openai_model"]
            else:
                from groq import Groq
                client = Groq(api_key=SET["groq_key"])
                model = SET["groq_model"]
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"}},
            ]
            resp = call_with_retry(lambda: client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                temperature=0,
                max_tokens=200,
            ))
            text = resp.choices[0].message.content or ""

        result = parse_action(text)
        ok = result.get("ok")
        if type(ok) is not bool:
            return None, f"invalid boolean in model answer: {ok!r}"
        return ok, str(result.get("reason", ""))
    except Exception as e:
        return None, f"check failed: {e}"


def verify_expectation(expect: str, shot_b64: str):
    """Checks the screen after the action against the expected result."""
    if not expect or not shot_b64:
        return None, "no expectation"
    return check_screen(VERIFY_SYSTEM, f"Ожидание: {expect}", shot_b64)


def guard_precondition(precondition: str, shot_b64: str):
    """Checks the screen before the action against the required condition."""
    if not precondition or not shot_b64:
        return None, "no precondition"
    return check_screen(GUARD_SYSTEM, precondition, shot_b64)


def safety_check(goal: str, action: dict, shot_b64: str):
    """Asks whether the action is safe and matches the user's goal, before it runs."""
    if not shot_b64:
        return None, "no screenshot"
    desc = json.dumps(action, ensure_ascii=False)
    extra = f"Цель пользователя: {goal}\nПредлагаемое действие: {desc}\n"
    return check_screen(SAFETY_SYSTEM, "действие безопасно и соответствует цели", shot_b64, extra)


# ---------- actions ----------
def paste(text):
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")


def find_images(query):
    html = requests.get(
        "https://www.bing.com/images/search",
        params={"q": query},
        headers=HEADERS,
        timeout=15,
    ).text
    urls = list(dict.fromkeys(re.findall(r'"murl":"(https?://[^"]+?)"', html)))[:8]
    return "\n".join(urls) if urls else "nothing found"


def read_page(url):
    html = requests.get(url, headers=HEADERS, timeout=15).text
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", text)[:3000]


def download(url, filename):
    path = safe_desktop_path(filename)
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    path.write_bytes(r.content)
    return f"downloaded: {path}"


def execute(a):
    k = a["action"]

    if k == "open_app":
        pyautogui.press("win")
        time.sleep(0.8)
        paste(a["name"])
        time.sleep(1.5)
        pyautogui.press("enter")
        time.sleep(3)
        return f"opened app {a['name']}"

    if k == "open_url":
        webbrowser.open(a["url"])
        time.sleep(3)
        return "page opened"

    if k in ("click_text", "double_click_text"):
        found = find_text(a["text"])
        if not found:
            return f"'{a['text']}': {t('not_found')}"
        x, y, how = found
        pyautogui.moveTo(x, y, duration=0.3)
        if k == "click_text":
            pyautogui.click(x, y)
        else:
            pyautogui.doubleClick(x, y)
        return f"{t('clicked')} '{a['text']}' ({x}, {y}), {how}"

    if k == "click":
        x, y = int(a["x"]), int(a["y"])
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click(x, y)
        return f"clicked ({x}, {y})"

    if k == "type":
        if a["text"].isascii():
            pyautogui.write(a["text"], interval=0.02)
        else:
            paste(a["text"])
        return "text typed"

    if k == "hotkey":
        pyautogui.hotkey(*a["keys"])
        return "hotkey pressed"

    if k == "scroll":
        pyautogui.scroll(int(a["amount"]))
        return "scrolled"

    if k == "wait":
        time.sleep(float(a["seconds"]))
        return "waited"

    if k == "find_images":
        return find_images(a["query"])

    if k == "download":
        return download(a["url"], a["filename"])

    if k == "read_page":
        return read_page(a["url"])

    if k == "save_screenshot":
        path = safe_desktop_path(a["filename"])
        pyautogui.screenshot().save(path)
        return f"screenshot saved: {path}"

    if k == "write_file":
        path = safe_desktop_path(a["filename"])
        path.write_text(a["content"], encoding="utf-8")
        return f"file written: {path}"

    raise ValueError(f"no handler for action {k!r}")


def is_risky(a):
    return any(w in json.dumps(a, ensure_ascii=False).lower() for w in RISKY)


# ---------- quick commands without the model ----------
def quick_route(goal: str) -> bool:
    low = goal.lower().strip()

    if re.search(r"(песн|музык|трек)", low):
        query = re.sub(r"\(.*?\)", "", low)
        query = re.sub(
            r"(найди|включи|поставь|запусти|песню|песня|музыку|музыка|трек|"
            r"на ют|на ютубе|в инете|в интернете|и включи)",
            "",
            query,
        )
        query = re.sub(r"\s+", " ", query).strip()
        if query:
            webbrowser.open("https://music.youtube.com/search?q=" + urllib.parse.quote(query))
            out(t("song_open") + f" «{query}»")
        else:
            out(t("song_ask"))
        return True

    m = re.match(r"^(найди|поищи|загугли)\s+(.+)$", low)
    if m:
        webbrowser.open("https://duckduckgo.com/?q=" + urllib.parse.quote(m.group(2)))
        out(t("search_open"))
        return True

    return False


# ---------- agent loop ----------
def run(goal):
    history = []
    last_key = None
    clicks_in_row = 0
    # True only when the last non-check action was confirmed by a verification.
    # Every new action resets it, so done cannot reuse an old confirmation.
    confirmed = None

    for step in range(1, MAX_STEPS + 1):
        if STATE["stop"]:
            raise StopRun()

        if SET["use_screenshot"]:
            shot, w, h = grab_screen()
        else:
            shot, w, h = None, 0, 0

        try:
            a = ask_model(goal, history, shot, w, h)
        except Exception as e:
            out(f"{t('error')}: {e}")
            return

        print(f"[{step}] {a}", flush=True)

        # Strict allowlist check: anything not matching the schema is never executed
        valid, why = validate_action(a)
        if not valid:
            print(f"  ⛔ {t('invalid_action')}: {why}", flush=True)
            history.append(f"{json.dumps(a, ensure_ascii=False, default=str)} -> {t('invalid_action').upper()}: {why}")
            continue

        act = a["action"]
        key = json.dumps(a, ensure_ascii=False, sort_keys=True)
        if key == last_key:
            out(t("repeat"))
            return
        last_key = key

        if act in ("click", "click_text", "double_click_text"):
            clicks_in_row += 1
        else:
            clicks_in_row = 0
        if clicks_in_row >= 4:
            out(t("clicks_stop"))
            return

        if act == "done":
            if SET["verify"]:
                if confirmed is not True:
                    print(f"  ✗ {t('done_rejected')}", flush=True)
                    history.append(f"done отклонён: последнее действие не подтверждено проверкой. {t('done_rejected')}")
                    continue
            elif not SET["trust_done"]:
                print(f"  ✗ {t('done_rejected')} (trust_done выключен)", flush=True)
                history.append("done отклонён: проверка выключена, а trust_done не разрешён в конфиге.")
                continue
            out(t("done") + a.get("summary", ""))
            return

        if act == "ask_user":
            ans = ask(a["question"])
            history.append(f"asked «{a['question']}», answer: {ans}")
            confirmed = None  # new information from the user: earlier confirmation no longer counts
            continue

        # Any new executable action must be confirmed again before done
        confirmed = None

        if is_risky(a):
            ans = ask(f"{t('risky')} {json.dumps(a, ensure_ascii=False)}")
            if ans.strip().lower() not in ("да", "yes", "y"):
                history.append(f"{t('cancelled')}: {a}")
                continue

        # Screen-changing actions need a precondition, otherwise they are not performed
        if act in CAN_GUARD and not a.get("precondition"):
            print(f"  ⛔ {t('no_precondition')}", flush=True)
            history.append(f"{json.dumps(a, ensure_ascii=False)} -> {t('no_precondition').upper()}")
            continue

        # Safety check: mandatory when enabled. Without a screenshot it cannot run, so the action is blocked.
        needs_safety = act in CAN_GUARD or act in SIDE_EFFECT
        if SET["safety_guard"] and needs_safety:
            if not shot:
                print(f"  ⛔ {t('no_screenshot_for_safety')}", flush=True)
                history.append(f"{json.dumps(a, ensure_ascii=False)} -> {t('no_screenshot_for_safety').upper()}")
                continue
            ok, reason = safety_check(goal, a, shot)
            if ok is not True:
                print(f"  ⛔ {t('safety_block')}: {reason}", flush=True)
                history.append(
                    f"{json.dumps(a, ensure_ascii=False)} -> {t('safety_block').upper()}: {reason}. Выбери другое действие."
                )
                continue

        if SET["guard"] and SET["use_screenshot"] and act in CAN_GUARD:
            ok, reason = guard_precondition(a["precondition"], shot)
            if ok is not True:
                print(f"  ⛔ {t('guard_block')}: {reason}", flush=True)
                history.append(
                    f"{json.dumps(a, ensure_ascii=False)} -> {t('guard_block').upper()}: "
                    f"{reason}. Сначала выполни условие «{a['precondition']}»."
                )
                continue

        try:
            result = execute(a)
        except UnsafePath as e:
            result = f"error: {e}"
        except Exception as e:
            result = f"error: {e}"
        print(f"  → {result[:200]}", flush=True)

        expect = a.get("expect", "")
        if SET["verify"] and SET["use_screenshot"] and expect and act not in NO_CHECK:
            time.sleep(1.5)
            after_shot, _, _ = grab_screen()
            ok, reason = verify_expectation(expect, after_shot)
            confirmed = ok
            if ok is True:
                print(f"  ✓ {t('verify_ok')}: {reason}", flush=True)
            elif ok is False:
                print(f"  ✗ {t('verify_fail')}: {reason}", flush=True)
                result = f"{result}; ПРОВЕРКА НЕ ПРОШЛА (ожидалось: {expect}; {reason}). Попробуй другой путь."
            else:
                print(f"  ? {t('verify_none')}: {reason}", flush=True)
                result = f"{result}; ПРОВЕРКА НЕ ВЫПОЛНЕНА ({reason}). Не вызывай done, пока результат не подтверждён."

        history.append(f"{json.dumps(a, ensure_ascii=False)} -> {result[:500]}")
        time.sleep(1)

    out(t("step_limit"))


def worker(goal):
    STATE["busy"] = True
    STATE["stop"] = False
    try:
        out(f"{t('task')}: {goal}")
        if not quick_route(goal):
            run(goal)
    except StopRun:
        out(t("stopped"))
    except Exception as e:
        out(f"{t('error')}: {e}")
    finally:
        try:
            tg_send_screenshot(f"{t('task')}: {goal}")
        except Exception as e:
            out(f"{t('screenshot_failed')}{e}")
        STATE["busy"] = False
        STATE["question"] = None


def start_goal(goal) -> bool:
    if STATE["busy"]:
        print(t("busy"), flush=True)
        tg_send(t("busy"))
        return False
    threading.Thread(target=worker, args=(goal,), daemon=True).start()
    return True


def stop_agent():
    STATE["stop"] = True
    ANSWER_EVENT.set()
    print(t("stopping"), flush=True)
    tg_send(t("stopping"))


def set_model(provider=None, model=None):
    if provider in ("ollama", "groq", "openai", "gemini"):
        SET["provider"] = provider
    if model:
        if SET["provider"] == "ollama":
            SET["ollama_model"] = model
        elif SET["provider"] == "openai":
            SET["openai_model"] = model
        elif SET["provider"] == "gemini":
            SET["gemini_model"] = model
        else:
            SET["groq_model"] = model


def show_models():
    print("Ollama:", flush=True)
    for m in list_ollama_models() or [t("none_models")]:
        print("  ", m, flush=True)
    print("Groq:", flush=True)
    for m in GROQ_PRESETS:
        print("  ", m, flush=True)
    print("OpenAI:", flush=True)
    for m in OPENAI_PRESETS:
        print("  ", m, flush=True)
    print("Gemini:", flush=True)
    for m in GEMINI_PRESETS:
        print("  ", m, flush=True)
    out(f"{t('current')}: {SET['provider']} / {current_model()}")


def handle_command(line: str):
    """Single entry point for commands from the console and from Telegram."""
    low = line.lower().strip()
    if low in ("стоп", "stop"):
        stop_agent()
    elif low in ("помощь", "help", "?"):
        out(t("help"))
    elif low in ("модели", "models"):
        show_models()
    elif low.startswith("lang "):
        code = low.split(maxsplit=1)[1].strip()
        if code in ("en", "ru"):
            LANG["current"] = code
            out(t("lang_set"))
        else:
            out(t("lang_bad"))
    elif low.startswith("провайдер ") or low.startswith("provider "):
        p = low.split(maxsplit=1)[1].strip()
        if p in ("ollama", "groq", "openai", "gemini"):
            set_model(provider=p)
            out(f"{SET['provider']} / {current_model()}")
        else:
            out(t("provider_bad"))
    elif low.startswith("модель ") or low.startswith("model "):
        name = line.split(maxsplit=1)[1].strip()
        set_model(model=name)
        out(f"{t('model_set')}: {SET['provider']} / {current_model()}")
    else:
        start_goal(line)


def console_loop():
    print(t("help"), flush=True)
    print(f"{t('current')}: {SET['provider']} / {current_model()}", flush=True)
    print(f"{t('web')}: http://localhost:{WEB_PORT}", flush=True)
    while True:
        try:
            line = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            continue
        if STATE["question"]:
            ANSWER["text"] = line
            ANSWER_EVENT.set()
            continue
        if line.lower() in ("выход", "exit", "quit"):
            break
        handle_command(line)


# ---------- web panel (localhost only, no token login) ----------
app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shiro Control Center</title>
<style>
:root{color-scheme:dark;--bg:#090d18;--panel:#111a2b;--panel2:#172238;--line:#263650;--muted:#91a1bb;--text:#edf3ff;--accent:#8b7cff;--cyan:#4dd9d0;--red:#ff647c;--green:#50d6a0}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 15% 0%,#20204b 0,transparent 38%),var(--bg);color:var(--text);font:15px system-ui,Segoe UI,sans-serif}
.wrap{max-width:1180px;margin:auto;padding:30px 22px 60px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:26px}
.brand{display:flex;align-items:center;gap:13px}.logo{width:48px;height:48px;border-radius:15px;background:linear-gradient(135deg,var(--accent),var(--cyan));display:grid;place-items:center;font-size:25px;font-weight:900;color:#07111d;box-shadow:0 8px 30px #766bff55}
h1{font-size:25px;margin:0;letter-spacing:-.7px}.sub{color:var(--muted);font-size:13px;margin-top:3px}.pill{border:1px solid var(--line);background:#111a2b;padding:8px 13px;border-radius:99px;color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:18px}.card{background:linear-gradient(160deg,#151f33,#101827);border:1px solid var(--line);border-radius:19px;padding:20px;box-shadow:0 14px 35px #0002}
.card h2{font-size:16px;margin:0 0 16px}.wide{grid-column:1/-1}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select{width:100%;min-width:0;background:#0b1322;border:1px solid #30415e;border-radius:11px;padding:12px 13px;color:var(--text);font:inherit;outline:none}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px #8b7cff22}.grow{flex:1}.field{flex:1;min-width:180px}
button{border:0;border-radius:11px;padding:11px 16px;font:700 13px system-ui;cursor:pointer;color:#08111e;background:linear-gradient(135deg,var(--cyan),#8ce6d0);transition:.15s}
button:hover{filter:brightness(1.1);transform:translateY(-1px)}button.secondary{background:#263653;color:var(--text)}button.stop{background:#4a2435;color:#ff9eae}button.purple{background:linear-gradient(135deg,#9b8cff,#6b91ff)}
.status{margin-top:15px;border:1px solid var(--line);border-radius:13px;padding:14px;background:#0b1322;min-height:55px;color:#c8d5eb;white-space:pre-wrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:8px}.dot.busy{background:#ffc66d;box-shadow:0 0 12px #ffc66d88}
.question{display:none;margin-top:12px;padding:14px;border-radius:13px;background:#332b19;border:1px solid #66502b}.question p{margin:0 0 10px;color:#ffda9c}
.schedule-list{display:grid;gap:10px;margin-top:15px}.schedule{display:flex;justify-content:space-between;gap:12px;align-items:center;background:#0b1322;border:1px solid var(--line);padding:13px;border-radius:13px}.schedule strong{display:block;font-size:14px}.schedule small{display:block;color:var(--muted);margin-top:4px}.empty{color:var(--muted);padding:16px;text-align:center;border:1px dashed var(--line);border-radius:12px}
@media(max-width:760px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}.wrap{padding:20px 14px}.top{align-items:flex-start;flex-direction:column}.schedule{align-items:flex-start;flex-direction:column}}
</style></head><body><main class="wrap">
<header class="top"><div class="brand"><div class="logo">S</div><div><h1>Shiro Control Center</h1><div class="sub">Five Banana · Desktop Agent</div></div></div><div class="pill" id="clock">LOCAL TIME</div></header>
<div class="grid">
<section class="card"><h2>⚡ Управление агентом</h2>
<div class="row"><div class="field"><label class="sub">Провайдер</label><select id="provider"><option value="ollama">Ollama</option><option value="groq">Groq</option><option value="openai">OpenAI</option><option value="gemini">Gemini</option></select></div>
<div class="field"><label class="sub">Модель</label><input id="model" list="models" placeholder="Название модели"><datalist id="models"></datalist></div><button class="secondary" onclick="applySettings()">Применить</button></div>
<div class="row" style="margin-top:15px"><input id="goal" class="grow" placeholder="Например: открой блокнот"><button class="purple" onclick="runTask()">Запустить</button><button class="stop" onclick="stopAgent()">Стоп</button></div>
<div class="status" id="status"><span class="dot"></span>Подключение...</div>
<div class="question" id="question"><p id="qtext"></p><div class="row"><input id="answer" class="grow" placeholder="Ответ Shiro"><button onclick="sendAnswer()">Ответить</button></div></div>
</section>
<section class="card"><h2>🧠 Состояние</h2><div class="status" id="state">Загрузка...</div><p class="sub">Панель доступна только на этом компьютере через localhost.</p></section>
<section class="card wide"><h2>⏰ Планировщик</h2>
<div class="row"><div class="field"><label class="sub">Задача для Shiro</label><input id="scheduleGoal" placeholder="Например: открой браузер"></div><div class="field"><label class="sub">Дата и время запуска (локальное время ПК)</label><input id="scheduleTime" type="datetime-local"></div><button onclick="addSchedule()">Добавить</button></div>
<div class="schedule-list" id="schedules"><div class="empty">Пока нет запланированных задач</div></div>
</section></div></main>
<script>
const $=id=>document.getElementById(id);let models={ollama:[],groq:[],openai:[],gemini:[]};let lastProvider=null;
async function api(url,body){const opts=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};const r=await fetch(url,opts);let data={};try{data=await r.json()}catch{}if(!r.ok)throw Error(data.error||('HTTP '+r.status));return data}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function load(){try{const s=await api('/api/state');models=s.models||models;$('status').innerHTML='<span class="dot '+(s.busy?'busy':'')+'"></span>'+(s.busy?'Shiro выполняет задачу':'Shiro свободен')+(s.last?'<br>'+esc(s.last):'');$('state').textContent='Провайдер: '+s.provider+'\\nМодель: '+s.model+'\\nСостояние: '+(s.busy?'Работает':'Ожидает');if(document.activeElement!==$('provider'))$('provider').value=s.provider;if(lastProvider!==s.provider){lastProvider=s.provider;$('models').innerHTML=(models[s.provider]||[]).map(m=>'<option value="'+esc(m)+'">').join('')}if(document.activeElement!==$('model'))$('model').value=s.model;$('question').style.display=s.question?'block':'none';$('qtext').textContent=s.question||'';renderSchedules(s.schedules||[])}catch(e){$('status').textContent='Ошибка соединения: '+e.message}}
function renderSchedules(items){$('schedules').innerHTML=items.length?items.map(x=>`<div class="schedule"><div><strong>${esc(x.goal)}</strong><small>${esc(x.run_at.replace('T',' '))} · ${x.enabled?'Ожидает':'Завершено / отключено'}${x.last_result?' · '+esc(x.last_result):''}</small></div><button class="stop" onclick="deleteSchedule('${esc(x.id)}')">Удалить</button></div>`).join(''):'<div class="empty">Пока нет запланированных задач</div>'}
async function applySettings(){try{await api('/api/settings',{provider:$('provider').value,model:$('model').value.trim()});lastProvider=null;await load()}catch(e){alert(e.message)}}
async function runTask(){const goal=$('goal').value.trim();if(!goal)return;try{await api('/api/task',{goal});$('goal').value='';await load()}catch(e){alert(e.message)}}
async function stopAgent(){try{await api('/api/stop');await load()}catch(e){alert(e.message)}}
async function sendAnswer(){const text=$('answer').value.trim();if(!text)return;try{await api('/api/answer',{text});$('answer').value='';await load()}catch(e){alert(e.message)}}
async function addSchedule(){const goal=$('scheduleGoal').value.trim(),run_at=$('scheduleTime').value;if(!goal||!run_at)return alert('Укажи задачу и время запуска.');try{await api('/api/schedules',{goal,run_at});$('scheduleGoal').value='';$('scheduleTime').value='';await load()}catch(e){alert(e.message)}}
async function deleteSchedule(id){try{await fetch('/api/schedules/'+encodeURIComponent(id),{method:'DELETE'});await load()}catch(e){alert(e.message)}}
$('goal').addEventListener('keydown',e=>{if(e.key==='Enter')runTask()});$('answer').addEventListener('keydown',e=>{if(e.key==='Enter')sendAnswer()});$('provider').addEventListener('change',()=>{const p=$('provider').value;$('models').innerHTML=(models[p]||[]).map(m=>'<option value="'+esc(m)+'">').join('');$('model').value=(models[p]||[''])[0]||''});
function tick(){ $('clock').textContent=new Date().toLocaleString() }tick();setInterval(tick,1000);setInterval(load,1500);load();
</script></body></html>"""

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")

@app.route("/api/state")
def api_state():
    with SCHEDULE_LOCK:
        schedules = [dict(x) for x in SCHEDULES]
    return jsonify(
        busy=STATE["busy"], question=STATE["question"], last=STATE["last"],
        provider=SET["provider"], model=current_model(),
        models={"ollama": list_ollama_models(), "groq": GROQ_PRESETS,
                "openai": OPENAI_PRESETS, "gemini": GEMINI_PRESETS},
        schedules=schedules,
    )

@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(silent=True) or {}
    set_model(provider=data.get("provider"), model=data.get("model"))
    return jsonify(ok=True, provider=SET["provider"], model=current_model())

@app.route("/api/task", methods=["POST"])
def api_task():
    goal = (request.get_json(silent=True) or {}).get("goal", "").strip()
    if not goal:
        return jsonify(error="empty"), 400
    if not start_goal(goal):
        return jsonify(error="busy"), 409
    return jsonify(ok=True)

@app.route("/api/answer", methods=["POST"])
def api_answer():
    ANSWER["text"] = (request.get_json(silent=True) or {}).get("text", "")
    ANSWER_EVENT.set()
    return jsonify(ok=True)

@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_agent()
    return jsonify(ok=True)

@app.route("/api/schedules", methods=["GET", "POST"])
def api_schedules():
    if request.method == "GET":
        with SCHEDULE_LOCK:
            return jsonify(items=[dict(x) for x in SCHEDULES])
    data = request.get_json(silent=True) or {}
    goal = str(data.get("goal", "")).strip()
    run_at = str(data.get("run_at", "")).strip()
    if not goal or len(goal) > MAX_TEXT:
        return jsonify(error="invalid goal"), 400
    try:
        when = datetime.fromisoformat(run_at)
    except Exception:
        return jsonify(error="invalid date/time"), 400
    if when <= datetime.now():
        return jsonify(error="time must be in the future"), 400
    item = {"id": uuid.uuid4().hex[:12], "goal": goal,
            "run_at": when.isoformat(timespec="minutes"), "enabled": True, "last_result": ""}
    with SCHEDULE_LOCK:
        SCHEDULES.append(item)
        save_schedules()
    return jsonify(ok=True, item=item), 201

@app.route("/api/schedules/<task_id>", methods=["DELETE"])
def api_delete_schedule(task_id):
    with SCHEDULE_LOCK:
        before = len(SCHEDULES)
        SCHEDULES[:] = [x for x in SCHEDULES if x.get("id") != task_id]
        if len(SCHEDULES) == before:
            return jsonify(error="not found"), 404
        save_schedules()
    return jsonify(ok=True)

def run_web():
    # No token login: bind to loopback so the unauthenticated control panel is not exposed to the LAN.
    app.run(host="127.0.0.1", port=WEB_PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_scheduler()
    threading.Thread(target=run_web, daemon=True).start()
    start_telegram()
    console_loop()