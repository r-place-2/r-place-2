import asyncio
import base64
import hashlib
import secrets
import struct
import time
import os

import asyncpg

from aiohttp import web, WSMsgType

PORT = int(os.getenv("PORT", 3000))
COOLDOWN_S = 300
DB_URL = os.getenv("DATABASE_URL")

WIDTH = 500
HEIGHT = 500

canvas = bytearray(WIDTH * HEIGHT)
client_cooldowns = {}
ws_to_id = {}
WS_CLIENTS = set()
total_pixels_placed = 0
db_pool = None

PALETTE_RGB = [
    (0xFF, 0xFF, 0xFF), (0xE4, 0xE4, 0xE4), (0x88, 0x88, 0x88), (0x22, 0x22, 0x22),
    (0xFF, 0xA7, 0xD1), (0xE5, 0x00, 0x00), (0xE5, 0x95, 0x00), (0xA0, 0x6A, 0x42),
    (0xE5, 0xD9, 0x00), (0x94, 0xE0, 0x44), (0x02, 0xBE, 0x01), (0x00, 0xD3, 0xDD),
    (0x00, 0x83, 0xC7), (0x00, 0x00, 0xEA), (0xCF, 0x6E, 0xE4), (0x82, 0x00, 0x80),
    (0x00, 0x00, 0x00),
]

PALETTE_HEX = [f"#{r:02X}{g:02X}{b:02X}" for r, g, b in PALETTE_RGB]


