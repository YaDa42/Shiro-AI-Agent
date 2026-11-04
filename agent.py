import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixel coordinates at 125%/150% scaling
except Exception:
    pass

import asyncio, base64, io, json, logging, os, re, threading, time, urllib.parse, webbrowser, difflib
from pathlib import Path

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

BASE = Path(__file__).parent
cfg = yaml.safe_load(open(BASE / "config.yaml", encoding="utf-8"))
LLM = cfg["llm"]
AG = cfg.get("agent", {})
TG = cfg.get("telegram", {})
MAX_STEPS = AG.get("max_steps", 25)
WEB_HOST = AG.get("web_host", "127.0.0.1")
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
  provider ollama|groq|openai
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
        "ask_hint": "(answer in the console, the web panel or Telegram)",
        "web": "Web panel",
        "current": "Current",
        "lang_set": "Language: English",
        "lang_bad": "Use: lang en  or  lang ru",
        "provider_bad": "Use: provider ollama, groq or openai",
        "model_set": "Model",
        "task": "Task",
        "not_found": "text not found on screen",
        "clicked": "clicked",
        "no_api_key": "OpenAI API key is not set. Set the OPENAI_API_KEY environment variable.",
        "song_ask": "Which song should I play?",
        "song_open": "Opened YouTube Music. Pick the track manually.",
        "search_open": "Search opened.",
        "risky": "Risky action. Allow it? (yes/no)",
        "cancelled": "user cancelled",
        "error": "Error",
        "none_models": "(none, or Ollama is not running)",
        "clicks_stop": "Four clicks in a row did nothing. Stopping. Check whether the input field is visible, or try a task that uses type without clicks.",
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
  провайдер ollama|groq|openai
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
        "ask_hint": "(ответь в консоли, в веб-панели или в Telegram)",
        "web": "Веб-панель",
        "current": "Сейчас",
        "lang_set": "Язык: русский",
        "lang_bad": "Пиши: lang en  или  lang ru",
        "provider_bad": "Пиши: провайдер ollama, groq или openai",
        "model_set": "Модель",
        "task": "Задача",
        "not_found": "текст на экране не найден",
        "clicked": "клик по",
        "no_api_key": "Не задан API-ключ OpenAI. Установи переменную OPENAI_API_KEY.",
        "song_ask": "Какую песню включить?",
        "song_open": "Открыл YouTube Music. Выбери трек вручную.",
        "search_open": "Открыл поиск.",
        "risky": "Опасное действие. Выполнить? (да/нет)",
        "cancelled": "пользователь отменил",
        "error": "Ошибка",
        "none_models": "(нет или Ollama не запущена)",
        "clicks_stop": "Четыре клика подряд ничего не дали. Останавливаюсь. Проверь, видно ли поле ввода, или попробуй задачу с type без кликов.",
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
    "gpt-6-luna",
    "gpt-6-astra",
]

SET = {
    "provider": LLM.get("provider", "ollama"),
    "ollama_model": LLM.get("model", "qwen2.5vl:3b"),
    "ollama_host": LLM.get("host", "http://localhost:11434"),
    "groq_model": LLM.get("groq_model") or GROQ_PRESETS[0],
    "groq_key": LLM.get("groq_api_key", ""),
    "openai_model": LLM.get("openai_model", "gpt-4.1-mini"),
    "openai_key": os.environ.get("OPENAI_API_KEY", LLM.get("openai_api_key", "")),
    "use_screenshot": LLM.get("use_screenshot", True),
}

