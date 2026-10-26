import base64, io, json, os, re, time, webbrowser
from pathlib import Path

import pyautogui
import pyperclip
import requests
import yaml

BASE = Path(__file__).parent
cfg = yaml.safe_load(open(BASE / "config.yaml", encoding="utf-8"))
LLM = cfg["llm"]
MAX_STEPS = cfg["agent"].get("max_steps", 25)
USE_SCREENSHOT = LLM.get("use_screenshot", True)
DESKTOP = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
HEADERS = {"User-Agent": "Mozilla/5.0"}

pyautogui.FAILSAFE = True   # мышь в левый верхний угол = аварийная остановка
pyautogui.PAUSE = 0.3

SYSTEM = """Ты ИИ Агент Shiro. Ты управляешь Windows-компьютером. Выполняй задачу сам, шаг за шагом.
Ответ — ОДИН JSON-объект, без пояснений и без текста после него. Допустимые действия (других не существует):

{"action": "open_app", "name": "...."}              открыть приложение через меню Пуск
{"action": "open_url", "url": "https://..."}         открыть страницу в браузере
{"action": "click", "x": 100, "y": 200}              клик по координатам на скриншоте
{"action": "double_click", "x": 100, "y": 200}
{"action": "right_click", "x": 100, "y": 200}
{"action": "type", "text": "..."}                    напечатать текст в активное окно
{"action": "hotkey", "keys": ["ctrl", "s"]}
{"action": "scroll", "amount": -500}                 отрицательное = вниз
{"action": "wait", "seconds": 2}
{"action": "find_images", "query": "..."}            найти ссылки на картинки
{"action": "download", "url": "...", "filename": "..."}   скачать файл на рабочий стол
{"action": "read_page", "url": "..."}                прочитать текст страницы
{"action": "save_screenshot", "filename": "shot.png"}
{"action": "write_file", "filename": "notes.txt", "content": "..."}
{"action": "ask_user", "question": "..."}
{"action": "done", "summary": "что сделано"}

Правила:
1. Не спрашивай, если задача уже понятна. «Нарисуй X в Paint» значит: открой paint и нарисуй X.
2. Сначала открой нужное приложение (open_app), потом действуй в нём.
3. Не печатай в поле текст из самой задачи, если пользователь не просил что-то написать.
4. Когда задача выполнена, вызови done. Других способов завершить нет.
5. ask_user только если без ответа невозможно, например нужен файл, которого нет.
"""

# Модель иногда придумывает синонимы. Приводим их к известным действиям.
ALIASES = {"terminate": "done", "finish": "done", "complete": "done", "open": "open_app"}

RISKY = ("удал", "delete", "отправ", "оплат", "перезапис", "format", "install", "установ")


# ---------- экран ----------
def grab_screen():
    """Скриншот уменьшается до 1280px. Возвращает base64 и коэффициент пересчёта координат."""
    from PIL import Image  # импорт здесь, чтобы не падать, если Pillow не нужен
    img = pyautogui.screenshot().convert("RGB")
    real_w = img.width
    if real_w > 1280:
        img = img.resize((1280, int(img.height * 1280 / real_w)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode(), real_w / img.width


# ---------- модель ----------
def build_prompt(goal, history):
    recent = "\n".join(history[-10:]) or "(пока ничего)"
    return f"Задача: {goal}\n\nИстория действий:\n{recent}\n\nКакое следующее действие? Ответь одним JSON."


def parse_action(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа. Лишний текст до и после игнорируется."""
    start = text.find("{")
    if start == -1:
        raise ValueError(f"модель ответила без JSON: {text}")
    action, _ = json.JSONDecoder().raw_decode(text[start:])
    return action


def ask_model(goal, history, shot_b64):
    prompt = build_prompt(goal, history)

    if LLM["provider"] == "ollama":
        import ollama
        client = ollama.Client(host=LLM["host"])
        msg = {"role": "user", "content": prompt}
        if shot_b64:
            msg["images"] = [shot_b64]
        resp = client.chat(model=LLM["model"],
                           messages=[{"role": "system", "content": SYSTEM}, msg],
                           options={"temperature": 0.1})
        text = resp["message"]["content"]

    else:  # groq
        from groq import Groq
        client = Groq(api_key=LLM["groq_api_key"])
        content = [{"type": "text", "text": prompt}]
        if shot_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"}})
        resp = client.chat.completions.create(
            model=LLM["groq_model"],
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


def execute(a, factor):
    k = a["action"]

    if k == "open_app":
        pyautogui.press("win"); time.sleep(0.8)
        paste(a["name"]); time.sleep(1.5)
        pyautogui.press("enter"); time.sleep(3)
        return f"открыл приложение {a['name']}"

    if k == "open_url":
        webbrowser.open(a["url"]); time.sleep(3)
        return "страница открыта"

    if k in ("click", "double_click", "right_click"):
        x, y = int(a["x"] * factor), int(a["y"] * factor)
        {"click": pyautogui.click,
         "double_click": pyautogui.doubleClick,
         "right_click": pyautogui.rightClick}[k](x, y)
        return f"{k} в ({x}, {y})"

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


# ---------- цикл ----------
def run(goal):
    history = []
    for step in range(1, MAX_STEPS + 1):
        if USE_SCREENSHOT:
            shot, factor = grab_screen()
        else:
            shot, factor = None, 1.0

        try:
            a = ask_model(goal, history, shot)
        except Exception as e:
            print(f"ошибка модели: {e}")
            return
        print(f"[{step}] {a}")

        if a["action"] == "done":
            print("Готово:", a.get("summary", ""))
            return

        if a["action"] == "ask_user":
            ans = input(a.get("question", "Уточни") + "\n> ")
            history.append(f"спросил «{a.get('question')}», ответ: {ans}")
            continue

        if is_risky(a):
            if input("Опасное действие. Выполнить? (y/n) ").strip().lower() != "y":
                history.append(f"пользователь отменил: {a}")
                continue

        try:
            result = execute(a, factor)
        except Exception as e:
            result = f"ошибка: {e}"
        print("  →", result[:200])
        history.append(f"{json.dumps(a, ensure_ascii=False)} -> {result[:500]}")
        time.sleep(1)

    print("Лимит шагов исчерпан.")


if __name__ == "__main__":
    model_name = LLM["model"] if LLM["provider"] == "ollama" else LLM["groq_model"]
    print(f"Агент готов ({LLM['provider']}: {model_name}). «выход» — закрыть.")
    while True:
        try:
            goal = input("\n> ").strip()
        except KeyboardInterrupt:
            break
        if goal.lower() in ("выход", "exit", "quit"):
            break
        if goal:
            run(goal)