import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # координаты в реальных пикселях при масштабе 125%/150%
except Exception:
    pass

import base64, io, json, logging, os, re, threading, time, urllib.parse, webbrowser, difflib
from pathlib import Path

import numpy as np
import pyautogui
import pyperclip
import requests
import uiautomation as auto
import yaml
from flask import Flask, jsonify, request, Response
from PIL import ImageDraw

BASE = Path(__file__).parent
cfg = yaml.safe_load(open(BASE / "config.yaml", encoding="utf-8"))
LLM = cfg["llm"]
AG = cfg.get("agent", {})
MAX_STEPS = AG.get("max_steps", 25)
WEB_HOST = AG.get("web_host", "127.0.0.1")
WEB_PORT = AG.get("web_port", 8000)
DESKTOP = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
HEADERS = {"User-Agent": "Mozilla/5.0"}

pyautogui.FAILSAFE = True   # мышь в левый верхний угол = аварийная остановка
pyautogui.PAUSE = 0.3
logging.getLogger("werkzeug").setLevel(logging.ERROR)

GROQ_PRESETS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
]

SET = {
    "provider": LLM.get("provider", "ollama"),
    "ollama_model": LLM.get("model", "qwen2.5vl:3b"),
    "ollama_host": LLM.get("host", "http://localhost:11434"),
    "groq_model": LLM.get("groq_model") or GROQ_PRESETS[0],
    "groq_key": LLM.get("groq_api_key", ""),
    "use_screenshot": LLM.get("use_screenshot", True),
}

SYSTEM = """Ты управляешь Windows-компьютером. Выполняй задачу сам, шаг за шагом.
Ответ — ОДИН JSON-объект, без пояснений и без текста после него. Допустимые действия:

{"action": "open_app", "name": "paint"}              открыть приложение через меню Пуск
{"action": "open_url", "url": "https://..."}         открыть страницу в браузере
{"action": "click_text", "text": "Сохранить"}        клик по тексту/кнопке на экране
{"action": "double_click_text", "text": "Файл"}      двойной клик по тексту
{"action": "click", "x": 100, "y": 200}              клик по координатам (только для иконок без текста)
{"action": "type", "text": "..."}                    напечатать текст в активное окно
{"action": "hotkey", "keys": ["ctrl", "s"]}
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
2. Сначала открой нужное приложение (open_app), потом действуй в нём.
3. Не печатай в поле текст из самой задачи, если пользователь не просил что-то написать.
4. Когда задача выполнена, вызови done.
5. ask_user только если без ответа невозможно.
6. Чтобы нажать на кнопку, ссылку или пункт меню, используй click_text и точно скопируй текст с экрана. Координаты не придумывай.
7. click с координатами используй только для иконок без подписи.
8. Если click_text ответил «не найден», попробуй другую формулировку или другое действие, но не повторяй тот же запрос.
"""

ALIASES = {"terminate": "done", "finish": "done", "complete": "done", "open": "open_app",
           "click_on_text": "click_text"}
RISKY = ("удал", "delete", "отправ", "оплат", "перезапис", "format", "install", "установ")

STATE = {"busy": False, "stop": False, "question": None, "last": ""}
ANSWER = {"text": ""}
ANSWER_EVENT = threading.Event()


class StopRun(Exception):
    pass


def out(msg: str):
    STATE["last"] = msg
    print(msg, flush=True)


def ask(question: str) -> str:
    """Спрашивает пользователя. Ответить можно в консоли или в веб-панели."""
    STATE["question"] = question
    ANSWER_EVENT.clear()
    print(f"\n❓ {question}\n(ответь в консоли или в веб-панели)", flush=True)
    while not ANSWER_EVENT.wait(timeout=0.5):
        if STATE["stop"]:
            STATE["question"] = None
            raise StopRun()
    STATE["question"] = None
    if STATE["stop"]:
        raise StopRun()
    return ANSWER["text"]


# ---------- скриншот с сеткой (для модели) ----------
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


# ---------- поиск текста на экране ----------
def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", (s or "").lower())


def _score(target: str, name: str) -> float:
    t, n = _norm(target), _norm(name)
    if not t or not n:
        return 0.0
    if t == n:
        return 1.0
    if t in n:
        return 0.9
    return difflib.SequenceMatcher(None, t, n).ratio()