SYSTEM = """Ты управляешь Windows-компьютером. Выполняй задачу сам, шаг за шагом.
Ответ — ОДИН JSON-объект, без пояснений и без текста после него. Допустимые действия:

{"action": "open_app", "name": "paint"}              открыть приложение через меню Пуск
{"action": "open_url", "url": "https://..."}         открыть страницу в браузере
{"action": "click_text", "text": "Сохранить"}        клик по тексту/кнопке на экране
{"action": "double_click_text", "text": "Файл"}      двойной клик по тексту
{"action": "click", "x": 100, "y": 200}              клик по координатам с сетки (для полей и иконок без подписи)
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


class StopRun(Exception):
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

        key = json.dumps(a, ensure_ascii=False, sort_keys=True)
        if key == last_key:
            out(t("repeat"))
            return
        last_key = key

        if a.get("action") in CLICK_ACTIONS:
            clicks_in_row += 1
        else:
            clicks_in_row = 0
        if clicks_in_row >= 4:
            out(t("clicks_stop"))
            return

        if a.get("action") == "done":
            out(t("done") + a.get("summary", ""))
            return

        if a.get("action") == "ask_user":
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
    if provider in ("ollama", "groq", "openai"):
        SET["provider"] = provider
    if model:
        if SET["provider"] == "ollama":
            SET["ollama_model"] = model
        elif SET["provider"] == "openai":
            SET["openai_model"] = model
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
        if p in ("ollama", "groq", "openai"):
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

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Agent</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:720px;margin:28px auto;padding:0 16px;background:#111;color:#eee}
 h2{margin-bottom:4px} .muted{color:#888;font-size:14px}
 .row{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}
 input,select{padding:10px;font-size:15px;background:#222;color:#eee;border:1px solid #444;border-radius:6px}
 input.grow{flex:1;min-width:200px}
 button{padding:10px 16px;font-size:15px;border-radius:6px;border:0;cursor:pointer;color:#fff}
 .go{background:#2e7d32} .stop{background:#c62828} .apply{background:#1565c0}
 #status{margin:12px 0;padding:10px;background:#1b1b1b;border-radius:6px;min-height:20px}
 #question{display:none;margin:12px 0;padding:12px;background:#3a2a00;border-radius:6px;color:#ffcc80}
</style></head><body>
<h2>Agent control</h2>
<div class="muted" id="currentModel"></div>

<div class="row">
  <select id="provider">
    <option value="ollama">Ollama (local)</option>
    <option value="groq">Groq (cloud)</option>
    <option value="openai">OpenAI (cloud)</option>
  </select>
  <input id="model" class="grow" list="models" placeholder="Model">
  <datalist id="models"></datalist>
  <button class="apply" onclick="applySettings()">Apply</button>
</div>

<div class="row">
  <input id="goal" class="grow" placeholder="e.g. open word" autofocus>
  <button class="go" onclick="runTask()">Run</button>
  <button class="stop" onclick="stopAgent()">Stop</button>
</div>

<div id="status"></div>
<div id="question">
  <div id="qtext"></div>
  <div class="row"><input id="answer" class="grow" placeholder="Answer the agent">
  <button class="go" onclick="sendAnswer()">Reply</button></div>
</div>

<script>
const $ = id => document.getElementById(id);
let models = {ollama: [], groq: [], openai: []};
let lastProvider = null;

async function load() {
  const s = await (await fetch('/api/state')).json();
  models = s.models;
  $('status').textContent = (s.busy ? 'Working. ' : 'Idle. ') + (s.last || '');
  $('currentModel').textContent = 'Current: ' + s.provider + ' / ' + s.model;
  if (!$('provider').matches(':focus')) $('provider').value = s.provider;
  if (lastProvider !== s.provider) {
    lastProvider = s.provider;
    $('models').innerHTML = (models[s.provider] || []).map(m => `<option value="${m}">`).join('');
  }
  if (document.activeElement !== $('model')) $('model').value = s.model;
  $('question').style.display = s.question ? 'block' : 'none';
  $('qtext').textContent = s.question ? 'Agent asks: ' + s.question : '';
}

async function applySettings() {
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({provider: $('provider').value, model: $('model').value.trim()})
  });
  lastProvider = null;
  load();
}

async function runTask() {
  const goal = $('goal').value.trim();
  if (!goal) return;
  const r = await fetch('/api/task', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({goal})
  });
  if (r.status === 409) alert('Agent is busy. Press Stop or wait.');
  $('goal').value = '';
  load();
}

async function stopAgent() {
  await fetch('/api/stop', {method: 'POST'});
  load();
}

async function sendAnswer() {
  const text = $('answer').value;
  $('answer').value = '';
  await fetch('/api/answer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text})
  });
  load();
}

$('goal').addEventListener('keydown', e => { if (e.key === 'Enter') runTask(); });
$('answer').addEventListener('keydown', e => { if (e.key === 'Enter') sendAnswer(); });
$('provider').addEventListener('change', () => {
  const provider = $('provider').value;
  $('models').innerHTML = (models[provider] || []).map(m => `<option value="${m}">`).join('');
  $('model').value = (models[provider] || [''])[0] || '';
});

setInterval(load, 1000);
load();
</script></body></html>"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/api/state")
def api_state():
    return jsonify(
        busy=STATE["busy"],
        question=STATE["question"],
        last=STATE["last"],
        provider=SET["provider"],
        model=current_model(),
        models={
            "ollama": list_ollama_models(),
            "groq": GROQ_PRESETS,
            "openai": OPENAI_PRESETS,
        },
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


def run_web():
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    start_telegram()
    console_loop()