async def init_db():
    global db_pool, WIDTH, HEIGHT, canvas
    if not DB_URL:
        print("[DB] No DATABASE_URL set – running in-memory only")
        return
    try:
        db_pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    except Exception as e:
        print(f"[DB] Connection failed ({e}) – running in-memory only")
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS canvas_state (
                id INTEGER PRIMARY KEY,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                data BYTEA NOT NULL
            )
        """)
        row = await conn.fetchrow(
            "SELECT width, height, data FROM canvas_state WHERE id = 1")
        if row:
            WIDTH = row["width"]
            HEIGHT = row["height"]
            canvas = bytearray(row["data"])
            print(f"[DB] Loaded canvas {WIDTH}x{HEIGHT}")
        else:
            await conn.execute(
                "INSERT INTO canvas_state (id, width, height, data) "
                "VALUES (1, $1, $2, $3)",
                WIDTH, HEIGHT, bytes(canvas))
            print("[DB] Created new canvas")


async def save_pixel(pos, idx):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE canvas_state SET data = "
                "overlay(data placing $1::bytea from $2 for 1) "
                "WHERE id = 1",
                bytes([idx]), pos + 1)
    except Exception:
        pass


async def save_canvas():
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE canvas_state SET width=$1, height=$2, data=$3 "
                "WHERE id = 1",
                WIDTH, HEIGHT, bytes(canvas))
    except Exception:
        pass


def rle_encode():
    buf = bytearray()
    i = 0
    total = WIDTH * HEIGHT
    while i < total:
        val = canvas[i]
        count = 1
        while i + count < total and count < 256 and canvas[i + count] == val:
            count += 1
        buf.append(count - 1)
        buf.append(val)
        i += count
    return buf


def build_init_msg():
    return struct.pack(">BHH", 0, WIDTH, HEIGHT) + rle_encode()


def resize_canvas(direction, amount):
    global WIDTH, HEIGHT, canvas
    old_w, old_h = WIDTH, HEIGHT
    white = 0

    if direction == "up":
        HEIGHT = old_h + amount
        canvas = bytearray([white] * (old_w * amount)) + canvas
    elif direction == "down":
        HEIGHT = old_h + amount
        canvas = canvas + bytearray([white] * (old_w * amount))
    elif direction == "left":
        WIDTH = old_w + amount
        new = bytearray()
        for y in range(old_h):
            new.extend(bytearray([white] * amount))
            new.extend(canvas[y * old_w:(y + 1) * old_w])
        canvas = new
    elif direction == "right":
        WIDTH = old_w + amount
        new = bytearray()
        for y in range(old_h):
            new.extend(canvas[y * old_w:(y + 1) * old_w])
            new.extend(bytearray([white] * amount))
        canvas = new


async def index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


ADMIN_USER = "Ben"
ADMIN_PASS = "Ben2013"
ADMIN_TOKEN = hashlib.sha256(f"{ADMIN_USER}:{ADMIN_PASS}".encode()).hexdigest()


def check_admin(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, _, passwd = decoded.partition(":")
            return user == ADMIN_USER and passwd == ADMIN_PASS
        except Exception:
            pass
    return request.cookies.get("admin_token", "") == ADMIN_TOKEN


LOGIN_FORM = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    "<title>r/place 2 – Admin Login</title>"
    "<style>"
    "body{font-family:system-ui,sans-serif;background:#111;color:#eee;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
    "form{background:#1a1a1a;padding:30px;border-radius:8px;border:1px solid #333;width:280px}"
    "input{display:block;margin:10px 0;padding:8px 12px;background:#222;color:#eee;"
    "border:1px solid #555;border-radius:4px;font-size:16px;width:100%;box-sizing:border-box}"
    'button{padding:8px 20px;background:#333;color:#eee;border:1px solid #555;'
    "border-radius:4px;cursor:pointer;font-size:16px}"
    "button:hover{background:#444}" ".error{color:#f66;font-size:14px;margin:10px 0}"
    "</style></head><body>"
    '<form method="post" action="/admin">'
    "<h2>Admin Login</h2>"
    '<input type="password" name="pw" placeholder="Password" autofocus>'
    "{error}"
    '<button type="submit">Login</button>'
    "</form></body></html>"
)


async def admin_handler(request):
    global WS_CLIENTS, WIDTH, HEIGHT
    if not check_admin(request):
        if request.method == "POST":
            data = await request.post()
            if data.get("pw") == ADMIN_PASS:
                resp = web.HTTPFound("/admin")
                resp.set_cookie("admin_token", ADMIN_TOKEN,
                                max_age=86400 * 7, httponly=True,
                                samesite="Lax", path="/")
                print(f"[+] Admin login from {request.remote}")
                return resp
        if request.method == "GET":
            return web.Response(
                content_type="text/html",
                text=LOGIN_FORM.replace("{error}", ""))
        return web.Response(
            content_type="text/html",
            text=LOGIN_FORM.replace(
                "{error}", '<div class="error">Wrong password</div>'))

    if request.method == "POST":
        data = await request.post()
        action = data.get("action")
        if action == "clear":
            canvas[:] = bytearray(WIDTH * HEIGHT)
            await save_canvas()
            dead = set()
            for c in WS_CLIENTS:
                try:
                    await c.send_bytes(build_init_msg())
                except (ConnectionResetError, ConnectionAbortedError):
                    dead.add(c)
            WS_CLIENTS -= dead
            return web.HTTPFound("/admin")
        if action == "resize":
            direction = data.get("dir", "")
            try:
                amount = int(data.get("amount", 0))
            except ValueError:
                amount = 0
            if direction in ("up", "down", "left", "right") and 1 <= amount <= 200:
                resize_canvas(direction, amount)
                await save_canvas()
                dead = set()
                for c in WS_CLIENTS:
                    try:
                        await c.send_bytes(build_init_msg())
                    except (ConnectionResetError, ConnectionAbortedError):
                        dead.add(c)
                WS_CLIENTS -= dead
            return web.HTTPFound("/admin")

    color_counts = {}
    for idx in canvas:
        color_counts[idx] = color_counts.get(idx, 0) + 1

    rows = "".join(
        f"<tr><td style='background:{PALETTE_HEX[idx]};width:24px;height:24px'></td>"
        f"<td>{PALETTE_HEX[idx]}</td>"
        f"<td>{count}</td>"
        f"<td>{count / (WIDTH * HEIGHT) * 100:.1f}%</td></tr>"
        for idx, count in sorted(color_counts.items(), key=lambda x: -x[1])
    )

    return web.Response(
        content_type="text/html",
        text=f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>r/place 2 – Admin</title>
<style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;padding:20px}}
h1{{color:#fff}}
table{{border-collapse:collapse}}
td,th{{padding:6px 12px;text-align:left;border-bottom:1px solid #333}}
th{{color:#888;text-transform:uppercase;font-size:12px;letter-spacing:1px}}
.stat{{font-size:24px;font-weight:700}}
.stat-label{{color:#888;font-size:13px}}
.grid{{display:flex;gap:40px;flex-wrap:wrap;margin-bottom:30px}}
.danger{{background:#500;color:#f88;padding:8px 16px;border:none;border-radius:4px;cursor:pointer;font-size:14px}}
.resize-input{{width:60px;padding:6px;background:#222;color:#eee;border:1px solid #555;border-radius:4px}}
.resize-btn{{padding:6px 14px;background:#333;color:#eee;border:1px solid #777;border-radius:4px;cursor:pointer}}
.resize-btn:hover{{background:#444}}
</style>
</head>
<body>
<h1>r/place 2 – Admin</h1>
<div class="grid">
  <div><div class="stat">{len(WS_CLIENTS)}</div><div class="stat-label">Active</div></div>
  <div><div class="stat">{total_pixels_placed}</div><div class="stat-label">Pixels Placed</div></div>
  <div><div class="stat">{len(client_cooldowns)}</div><div class="stat-label">Users</div></div>
  <div><div class="stat">{COOLDOWN_S}s</div><div class="stat-label">Cooldown</div></div>
  <div><div class="stat">{WIDTH}&times;{HEIGHT}</div><div class="stat-label">Size</div></div>
  <div><div class="stat">{len(canvas)}</div><div class="stat-label">Canvas Bytes</div></div>
</div>
<form method="post" action="/admin" onsubmit="return confirm('Clear entire canvas?')">
  <input type="hidden" name="action" value="clear">
  <button class="danger">Clear Canvas</button>
</form>
<h2>Resize</h2>
<form method="post" action="/admin" style="margin-bottom:20px">
  <input type="hidden" name="action" value="resize">
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <input class="resize-input" type="number" name="amount" min="1" max="200" value="10">
    <button class="resize-btn" type="submit" name="dir" value="up">Up</button>
    <button class="resize-btn" type="submit" name="dir" value="down">Down</button>
    <button class="resize-btn" type="submit" name="dir" value="left">Left</button>
    <button class="resize-btn" type="submit" name="dir" value="right">Right</button>
    <span style="color:#888;font-size:13px">px</span>
  </div>
</form>
<h2>Pixel Distribution</h2>
<table><tr><th>Color</th><th>Hex</th><th>Count</th><th>%</th></tr>{rows}</table>
</body>
</html>""")


