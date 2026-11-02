"""Сервер родительского контроля: принимает отчёты от агента и показывает панель."""
import asyncio
import html
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Kyiv"))
DB_PATH = os.environ.get("DB_PATH", "data.db")
PASSWORD = os.environ.get("PARENT_PASSWORD")
AGENT_KEY = os.environ.get("AGENT_KEY")

if not PASSWORD or not AGENT_KEY:
    raise SystemExit(
        "Задайте переменные окружения PARENT_PASSWORD (пароль для входа на сайт) "
        "и AGENT_KEY (секретный ключ агента). См. README.md"
    )

SECRET = os.environ.get("SESSION_SECRET") or (AGENT_KEY + PASSWORD)

DEFAULT_KEYWORDS = "porn\nпорно\nxxx\n18+\ncasino\nказино\nставки\nbetting"

app = FastAPI(title="Family Monitor")
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 24 * 30)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL,
                ts INTEGER NOT NULL,
                seconds INTEGER NOT NULL,
                process TEXT NOT NULL,
                title TEXT NOT NULL
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS devices (name TEXT PRIMARY KEY, last_seen INTEGER)")
        c.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('keywords', ?)",
            (DEFAULT_KEYWORDS,),
        )


init_db()


# ---------- API для агента ----------

class Event(BaseModel):
    ts: int
    seconds: int
    process: str
    title: str


class Report(BaseModel):
    device: str
    events: list[Event]


@app.post("/api/report")
def api_report(report: Report, x_agent_key: str = Header(default="")):
    if not secrets.compare_digest(x_agent_key, AGENT_KEY):
        raise HTTPException(status_code=401, detail="bad key")
    now = int(time.time())
    with db() as c:
        c.executemany(
            "INSERT INTO events(device, ts, seconds, process, title) VALUES(?,?,?,?,?)",
            [
                (report.device[:60], e.ts, max(0, min(e.seconds, 3600)), e.process[:100], e.title[:300])
                for e in report.events[:2000]
            ],
        )
        c.execute(
            "INSERT INTO devices(name, last_seen) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
            (report.device[:60], now),
        )
    return {"ok": True, "saved": len(report.events)}


# ---------- Просмотр экрана (видеопоток, кадры хранятся только в памяти) ----------
#
# Агент непрерывно шлёт кадры POST-ом на /api/frame, пока хоть один родитель
# смотрит /api/live/mjpeg. Эндпоинт /api/live/mjpeg держит соединение открытым
# и пересылает каждый новый кадр браузеру как multipart/x-mixed-replace —
# это и есть "видео" в теге <img>, без JS-опроса и лишней задержки.

LIVE: dict[str, dict] = {}
LIVE_LOCK = threading.Lock()


def _live(device: str) -> dict:
    with LIVE_LOCK:
        if device not in LIVE:
            LIVE[device] = {"frame": None, "frame_ts": 0.0, "event": threading.Event(), "watchers": 0}
        return LIVE[device]


def _check_key(key: str):
    if not secrets.compare_digest(key, AGENT_KEY):
        raise HTTPException(status_code=401, detail="bad key")


class Poll(BaseModel):
    device: str


@app.post("/api/agent/poll")
def agent_poll(p: Poll, x_agent_key: str = Header(default="")):
    """Агент спрашивает, нужно ли сейчас присылать кадры (кто-то открыл /live)."""
    _check_key(x_agent_key)
    device = p.device[:60]
    now = int(time.time())
    with db() as c:
        c.execute(
            "INSERT INTO devices(name, last_seen) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
            (device, now),
        )
    return {"stream": _live(device)["watchers"] > 0}


@app.post("/api/frame")
async def agent_frame(request: Request, device: str, x_agent_key: str = Header(default="")):
    _check_key(x_agent_key)
    data = await request.body()
    if len(data) > 3_000_000 or not data.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=400, detail="bad frame")
    st = _live(device[:60])
    st["frame"] = data
    st["frame_ts"] = time.time()
    st["event"].set()
    return {"ok": True}


def _mjpeg_generator(device: str):
    st = _live(device)
    with LIVE_LOCK:
        st["watchers"] += 1
    try:
        last_ts = 0.0
        # сразу отдаём последний известный кадр, если он свежий
        deadline = time.time() + 30
        while time.time() < deadline:
            st["event"].wait(timeout=2)
            st["event"].clear()
            if st["frame"] and st["frame_ts"] != last_ts:
                last_ts = st["frame_ts"]
                deadline = time.time() + 30  # есть сигнал от агента — продлеваем
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(st["frame"])).encode()
                    + b"\r\n\r\n" + st["frame"] + b"\r\n"
                )
    finally:
        with LIVE_LOCK:
            st["watchers"] = max(0, st["watchers"] - 1)


