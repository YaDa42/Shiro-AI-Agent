import ctypes
from ctypes import wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixel coordinates at 125%/150% scaling
except Exception:
    pass

import asyncio, base64, io, json, logging, os, re, threading, time, urllib.parse, webbrowser, difflib, uuid
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pyautogui
import pyperclip
import requests
import uiautomation as auto
import yaml
from flask import Flask, jsonify, request, Response
from PIL import ImageDraw
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _screen_to_absolute(x: int, y: int):
    """Переводит пиксели экрана в диапазон 0..65535, который требует SendInput."""
    vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    ax = int((x - vx) * 65535 / max(vw - 1, 1))
    ay = int((y - vy) * 65535 / max(vh - 1, 1))
    return ax, ay


def raw_click(x: int, y: int, double: bool = False):
    """Клик через SendInput с абсолютными координатами.
    Работает там, где pyautogui не работает: игры с захватом мыши, полноэкранные окна, VM."""
    ax, ay = _screen_to_absolute(x, y)
    flags_move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    def send(flags):
        inp = INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(ax, ay, 0, flags, 0, 0)))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    send(flags_move)
    time.sleep(0.05)
    for _ in range(2 if double else 1):
        send(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.03)
        send(MOUSEEVENTF_LEFTUP)
        time.sleep(0.05)


BASE = Path(__file__).parent
cfg = yaml.safe_load(open(BASE / "config.yaml", encoding="utf-8"))
LLM = cfg["llm"]
AG = cfg.get("agent", {})
TG = cfg.get("telegram", {})
MAX_STEPS = AG.get("max_steps", 25)
# This release has no web login, so keep the control panel local-only.
_CONFIGURED_WEB_HOST = str(AG.get("web_host", "127.0.0.1")).strip().lower()
WEB_HOST = _CONFIGURED_WEB_HOST if _CONFIGURED_WEB_HOST in ("127.0.0.1", "localhost") else "127.0.0.1"
WEB_PORT = AG.get("web_port", 8000)
DESKTOP = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
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
        "step_limit": "Step limit reached.",
        "repeat": "The same action repeats, stopping.",
        "clicks_stop": "Too many clicks in a row, stopping.",
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
        "screenshot_failed": "screenshot not sent: ",
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
        "step_limit": "Лимит шагов исчерпан.",
        "repeat": "Одно и то же действие повторяется, останавливаюсь.",
        "clicks_stop": "Слишком много кликов подряд, останавливаюсь.",
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
        "screenshot_failed": "скриншот не отправлен: ",
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
    "gemini-3.1-flash-lite",
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
}

SYSTEM = """Ты Shiro Ai Agent и ты управляешь Windows-компьютером. Выполняй задачу сам, шаг за шагом.
Ответ — ОДИН JSON-объект, без пояснений и без текста после него. Допустимые действия:

{"action": "open_app", "name": "paint"}              открыть приложение через меню Пуск
{"action": "open_url", "url": "https://..."}         открыть страницу в браузере
{"action": "click_text", "text": "Сохранить"}        клик по тексту на экране (координаты находит программа)
{"action": "double_click_text", "text": "Файл"}      двойной клик по тексту
{"action": "click", "x": 100, "y": 200}              клик по координатам с сетки (только если click_text ответил «не найден»)
{"action": "type", "text": "..."}                    напечатать текст в активное окно
{"action": "hotkey", "keys": ["enter"]}              нажать клавишу или сочетание
{"action": "scroll", "amount": -500}                 отрицательное = вниз
{"action": "wait", "seconds": 2}
{"action": "find_images", "query": "..."}
{"action": "download", "url": "...", "filename": "..."}
{"action": "read_page", "url": "..."}
{"action": "save_screenshot", "filename": "shot.png"}
{"action": "write_file", "filename": "notes.txt", "content": "..."}
{"action": "ask_user", "question": "..."}
{"action": "done", "summary": "что сделано"}

Правила:
0. Если задача говорит, что что-то уже настроено или установлено, не открывай настройки и не ищи установку.
   Сразу найди нужную кнопку на экране (например «Запустить») и нажми её через click_text.
1. Если на экране есть подпись нужного элемента, ВСЕГДА используй click_text (или double_click_text) с точным текстом с экрана.
   Координаты в этом случае не указывай.
2. click по координатам разрешён ТОЛЬКО после того, как click_text ответил «не найден».
3. Не спрашивай, если задача понятна.
4. Сначала открой нужное приложение или сайт (open_app / open_url), потом действуй в нём.
5. Печатай в поле только текст, который пользователь попросил написать.
6. Чтобы напечатать в поле ввода: если поле уже активно, сразу type. Иначе нажми на него через click_text, затем type.
7. Чтобы отправить сообщение: после ввода текста вызови hotkey ["enter"].
8. Если страница не изменилась после клика, не повторяй тот же клик больше двух раз.
9. ask_user только если без ответа невозможно.
10. Вызывай done только тогда, когда на экране виден результат задачи.
"""