async def websocket_handler(request):
    global WS_CLIENTS, WIDTH, HEIGHT, total_pixels_placed
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    WS_CLIENTS.add(ws)
    client_id = None
    is_admin = check_admin(request)
    addr = request.remote
    print(f"[+] {addr} connected  admin={is_admin}")

    try:
        await ws.send_bytes(build_init_msg())

        async for msg in ws:
            try:
                if msg.type != WSMsgType.BINARY:
                    continue
                data = msg.data
                if len(data) < 1:
                    continue
                mt = data[0]

                if mt == 0x10 and len(data) >= 9:
                    client_id = bytes(data[1:9])
                    ws_to_id[ws] = client_id
                    continue

                if mt != 1 or len(data) < 8:
                    continue

                x = (data[1] << 16) | (data[2] << 8) | data[3]
                y = (data[4] << 16) | (data[5] << 8) | data[6]
                palette_idx = data[7]

                if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                    continue
                if not (0 <= palette_idx < len(PALETTE_RGB)):
                    continue

                if not is_admin:
                    key = client_id if client_id else id(ws)
                    now = time.time()
                    last = client_cooldowns.get(key, 0)
                    remaining = COOLDOWN_S - (now - last)

                    if remaining > 0:
                        print(f"[!] {addr} cooldown {remaining:.0f}s")
                        await ws.send_bytes(struct.pack(">BI", 2, int(remaining * 1000)))
                        continue

                    client_cooldowns[key] = now

                total_pixels_placed += 1
                pos = y * WIDTH + x
                canvas[pos] = palette_idx
                asyncio.ensure_future(save_pixel(pos, palette_idx))
                print(f"[.] {addr} pixel {x},{y} idx={palette_idx}"
                      f"{' [admin]' if is_admin else ''}")

                pixel_msg = bytes([
                    1,
                    (x >> 16) & 0xFF, (x >> 8) & 0xFF, x & 0xFF,
                    (y >> 16) & 0xFF, (y >> 8) & 0xFF, y & 0xFF,
                    palette_idx,
                ])

                dead = set()
                for c in WS_CLIENTS:
                    try:
                        await c.send_bytes(pixel_msg)
                    except (ConnectionResetError, ConnectionAbortedError):
                        dead.add(c)
                WS_CLIENTS -= dead
            except Exception:
                pass
    finally:
        WS_CLIENTS.discard(ws)
        ws_to_id.pop(ws, None)
        print(f"[-] {addr} disconnected")
    return ws


STATIC_DIR = os.path.join(os.path.dirname(__file__), "public")

app = web.Application()
app.router.add_get("/", index)
app.router.add_route("*", "/admin", admin_handler)
app.router.add_static("/", STATIC_DIR)
app.router.add_get("/ws", websocket_handler)

async def on_startup(app):
    await init_db()

app.on_startup.append(on_startup)

if __name__ == "__main__":
    print(f"r/place 2 running at http://localhost:{PORT}")
    web.run_app(app, port=PORT)