@app.get("/api/live/mjpeg")
def live_mjpeg(request: Request, device: str):
    if not is_logged_in(request):
        raise HTTPException(status_code=401)
    return StreamingResponse(
        _mjpeg_generator(device[:60]),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/live/status")
def live_status(request: Request, device: str):
    if not is_logged_in(request):
        raise HTTPException(status_code=401)
    st = _live(device[:60])
    age = time.time() - st["frame_ts"] if st["frame_ts"] else None
    return {"has_frame": age is not None and age < 8}


LIVE_PAGE = """
<div class="card nav"><a href="/">← К отчётам</a></div>
<div class="card">
  <h2>Экран: <span id="dev"></span></h2>
  <div id="st" class="muted">Подключаемся… (может занять до 10 секунд, пока агент не начал слать кадры)</div>
  <img id="scr" alt="экран" style="width:100%;border-radius:6px;display:none;margin-top:10px">
  <div class="muted" style="margin-top:8px">Поток идёт, пока эта страница открыта, и нигде не сохраняется.</div>
</div>
<script>
const DEV = __DEV__;
const img = document.getElementById('scr');
const st = document.getElementById('st');
document.getElementById('dev').textContent = DEV;

function startStream() {
  img.src = '/api/live/mjpeg?device=' + encodeURIComponent(DEV) + '&t=' + Date.now();
}
img.addEventListener('load', () => { img.style.display = 'none'; });  // поток оборвался — браузер "загрузил" пустое
img.addEventListener('error', () => { setTimeout(startStream, 1500); });
startStream();

async function poll() {
  try {
    const r = await fetch('/api/live/status?device=' + encodeURIComponent(DEV), {cache: 'no-store'});
    if (r.status === 401) { location = '/login'; return; }
    const j = await r.json();
    if (j.has_frame) {
      img.style.display = 'block';
      st.textContent = 'В эфире · ' + new Date().toLocaleTimeString();
    } else {
      st.textContent = 'Нет сигнала с ПК (выключен или агент не на связи)';
    }
  } catch (e) {}
  setTimeout(poll, 2000);
}
poll();
</script>
"""


@app.get("/live", response_class=HTMLResponse)
def live_page(request: Request, device: str = ""):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    if not device:
        with db() as c:
            row = c.execute("SELECT name FROM devices ORDER BY last_seen DESC LIMIT 1").fetchone()
        if not row:
            return render('<div class="card"><a href="/">← К отчётам</a><p class="muted">Агент ещё не выходил на связь.</p></div>')
        device = row["name"]
    dev_js = json.dumps(device).replace("</", "<\\/")
    return render(LIVE_PAGE.replace("__DEV__", dev_js))


# ---------- Вспомогательные функции ----------

def fmt(seconds: int) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин"
    return f"{seconds} сек"


def esc(s) -> str:
    return html.escape(str(s))


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("ok"))


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Родительский контроль</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f4f5f7;color:#1c1e21}}
 header{{background:#2d3a4f;color:#fff;padding:14px 20px;display:flex;justify-content:space-between;align-items:center}}
 header a{{color:#cfd8e6;text-decoration:none;font-size:14px}}
 main{{max-width:960px;margin:20px auto;padding:0 16px}}
 .card{{background:#fff;border-radius:10px;padding:16px 18px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 h2{{margin:0 0 10px;font-size:18px}}
 table{{width:100%;border-collapse:collapse;font-size:14px}}
 td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
 th{{color:#666;font-weight:600}}
 .flag{{background:#fdecea}}
 .muted{{color:#777;font-size:13px}}
 .ok{{color:#1a7f37}} .bad{{color:#c62828}}
 textarea{{width:100%;min-height:110px;box-sizing:border-box;font-family:inherit}}
 button{{background:#2d3a4f;color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer}}
 input[type=password],input[type=date]{{padding:7px;border:1px solid #ccc;border-radius:6px}}
 .nav a{{margin-right:10px}}
</style></head><body>
<header><strong>Родительский контроль</strong>{right}</header>
<main>{body}</main></body></html>"""


def render(body: str, logged: bool = True) -> HTMLResponse:
    right = '<a href="/logout">Выйти</a>' if logged else ""
    return HTMLResponse(PAGE.format(body=body, right=right))


# ---------- Вход ----------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    body = """<div class="card" style="max-width:340px;margin:60px auto">
    <h2>Вход</h2>
    <form method="post" action="/login">
      <p><input type="password" name="password" placeholder="Пароль" autofocus style="width:100%;box-sizing:border-box"></p>
      <button type="submit">Войти</button>
    </form></div>"""
    return render(body, logged=False)


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    if secrets.compare_digest(password, PASSWORD):
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    await asyncio.sleep(1.5)  # небольшая защита от подбора пароля
    body = '<div class="card" style="max-width:340px;margin:60px auto"><p class="bad">Неверный пароль.</p><a href="/login">Попробовать снова</a></div>'
    return render(body, logged=False)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- Панель ----------

def get_keywords() -> list[str]:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='keywords'").fetchone()
    raw = row["value"] if row else ""
    return [k.strip().lower() for k in raw.splitlines() if k.strip()]


@app.post("/keywords")
def save_keywords(request: Request, keywords: str = Form(""), d: str = Form("")):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('keywords', ?)", (keywords[:5000],))
    return RedirectResponse(f"/?d={d}" if d else "/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, d: str = ""):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    today = datetime.now(TZ).date()
    try:
        day = datetime.strptime(d, "%Y-%m-%d").date() if d else today
    except ValueError:
        day = today
    start = int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())
    end = int((datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(days=1)).timestamp())

    with db() as c:
        rows = c.execute(
            "SELECT ts, seconds, process, title FROM events WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, end),
        ).fetchall()
        devices = c.execute("SELECT name, last_seen FROM devices").fetchall()

    keywords = get_keywords()
    total = sum(r["seconds"] for r in rows)

    by_process: dict[str, int] = {}
    by_title: dict[tuple, int] = {}
    last_ts: dict[tuple, int] = {}
    for r in rows:
        by_process[r["process"]] = by_process.get(r["process"], 0) + r["seconds"]
        key = (r["process"], r["title"])
        by_title[key] = by_title.get(key, 0) + r["seconds"]
        last_ts[key] = max(last_ts.get(key, 0), r["ts"])

    def flagged(process: str, title: str) -> bool:
        hay = f"{process} {title}".lower()
        return any(k in hay for k in keywords)

    # Статус устройств
    now = int(time.time())
    dev_html = ""
    for dv in devices:
        ago = now - dv["last_seen"]
        online = ago < 180
        status = '<span class="ok">● на связи</span>' if online else f'<span class="muted">● не в сети (последний раз {fmt(ago)} назад)</span>'
        dev_html += f"<div>{esc(dv['name'])}: {status}</div>"
    if not devices:
        dev_html = '<div class="muted">Агент ещё ни разу не выходил на связь.</div>'

    # Подозрительное
    flagged_rows = [
        (p, t, s, last_ts[(p, t)]) for (p, t), s in by_title.items() if flagged(p, t)
    ]
    flagged_rows.sort(key=lambda x: -x[3])
    if flagged_rows:
        fl_html = "<table><tr><th>Время</th><th>Программа</th><th>Окно / страница</th><th>Сколько</th></tr>"
        for p, t, s, ts in flagged_rows[:100]:
            tm = datetime.fromtimestamp(ts, TZ).strftime("%H:%M")
            fl_html += f"<tr class='flag'><td>{tm}</td><td>{esc(p)}</td><td>{esc(t)}</td><td>{fmt(s)}</td></tr>"
        fl_html += "</table>"
    else:
        fl_html = '<div class="ok">За этот день совпадений со списком слов не найдено.</div>'

    # Топ программ
    proc_html = "<table><tr><th>Программа / игра</th><th>Время</th></tr>"
    for p, s in sorted(by_process.items(), key=lambda x: -x[1])[:30]:
        cls = " class='flag'" if flagged(p, "") else ""
        proc_html += f"<tr{cls}><td>{esc(p)}</td><td>{fmt(s)}</td></tr>"
    proc_html += "</table>" if by_process else ""
    if not by_process:
        proc_html = '<div class="muted">Нет данных за этот день.</div>'

    # Окна и страницы
    title_html = "<table><tr><th>Программа</th><th>Окно / страница</th><th>Время</th></tr>"
    for (p, t), s in sorted(by_title.items(), key=lambda x: -x[1])[:80]:
        cls = " class='flag'" if flagged(p, t) else ""
        title_html += f"<tr{cls}><td>{esc(p)}</td><td>{esc(t)}</td><td>{fmt(s)}</td></tr>"
    title_html += "</table>"
    if not by_title:
        title_html = '<div class="muted">Нет данных за этот день.</div>'

    prev_day = (day - timedelta(days=1)).isoformat()
    next_day = (day + timedelta(days=1)).isoformat()
    next_link = f'<a href="/?d={next_day}">След. день →</a>' if day < today else ""

    body = f"""
<div class="card nav">
  <a href="/?d={prev_day}">← Пред. день</a>
  <form method="get" action="/" style="display:inline">
    <input type="date" name="d" value="{day.isoformat()}" max="{today.isoformat()}">
    <button type="submit">Показать</button>
  </form>
  {next_link}
  <a href="/live" style="float:right">📺 Смотреть экран онлайн</a>
</div>
<div class="card"><h2>Устройства</h2>{dev_html}</div>
<div class="card"><h2>Всего за компьютером: {fmt(total)}</h2>
  <div class="muted">Дата: {day.strftime('%d.%m.%Y')}. Время считается, когда сын реально работал за ПК (простой не учитывается).</div></div>
<div class="card"><h2>⚠ Совпадения со списком слов</h2>{fl_html}</div>
<div class="card"><h2>Программы и игры</h2>{proc_html}</div>
<div class="card"><h2>Окна и страницы</h2>
  <div class="muted">Для браузеров показывается название вкладки (например, заголовок страницы).</div>{title_html}</div>
<div class="card"><h2>Список слов для отметки</h2>
  <div class="muted">По одному слову на строку. Проверяются название программы и заголовок окна. Можно добавлять названия игр.</div>
  <form method="post" action="/keywords">
    <input type="hidden" name="d" value="{day.isoformat()}">
    <textarea name="keywords">{esc(chr(10).join(keywords))}</textarea>
    <p><button type="submit">Сохранить</button></p>
  </form></div>
"""
    return render(body)