ALIASES = {
    "terminate": "done",
    "finish": "done",
    "complete": "done",
    "open": "open_app",
    "click_on_text": "click_text",
}
RISKY = ("удал", "delete", "отправ", "оплат", "перезапис", "format", "install", "установ")

STATE = {"busy": False, "stop": False, "question": None, "last": ""}
ANSWER = {"text": ""}
ANSWER_EVENT = threading.Event()
TG_APP = {"app": None, "loop": None, "chat_id": None}

# Scheduled tasks are stored next to agent.py and use the computer's local time.
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
        print(f"Could not load scheduled tasks: {e}", flush=True)
        SCHEDULES = []


def save_schedules():
    with SCHEDULE_LOCK:
        temp = SCHEDULE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(SCHEDULES, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(SCHEDULE_FILE)


def schedule_task(goal: str, run_at: str):
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("Task description is empty.")
    when = datetime.fromisoformat(run_at)
    if when <= datetime.now():
        raise ValueError("Choose a time in the future.")
    item = {
        "id": uuid.uuid4().hex[:12],
        "goal": goal[:1000],
        "run_at": when.isoformat(timespec="minutes"),
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with SCHEDULE_LOCK:
        SCHEDULES.append(item)
        SCHEDULES.sort(key=lambda x: x.get("run_at", ""))
        save_schedules()
    return item


def delete_schedule(task_id: str) -> bool:
    with SCHEDULE_LOCK:
        before = len(SCHEDULES)
        SCHEDULES[:] = [x for x in SCHEDULES if x.get("id") != task_id]
        changed = len(SCHEDULES) != before
        if changed:
            save_schedules()
        return changed


def scheduler_loop():
    while True:
        due = None
        now = datetime.now()
        with SCHEDULE_LOCK:
            for item in SCHEDULES:
                if item.get("status") == "pending":
                    try:
                        if datetime.fromisoformat(item["run_at"]) <= now:
                            due = item
                            break
                    except Exception:
                        item["status"] = "invalid"
                        save_schedules()
        if due:
            if STATE["busy"]:
                # Do not lose a scheduled task when another task is running.
                with SCHEDULE_LOCK:
                    due["run_at"] = (datetime.now().replace(second=0, microsecond=0)
                                     + timedelta(minutes=1)).isoformat(timespec="minutes")
                    save_schedules()
            elif start_goal(due["goal"]):
                with SCHEDULE_LOCK:
                    due["status"] = "started"
                    due["started_at"] = datetime.now().isoformat(timespec="seconds")
                    save_schedules()
        time.sleep(1)


def start_scheduler():
    load_schedules()
    threading.Thread(target=scheduler_loop, daemon=True, name="ShiroScheduler").start()


class StopRun(Exception):
    pass


class TextNotFound(Exception):
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


# ---------- finding text on screen (UIA first, then OCR) ----------
def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", (s or "").lower())


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
    return difflib.SequenceMatcher(None, tn, nn).ratio()


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
    """Ищет элемент с нужной подписью через UI Automation. Работает в обычных окнах."""
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


def find_text_screen(text: str):
    """UIA сначала (точные координаты), если нет уверенного совпадения, OCR по скриншоту."""
    uia = _uia_candidate(text)
    if uia and uia[0] >= 0.95:
        return uia[1], uia[2], f"{uia[3]} ({uia[0]:.2f})"

    cands = []
    if uia:
        cands.append(uia)
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
        resp = client.chat.completions.create(
            model=SET["gemini_model"],
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": content},
            ],
            temperature=0.1,
            max_tokens=600,
        )
        text = resp.choices[0].message.content or ""
        if not text.strip():
            raise ValueError("empty model response, try again")

    else:
        raise ValueError(f"Unknown provider: {SET['provider']}")

    action = parse_action(text)
    kind = action.get("action", "")
    action["action"] = ALIASES.get(kind, kind)
    return action


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
    DESKTOP.mkdir(exist_ok=True)
    path = DESKTOP / filename
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
        found = find_text_screen(a["text"])
        if not found:
            raise TextNotFound(a["text"])
        x, y, how = found
        raw_click(x, y, double=(k == "double_click_text"))
        return f"{t('clicked')} '{a['text']}' ({x}, {y}), {how}"

    if k == "click":
        x, y = int(a["x"]), int(a["y"])
        raw_click(x, y)
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
        time.sleep(float(a.get("seconds", 1)))
        return "waited"

    if k == "find_images":
        return find_images(a["query"])

    if k == "download":
        return download(a["url"], a["filename"])

    if k == "read_page":
        return read_page(a["url"])

    if k == "save_screenshot":
        DESKTOP.mkdir(exist_ok=True)
        path = DESKTOP / a["filename"]
        pyautogui.screenshot().save(path)
        return f"screenshot saved: {path}"

    if k == "write_file":
        DESKTOP.mkdir(exist_ok=True)
        path = DESKTOP / a["filename"]
        path.write_text(a["content"], encoding="utf-8")
        return f"file written: {path}"

    return f"unknown action '{k}', skipping"


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
    # Координаты разрешены только после того, как click_text не нашёл текст.
    text_tried = False    # был хоть один click_text / double_click_text в этом запуске
    text_missing = False  # последний click_text / double_click_text не нашёл текст
    CLICK_ACTIONS = ("click", "click_text", "double_click_text")

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
        kind = a.get("action", "")

        # Жёсткая проверка: координаты без предварительного «не найден» не выполняются
        if kind == "click" and not (text_tried and text_missing):
            msg = "click по координатам запрещён: сначала click_text по подписи, координаты только если текст не найден"
            print(f"  ⛔ {msg}", flush=True)
            history.append(f"{json.dumps(a, ensure_ascii=False)} -> ЗАБЛОКИРОВАНО: {msg}")
            time.sleep(0.5)
            continue

        key = json.dumps(a, ensure_ascii=False, sort_keys=True)
        if key == last_key:
            out(t("repeat"))
            return
        last_key = key

        if kind in CLICK_ACTIONS:
            clicks_in_row += 1
        else:
            clicks_in_row = 0
        if clicks_in_row >= 4:
            out(t("clicks_stop"))
            return

        if kind == "done":
            out(t("done") + a.get("summary", ""))
            return

        if kind == "ask_user":
            ans = ask(a.get("question", "?"))
            history.append(f"asked «{a.get('question')}», answer: {ans}")
            continue

        if is_risky(a):
            ans = ask(f"{t('risky')} {json.dumps(a, ensure_ascii=False)}")
            if ans.strip().lower() not in ("да", "yes", "y"):
                history.append(f"{t('cancelled')}: {a}")
                continue

        try:
            result = execute(a)
            if kind in ("click_text", "double_click_text"):
                text_tried = True
                text_missing = False
        except TextNotFound:
            text_tried = True
            text_missing = True
            result = f"'{a['text']}': {t('not_found')}. Используй click по координатам."
        except Exception as e:
            result = f"error: {e}"

        print(f"  → {result[:200]}", flush=True)
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


# ---------- web panel ----------
app = Flask(__name__)

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shiro Control Center</title>
<style>
:root{color-scheme:dark;--bg:#0b0d12;--panel:#131722;--line:#283247;--text:#edf2fb;--muted:#91a0b8;--green:#a3e635;--cyan:#67e8f9;--red:#fb7185}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 15% 0%,#1a2740 0,transparent 42%),var(--bg);color:var(--text);font:15px/1.5 Inter,Segoe UI,system-ui,sans-serif}
.wrap{max-width:1180px;margin:auto;padding:30px 22px 60px}.top{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:14px}.logo{width:48px;height:48px;border-radius:15px;background:linear-gradient(135deg,#bef264,#22d3ee);display:grid;place-items:center;color:#10151b;font-weight:900;font-size:24px}
h1{font-size:25px;margin:0;letter-spacing:-.7px}.sub,.small{color:var(--muted);font-size:12px}.pill{border:1px solid var(--line);border-radius:999px;padding:7px 12px;color:var(--muted);font-size:12px}
.layout{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(280px,.85fr);gap:18px}.col{display:grid;gap:18px;align-content:start}
.card{background:linear-gradient(145deg,#171d29,#111620);border:1px solid var(--line);border-radius:19px;padding:21px;box-shadow:0 14px 35px #0003}
.cardhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.card h2{font-size:16px;margin:0}.eyebrow{font-size:11px;color:var(--green);font-weight:800;letter-spacing:1.3px;text-transform:uppercase}
.field{display:grid;gap:7px;margin:12px 0}.field label{font-size:12px;color:var(--muted);font-weight:650}
input,select{width:100%;background:#0c111a;color:var(--text);border:1px solid #30394a;border-radius:11px;padding:12px 13px;font:inherit;outline:none;min-width:0}
input:focus,select:focus{border-color:var(--cyan);box-shadow:0 0 0 3px #67e8f91a}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:15px}
button{border:0;border-radius:11px;padding:11px 16px;font:700 13px system-ui,sans-serif;cursor:pointer;transition:transform .15s,filter .15s}
button:hover{filter:brightness(1.1);transform:translateY(-1px)}button:disabled{opacity:.45;cursor:not-allowed;transform:none}
.primary{background:var(--green);color:#17200b}.secondary{background:#263247;color:var(--text);border:1px solid #35435a}.danger{background:#4a202b;color:#fecdd3;border:1px solid #713344}.cyan{background:#123b48;color:#a5f3fc;border:1px solid #1d5967}
.status{background:#0c111a;border:1px solid var(--line);border-radius:13px;padding:14px;min-height:74px;white-space:pre-wrap;overflow-wrap:anywhere}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:8px}.dot.busy{background:#fbbf24;box-shadow:0 0 12px #fbbf2466}
.schedule-list{display:grid;gap:9px}.schedule{display:flex;justify-content:space-between;align-items:center;gap:12px;background:#0d131d;border:1px solid var(--line);border-radius:12px;padding:12px}
.schedule-main{min-width:0}.schedule-goal{font-weight:650;overflow-wrap:anywhere}.schedule-time{font-size:12px;color:var(--cyan);margin-top:3px}.tag{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--green)}
.answerbox{display:none;margin-top:14px;padding:14px;border:1px solid #795b25;background:#2a2111;border-radius:13px}.answerbox p{margin:0 0 10px;color:#fcd78b}
footer{margin-top:20px;color:#667187;font-size:11px;text-align:center}
@media(max-width:800px){.layout{grid-template-columns:1fr}.wrap{padding:20px 14px}.top{align-items:flex-start}.pill{display:none}}
@media(max-width:480px){.grid2{grid-template-columns:1fr}.card{padding:16px}h1{font-size:21px}}
</style></head><body><main class="wrap">
<header class="top"><div class="brand"><div class="logo">S</div><div><h1>Shiro Control Center</h1><div class="sub">Desktop agent · Control panel</div></div></div><div class="pill">ICE CREAM · v6.0.0</div></header>
<div class="layout"><section class="col">
<div class="card"><div class="cardhead"><div><div class="eyebrow">01 / Control</div><h2>Run a task</h2></div><span class="small" id="modelNow">Loading model…</span></div>
<div class="field"><label for="goal">WHAT SHOULD SHIRO DO?</label><input id="goal" placeholder="For example: open Notepad" autocomplete="off"></div>
<div class="actions"><button class="primary" id="runBtn" onclick="runTask()">▶ Run task</button><button class="danger" onclick="stopAgent()">■ Stop agent</button></div>
<div class="field"><label>AGENT STATUS</label><div class="status"><span id="dot" class="dot"></span><span id="status">Connecting…</span></div></div>
<div class="answerbox" id="answerBox"><p id="questionText"></p><div class="grid2"><input id="answer" placeholder="Type your answer"><button class="cyan" onclick="sendAnswer()">Send answer</button></div></div></div>
<div class="card"><div class="cardhead"><div><div class="eyebrow">02 / Scheduler</div><h2>Schedule a task</h2></div><span class="small">Local computer time</span></div>
<div class="field"><label for="scheduledGoal">TASK</label><input id="scheduledGoal" placeholder="For example: open the browser"></div>
<div class="field"><label for="runAt">DATE AND TIME</label><input id="runAt" type="datetime-local"></div>
<div class="actions"><button class="primary" onclick="addSchedule()">＋ Schedule task</button></div><div class="small" id="scheduleMessage" style="margin-top:10px"></div></div>
<div class="card"><div class="cardhead"><div><div class="eyebrow">03 / Queue</div><h2>Scheduled tasks</h2></div><span class="small" id="scheduleCount">0 tasks</span></div><div class="schedule-list" id="scheduleList"><div class="small">No scheduled tasks yet.</div></div></div>
</section><aside class="col">
<div class="card"><div class="cardhead"><div><div class="eyebrow">04 / Configuration</div><h2>Model settings</h2></div></div>
<div class="field"><label for="provider">PROVIDER</label><select id="provider"><option value="ollama">Ollama · Local</option><option value="groq">Groq</option><option value="openai">OpenAI</option><option value="gemini">Gemini</option></select></div>
<div class="field"><label for="model">MODEL</label><input id="model" list="models" placeholder="Model name"><datalist id="models"></datalist></div>
<button class="secondary" onclick="applySettings()">Apply settings</button><div class="small" id="settingsMessage" style="margin-top:10px"></div></div>
<div class="card"><div class="eyebrow">Quick note</div><h2 style="margin:5px 0 8px">Shiro is ready.</h2><p class="small">Scheduled tasks start automatically while Shiro is running. If another task is active at the scheduled time, the scheduled task waits and retries shortly after.</p><p class="small">This build has no web-panel token login. Keep the panel on localhost and do not expose it to an untrusted network.</p></div>
</aside></div><footer>SHIRO AI AGENT · FIVE BANANA</footer></main>
<script>
const $=id=>document.getElementById(id);let models={ollama:[],groq:[],openai:[],gemini:[]};let lastProvider=null;
async function api(url,opts={}){const r=await fetch(url,opts);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data}
function setModelOptions(list){const dl=$('models');dl.replaceChildren();for(const name of list||[]){const o=document.createElement('option');o.value=name;dl.appendChild(o)}}
async function load(){try{const s=await api('/api/state');models=s.models||models;$('status').textContent=(s.busy?'Working. ':'Idle. ')+(s.last||'');$('dot').className='dot'+(s.busy?' busy':'');$('modelNow').textContent=s.provider+' / '+s.model;if(!$('provider').matches(':focus'))$('provider').value=s.provider;if(lastProvider!==s.provider){lastProvider=s.provider;setModelOptions(models[s.provider])}if(document.activeElement!==$('model'))$('model').value=s.model;$('answerBox').style.display=s.question?'block':'none';$('questionText').textContent=s.question||'';renderSchedules(s.schedules||[]);$('runBtn').disabled=!!s.busy}catch(e){$('status').textContent='Connection error: '+e.message}}
function renderSchedules(items){$('scheduleCount').textContent=items.length+' task(s)';const list=$('scheduleList');list.replaceChildren();if(!items.length){const e=document.createElement('div');e.className='small';e.textContent='No scheduled tasks yet.';list.appendChild(e);return}for(const item of items){const row=document.createElement('div');row.className='schedule';const main=document.createElement('div');main.className='schedule-main';const goal=document.createElement('div');goal.className='schedule-goal';goal.textContent=item.goal;const time=document.createElement('div');time.className='schedule-time';time.textContent=new Date(item.run_at).toLocaleString();const tag=document.createElement('div');tag.className='tag';tag.textContent=item.status;main.append(goal,time,tag);row.appendChild(main);if(item.status==='pending'){const b=document.createElement('button');b.className='danger';b.textContent='Delete';b.onclick=()=>deleteSchedule(item.id);row.appendChild(b)}list.appendChild(row)}}
async function applySettings(){try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:$('provider').value,model:$('model').value.trim()})});$('settingsMessage').textContent='Settings applied.';lastProvider=null;load()}catch(e){$('settingsMessage').textContent=e.message}}
async function runTask(){const goal=$('goal').value.trim();if(!goal)return;try{await api('/api/task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal})});$('goal').value='';load()}catch(e){alert(e.message)}}
async function stopAgent(){try{await api('/api/stop',{method:'POST'});load()}catch(e){alert(e.message)}}
async function sendAnswer(){const text=$('answer').value.trim();if(!text)return;try{await api('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});$('answer').value='';load()}catch(e){alert(e.message)}}
async function addSchedule(){const goal=$('scheduledGoal').value.trim(),run_at=$('runAt').value;if(!goal||!run_at){$('scheduleMessage').textContent='Enter a task and date/time.';return}try{await api('/api/schedules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal,run_at})});$('scheduledGoal').value='';$('scheduleMessage').textContent='Task scheduled.';load()}catch(e){$('scheduleMessage').textContent=e.message}}
async function deleteSchedule(id){try{await api('/api/schedules/'+encodeURIComponent(id),{method:'DELETE'});load()}catch(e){alert(e.message)}}
$('goal').addEventListener('keydown',e=>{if(e.key==='Enter')runTask()});$('answer').addEventListener('keydown',e=>{if(e.key==='Enter')sendAnswer()});$('provider').addEventListener('change',()=>{const p=$('provider').value;setModelOptions(models[p]||[]);$('model').value=(models[p]||[])[0]||''});
const now=new Date();now.setMinutes(now.getMinutes()+5);now.setSeconds(0,0);$('runAt').value=new Date(now-now.getTimezoneOffset()*60000).toISOString().slice(0,16);
setInterval(load,1500);load();
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
        return jsonify(error="Task is empty."), 400
    if not start_goal(goal):
        return jsonify(error="Agent is busy."), 409
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
            return jsonify(schedules=[dict(x) for x in SCHEDULES])
    data = request.get_json(silent=True) or {}
    try:
        item = schedule_task(data.get("goal", ""), data.get("run_at", ""))
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, schedule=item), 201


@app.route("/api/schedules/<task_id>", methods=["DELETE"])
def api_delete_schedule(task_id):
    if not delete_schedule(task_id):
        return jsonify(error="Scheduled task not found."), 404
    return jsonify(ok=True)


def run_web():
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    if _CONFIGURED_WEB_HOST not in ("127.0.0.1", "localhost"):
        print("Web panel is local-only in this build; configured web_host was ignored.", flush=True)
    start_scheduler()
    threading.Thread(target=run_web, daemon=True).start()
    start_telegram()
    console_loop()