def find_by_uia(text: str, max_nodes: int = 4000):
    """Ищет элемент с нужным именем в окне переднего плана через UI Automation."""
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
                    rect = ctrl.BoundingRectangle
                    if s >= 0.8 and rect.width() > 0 and rect.height() > 0:
                        if best is None or s > best[0]:
                            best = (s, rect.xcenter(), rect.ycenter())
                if depth < 25:
                    try:
                        for ch in ctrl.GetChildren():
                            stack.append((ch, depth + 1))
                    except Exception:
                        pass
            if best:
                return best[1], best[2], f"UIA ({best[0]:.2f})"
            return None
    except Exception as e:
        out(f"UIA недоступен: {e}")
        return None


_ocr_engine = None


def find_by_ocr(text: str):
    """Запасной вариант: OCR по скриншоту (RapidOCR, pip-only)."""
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
        if s >= 0.75 and (best is None or s > best[0]):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            best = (s, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    if best:
        return int(best[1]), int(best[2]), f"OCR ({best[0]:.2f})"
    return None


def find_text(text: str):
    found = find_by_uia(text)
    if found:
        return found
    try:
        return find_by_ocr(text)
    except Exception as e:
        out(f"OCR недоступен: {e}")
        return None


# ---------- модель ----------
def current_model() -> str:
    return SET["ollama_model"] if SET["provider"] == "ollama" else SET["groq_model"]


def list_ollama_models():
    try:
        import ollama
        res = ollama.Client(host=SET["ollama_host"]).list()
        items = res["models"] if isinstance(res, dict) else res.models
        return [m["model"] if isinstance(m, dict) else m.model for m in items]
    except Exception:
        return []


def build_prompt(goal, history, w, h):
    recent = "\n".join(history[-10:]) or "(пока ничего)"
    return (f"Задача: {goal}\nРазмер экрана: {w}x{h}.\n\n"
            f"История действий:\n{recent}\n\nКакое следующее действие? Ответь одним JSON.")


def parse_action(text: str) -> dict:
    start = text.find("{")
    if start == -1:
        raise ValueError(f"модель ответила без JSON: {text}")
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
        resp = client.chat(model=SET["ollama_model"],
                           messages=[{"role": "system", "content": SYSTEM}, msg],
                           options={"temperature": 0.1})
        text = resp["message"]["content"]
    else:
        from groq import Groq
        client = Groq(api_key=SET["groq_key"])
        content = [{"type": "text", "text": prompt}]
        if shot_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"}})
        resp = client.chat.completions.create(
            model=SET["groq_model"],
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": content}],
            temperature=0.1, max_tokens=600)
        text = resp.choices[0].message.content or ""

    action = parse_action(text)
    kind = action.get("action", "")
    action["action"] = ALIASES.get(kind, kind)
    return action


# ---------- действия ----------
def paste(text):
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")


def find_images(query):
    html = requests.get("https://www.bing.com/images/search",
                        params={"q": query}, headers=HEADERS, timeout=15).text
    urls = list(dict.fromkeys(re.findall(r'"murl":"(https?://[^"]+?)"', html)))[:8]
    return "\n".join(urls) if urls else "ничего не найдено"


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
    return f"скачано: {path}"


def execute(a):
    k = a["action"]

    if k == "open_app":
        pyautogui.press("win"); time.sleep(0.8)
        paste(a["name"]); time.sleep(1.5)
        pyautogui.press("enter"); time.sleep(3)
        return f"открыл приложение {a['name']}"

    if k == "open_url":
        webbrowser.open(a["url"]); time.sleep(3)
        return "страница открыта"

    if k in ("click_text", "double_click_text"):
        found = find_text(a["text"])
        if not found:
            return f"текст «{a['text']}» на экране не найден"
        x, y, how = found
        pyautogui.moveTo(x, y, duration=0.3)
        if k == "click_text":
            pyautogui.click(x, y)
        else:
            pyautogui.doubleClick(x, y)
        return f"клик по «{a['text']}» в ({x}, {y}), {how}"

    if k == "click":
        x, y = int(a["x"]), int(a["y"])
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click(x, y)
        return f"клик в ({x}, {y})"

    if k == "type":
        if a["text"].isascii():
            pyautogui.write(a["text"], interval=0.02)
        else:
            paste(a["text"])
        return "текст введён"

    if k == "hotkey":
        pyautogui.hotkey(*a["keys"])
        return "горячая клавиша нажата"

    if k == "scroll":
        pyautogui.scroll(int(a["amount"]))
        return "прокрутка"

    if k == "wait":
        time.sleep(float(a.get("seconds", 1)))
        return "ждал"

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
        return f"скриншот сохранён: {path}"

    if k == "write_file":
        DESKTOP.mkdir(exist_ok=True)
        path = DESKTOP / a["filename"]
        path.write_text(a["content"], encoding="utf-8")
        return f"файл записан: {path}"

    return f"неизвестное действие «{k}», пропускаю"


def is_risky(a):
    return any(w in json.dumps(a, ensure_ascii=False).lower() for w in RISKY)


# ---------- быстрые команды без модели ----------
def quick_route(goal: str) -> bool:
    low = goal.lower().strip()

    if re.search(r"(песн|музык|трек)", low):
        query = re.sub(r"\(.*?\)", "", low)
        query = re.sub(r"(найди|включи|поставь|запусти|песню|песня|музыку|музыка|трек|"
                       r"на ют|на ютубе|в инете|в интернете|и включи)", "", query)
        query = re.sub(r"\s+", " ", query).strip()
        if query:
            webbrowser.open("https://music.youtube.com/search?q=" + urllib.parse.quote(query))
            out(f"Открыл YouTube Music: «{query}». Выбери трек вручную.")
        else:
            out("Какую песню включить?")
        return True

    m = re.match(r"^(найди|поищи|загугли)\s+(.+)$", low)
    if m:
        webbrowser.open("https://duckduckgo.com/?q=" + urllib.parse.quote(m.group(2)))
        out("Открыл поиск.")
        return True

    return False


# ---------- цикл агента ----------
def run(goal):
    history = []
    last_key = None
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
            out(f"ошибка модели: {e}")
            return

        print(f"[{step}] {a}", flush=True)

        key = json.dumps(a, ensure_ascii=False, sort_keys=True)
        if key == last_key:
            out("Одно и то же действие повторяется, останавливаюсь.")
            return
        last_key = key

        if a.get("action") == "done":
            out("Готово: " + a.get("summary", ""))
            return

        if a.get("action") == "ask_user":
            ans = ask(a.get("question", "Уточни"))
            history.append(f"спросил «{a.get('question')}», ответ: {ans}")
            continue

        if is_risky(a):
            ans = ask(f"Опасное действие: {json.dumps(a, ensure_ascii=False)}. Выполнить? (да/нет)")
            if ans.strip().lower() not in ("да", "yes", "y"):
                history.append(f"пользователь отменил: {a}")
                continue

        try:
            result = execute(a)
        except Exception as e:
            result = f"ошибка: {e}"
        print(f"  → {result[:200]}", flush=True)
        history.append(f"{json.dumps(a, ensure_ascii=False)} -> {result[:500]}")
        time.sleep(1)

    out("Лимит шагов исчерпан.")


def worker(goal):
    STATE["busy"] = True
    STATE["stop"] = False
    try:
        out(f"Задача: {goal}")
        if not quick_route(goal):
            run(goal)
    except StopRun:
        out("Остановлено.")
    except Exception as e:
        out(f"Ошибка: {e}")
    finally:
        STATE["busy"] = False
        STATE["question"] = None


def start_goal(goal) -> bool:
    if STATE["busy"]:
        print("Агент уже занят. Дождись окончания или напиши «стоп».", flush=True)
        return False
    threading.Thread(target=worker, args=(goal,), daemon=True).start()
    return True


def stop_agent():
    STATE["stop"] = True
    ANSWER_EVENT.set()
    print("Останавливаю...", flush=True)


def set_model(provider=None, model=None):
    if provider in ("ollama", "groq"):
        SET["provider"] = provider
    if model:
        if SET["provider"] == "ollama":
            SET["ollama_model"] = model
        else:
            SET["groq_model"] = model


def show_models():
    print("Ollama (установленные):", flush=True)
    for m in list_ollama_models() or ["(нет или Ollama не запущена)"]:
        print("  ", m, flush=True)
    print("Groq (варианты):", flush=True)
    for m in GROQ_PRESETS:
        print("  ", m, flush=True)
    print(f"Сейчас: {SET['provider']} / {current_model()}", flush=True)


HELP = """Команды:
  любая фраза        выполнить задачу, например: открой ворд
  модели             показать доступные модели
  провайдер ollama|groq
  модель <имя>       выбрать модель для текущего провайдера
  стоп               остановить агента
  выход              закрыть программу"""


def console_loop():
    print(HELP, flush=True)
    print(f"Сейчас: {SET['provider']} / {current_model()}", flush=True)
    print(f"Веб-панель: http://localhost:{WEB_PORT}", flush=True)
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

        low = line.lower()
        if low in ("выход", "exit", "quit"):
            break
        if low in ("стоп", "stop"):
            stop_agent()
        elif low in ("помощь", "help", "?"):
            print(HELP, flush=True)
        elif low in ("модели", "models"):
            show_models()
        elif low.startswith("провайдер "):
            p = low.split(maxsplit=1)[1].strip()
            if p in ("ollama", "groq"):
                set_model(provider=p)
                print(f"Провайдер: {p} / {current_model()}", flush=True)
            else:
                print("Пиши: провайдер ollama  или  провайдер groq", flush=True)
        elif low.startswith("модель "):
            name = line.split(maxsplit=1)[1].strip()
            set_model(model=name)
            print(f"Модель: {SET['provider']} / {current_model()}", flush=True)
        else:
            start_goal(line)


# ---------- веб-панель ----------
app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Агент</title>
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
<h2>Управление агентом</h2>
<div class="muted" id="model"></div>

<div class="row">
  <select id="provider"><option value="ollama">Ollama (локально)</option><option value="groq">Groq (облако)</option></select>
  <input id="model" class="grow" list="models" placeholder="Модель">
  <datalist id="models"></datalist>
  <button class="apply" onclick="applySettings()">Применить</button>
</div>

<div class="row">
  <input id="goal" class="grow" placeholder="Например: открой ворд" autofocus>
  <button class="go" onclick="runTask()">Запустить</button>
  <button class="stop" onclick="stopAgent()">Стоп</button>
</div>

<div id="status"></div>
<div id="question">
  <div id="qtext"></div>
  <div class="row"><input id="answer" class="grow" placeholder="Ответ агенту">
  <button class="go" onclick="sendAnswer()">Ответить</button></div>
</div>

<script>
const $ = id => document.getElementById(id);
let models = {ollama: [], groq: []};
let lastProvider = null;

async function load() {
  const s = await (await fetch('/api/state')).json();
  models = s.models;
  $('status').textContent = (s.busy ? 'Работает. ' : 'Ожидает задачу. ') + (s.last || '');
  $('model').textContent = 'Сейчас: ' + s.provider + ' / ' + s.model;
  if (!$('provider').matches(':focus')) $('provider').value = s.provider;
  if (lastProvider !== s.provider) {
    lastProvider = s.provider;
    $('models').innerHTML = (models[s.provider] || []).map(m => `<option value="${m}">`).join('');
  }
  if (document.activeElement !== $('model')) $('model').value = s.model;
  $('question').style.display = s.question ? 'block' : 'none';
  $('qtext').textContent = s.question ? 'Агент спрашивает: ' + s.question : '';
}

async function applySettings() {
  await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({provider: $('provider').value, model: $('model').value.trim()})});
  lastProvider = null; load();
}
async function runTask() {
  const goal = $('goal').value.trim(); if (!goal) return;
  const r = await fetch('/api/task', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({goal})});
  if (r.status === 409) alert('Агент уже занят. Нажми Стоп или дождись окончания.');
  $('goal').value = ''; load();
}
async function stopAgent() { await fetch('/api/stop', {method:'POST'}); load(); }
async function sendAnswer() {
  const text = $('answer').value; $('answer').value = '';
  await fetch('/api/answer', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})});
  load();
}
$('goal').addEventListener('keydown', e => { if (e.key === 'Enter') runTask(); });
$('answer').addEventListener('keydown', e => { if (e.key === 'Enter') sendAnswer(); });
$('provider').addEventListener('change', () => {
  $('models').innerHTML = (models[$('provider').value] || []).map(m => `<option value="${m}">`).join('');
  $('model').value = (models[$('provider').value] || [''])[0] || '';
});
setInterval(load, 1000); load();
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
        models={"ollama": list_ollama_models(), "groq": GROQ_PRESETS},
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
    console_loop()