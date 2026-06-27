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
BACKUP_DB_URL = os.getenv("BACKUP_DB_URL")

WIDTH = 500
HEIGHT = 500

canvas = bytearray(WIDTH * HEIGHT)
client_cooldowns = {}
ws_to_id = {}
WS_CLIENTS = set()
total_pixels_placed = 0
db_pool = None
backup_pool = None
LOCKED = False

PALETTE_RGB = [
    (0xFF, 0xFF, 0xFF), (0xE4, 0xE4, 0xE4), (0x88, 0x88, 0x88), (0x22, 0x22, 0x22),
    (0xFF, 0xA7, 0xD1), (0xE5, 0x00, 0x00), (0xE5, 0x95, 0x00), (0xA0, 0x6A, 0x42),
    (0xE5, 0xD9, 0x00), (0x94, 0xE0, 0x44), (0x02, 0xBE, 0x01), (0x00, 0xD3, 0xDD),
    (0x00, 0x83, 0xC7), (0x00, 0x00, 0xEA), (0xCF, 0x6E, 0xE4), (0x82, 0x00, 0x80),
    (0x00, 0x00, 0x00),
]

PALETTE_HEX = [f"#{r:02X}{g:02X}{b:02X}" for r, g, b in PALETTE_RGB]


async def connect_db(url):
    pool = None
    try:
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS canvas_state (
                    id INTEGER PRIMARY KEY,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    data BYTEA NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS canvas_snapshots (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    data BYTEA NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        return pool
    except Exception as e:
        if pool:
            await pool.close()
        raise e


async def init_db():
    global db_pool, backup_pool, WIDTH, HEIGHT, canvas
    primary_url = DB_URL
    fallback_url = BACKUP_DB_URL

    if not primary_url and not fallback_url:
        print("[DB] No database configured – running in-memory only")
        return

    # Try primary
    primary_ok = False
    if primary_url:
        try:
            db_pool = await connect_db(primary_url)
            primary_ok = True
            print(f"[DB] Connected to primary database")
        except Exception as e:
            print(f"[DB] Primary connection failed: {e}")

    # Try fallback if primary failed
    if not primary_ok and fallback_url:
        try:
            db_pool = await connect_db(fallback_url)
            print(f"[DB] Using fallback database (primary unavailable)")
        except Exception as e:
            print(f"[DB] Fallback connection failed: {e}")

    if not db_pool:
        print("[DB] All databases failed – running in-memory only")
        return

    # Connect backup pool (best-effort)
    if fallback_url:
        try:
            backup_pool = await connect_db(fallback_url)
            print(f"[DB] Backup database connected")
        except Exception as e:
            print(f"[DB] Backup connection failed: {e}")

    # Load canvas from whichever pool succeeded
    async with db_pool.acquire() as conn:
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


async def backup_loop():
    while True:
        await asyncio.sleep(600)
        if not backup_pool:
            continue
        try:
            async with backup_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE canvas_state SET width=$1, height=$2, data=$3 "
                    "WHERE id = 1",
                    WIDTH, HEIGHT, bytes(canvas))
            print("[Backup] Canvas saved to backup DB")
        except Exception as e:
            print(f"[Backup] Failed: {e}")


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


async def save_snapshot(name):
    if not db_pool:
        return False
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO canvas_snapshots (name, width, height, data) "
                "VALUES ($1, $2, $3, $4)",
                name, WIDTH, HEIGHT, bytes(canvas))
        return True
    except Exception:
        return False


async def get_snapshots():
    if not db_pool:
        return []
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, width, height, created_at "
                "FROM canvas_snapshots ORDER BY created_at DESC")
            return [
                {"id": r["id"], "name": r["name"],
                 "width": r["width"], "height": r["height"],
                 "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""}
                for r in rows
            ]
    except Exception:
        return []


async def get_snapshot(id_):
    if not db_pool:
        return None
    try:
        async with db_pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM canvas_snapshots WHERE id = $1", id_)
    except Exception:
        return None


async def delete_snapshot(id_):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM canvas_snapshots WHERE id = $1", id_)
    except Exception:
        pass


async def broadcast_lock_status():
    global WS_CLIENTS
    msg = struct.pack(">BB", 3, 1 if LOCKED else 0)
    dead = set()
    for c in list(WS_CLIENTS):
        try:
            await c.send_bytes(msg)
        except Exception:
            dead.add(c)
    WS_CLIENTS -= dead


def rle_encode(data, w, h):
    buf = bytearray()
    i = 0
    total = w * h
    while i < total:
        val = data[i]
        count = 1
        while i + count < total and count < 256 and data[i + count] == val:
            count += 1
        buf.append(count - 1)
        buf.append(val)
        i += count
    return buf


def build_init_msg():
    return struct.pack(">BHH", 0, WIDTH, HEIGHT) + rle_encode(canvas, WIDTH, HEIGHT)


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
    global WS_CLIENTS, WIDTH, HEIGHT, LOCKED
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
            for c in list(WS_CLIENTS):
                try:
                    await c.send_bytes(build_init_msg())
                except Exception:
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
                for c in list(WS_CLIENTS):
                    try:
                        await c.send_bytes(build_init_msg())
                    except Exception:
                        dead.add(c)
                WS_CLIENTS -= dead
            return web.HTTPFound("/admin")

        if action == "lock":
            LOCKED = data.get("state") == "1"
            await broadcast_lock_status()
            print(f"[Admin] Canvas {'locked' if LOCKED else 'unlocked'}")
            return web.HTTPFound("/admin")

        if action == "snapshot":
            name = data.get("name", "").strip()
            ok = await save_snapshot(name)
            if ok:
                print(f"[Admin] Snapshot saved: {name or 'unnamed'}")
            return web.HTTPFound("/admin")

        if action == "del_snapshot":
            try:
                sid = int(data.get("id", 0))
                await delete_snapshot(sid)
            except ValueError:
                pass
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

    snapshots = await get_snapshots()
    snap_rows = "".join(
        f"<tr>"
        f"<td>{s['name'] or '—'}</td>"
        f"<td>{s['width']}&times;{s['height']}</td>"
        f"<td>{s['created_at']}</td>"
        f"<td><a href='/snapshot/{s['id']}' target=_blank>View</a></td>"
        f"<td>"
        f"<form method=post action=/admin style='display:inline'>"
        f"<input type=hidden name=action value=del_snapshot>"
        f"<input type=hidden name=id value={s['id']}>"
        f"<button style='background:#500;color:#f88;border:none;border-radius:3px;padding:2px 8px;cursor:pointer'>Del</button>"
        f"</form>"
        f"</td>"
        f"</tr>"
        for s in snapshots
    )

    lock_label = "Unlocked" if not LOCKED else "Locked"
    lock_color = "#4caf50" if not LOCKED else "#f44336"
    lock_btn = "Lock" if not LOCKED else "Unlock"
    lock_state = "1" if not LOCKED else "0"

    return web.Response(
        content_type="text/html",
        text=f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>r/place 2 – Admin</title>
<style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;padding:20px}}
h1,h2{{color:#fff}}
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
.section{{border:1px solid #333;border-radius:6px;padding:16px;margin-bottom:20px}}
.section h2{{margin-top:0}}
input[type=text]{{padding:6px 10px;background:#222;color:#eee;border:1px solid #555;border-radius:4px}}
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
  <div><div class="stat" style="color:{lock_color}">{lock_label}</div><div class="stat-label">Status</div></div>
</div>

<div class="section">
  <h2>Canvas Lock</h2>
  <form method=post action=/admin>
    <input type=hidden name=action value=lock>
    <input type=hidden name=state value={lock_state}>
    <button style="padding:8px 24px;background:{'#f44336' if not LOCKED else '#4caf50'};color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:16px">{lock_btn}</button>
  </form>
</div>

<div style="display:flex;gap:20px;flex-wrap:wrap">

<div class="section" style="flex:1;min-width:300px">
  <h2>Danger Zone</h2>
  <form method="post" action="/admin" onsubmit="return confirm('Clear entire canvas?')">
    <input type="hidden" name="action" value="clear">
    <button class="danger">Clear Canvas</button>
  </form>
</div>

<div class="section" style="flex:1;min-width:300px">
  <h2>Resize</h2>
  <form method="post" action="/admin">
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
</div>

<div class="section" style="flex:1;min-width:300px">
  <h2>Save Snapshot</h2>
  <form method=post action=/admin>
    <input type=hidden name=action value=snapshot>
    <div style="display:flex;gap:8px;align-items:center">
      <input type=text name=name placeholder="Snapshot name (optional)">
      <button style="padding:6px 16px;background:#333;color:#eee;border:1px solid #777;border-radius:4px;cursor:pointer">Save</button>
    </div>
  </form>
  <p style="color:#888;font-size:13px">Snapshots survive clear. Viewable at /snapshot/ID.</p>
</div>

</div>

<h2>Pixel Distribution</h2>
<table><tr><th>Color</th><th>Hex</th><th>Count</th><th>%</th></tr>{rows}</table>

<h2>Snapshots</h2>
<table>
<tr><th>Name</th><th>Size</th><th>Date</th><th></th><th></th></tr>
{snap_rows if snap_rows else '<tr><td colspan=5 style="color:#666">No snapshots yet</td></tr>'}
</table>

</body>
</html>""")


async def snapshot_viewer(request):
    try:
        sid = int(request.match_info.get("id", 0))
    except ValueError:
        raise web.HTTPNotFound()
    snap = await get_snapshot(sid)
    if not snap:
        raise web.HTTPNotFound()

    sw, sh = snap["width"], snap["height"]
    rle_data = rle_encode(snap["data"], sw, sh)

    import base64
    b64 = base64.b64encode(rle_data).decode()

    palette_json = "[" + ",".join(
        f'["#{r:02X}{g:02X}{b:02X}"]' for r, g, b in PALETTE_RGB
    ) + "]"

    name = snap["name"] or f"Snapshot #{sid}"

    return web.Response(
        content_type="text/html",
        text=f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} – r/place 2</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#111;color:#eee;font-family:system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;padding:20px;min-height:100vh}}
h1{{font-size:18px;margin-bottom:8px}}
.info{{color:#888;font-size:13px;margin-bottom:20px}}
#wrap{{overflow:auto;max-width:100%}}
canvas{{image-rendering:pixelated}}
.back{{color:#0083C7;text-decoration:none;margin-bottom:16px;display:inline-block}}
.back:hover{{text-decoration:underline}}
</style>
</head>
<body>
<a class=back href='/admin'>&larr; Back to Admin</a>
<h1>{name}</h1>
<div class=info>{sw}&times;{sh} &middot; Read-only snapshot</div>
<div id=wrap><canvas id=c></canvas></div>
<script>
const PALETTE = {palette_json};
const W={sw},H={sh};
const rle=Uint8Array.from(atob("{b64}"),c=>c.charCodeAt(0));
function decodeRLE(data){{const out=[];let off=0;while(off<data.length){{const n=data[off]+1,idx=data[off+1];for(let k=0;k<n;k++)out.push(idx);off+=2}}return new Uint8Array(out)}}
const pixels=decodeRLE(rle);
const el=document.getElementById("c");
el.width=W;el.height=H;
const ctx=el.getContext("2d");
const img=ctx.createImageData(W,H);
const d=img.data;
for(let i=0,j=0;i<pixels.length;i++,j+=4){{const[r,g,b]=PALETTE[pixels[i]];d[j]=parseInt(r.slice(1,3),16);d[j+1]=parseInt(g.slice(1,3),16);d[j+2]=parseInt(b.slice(1,3),16);d[j+3]=255}}
ctx.putImageData(img,0,0);
</script>
</body>
</html>"""
    )


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
        await ws.send_bytes(struct.pack(">BB", 3, 1 if LOCKED else 0))

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

                if LOCKED and not is_admin:
                    await ws.send_bytes(struct.pack(">BI", 2, 0))
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
                for c in list(WS_CLIENTS):
                    try:
                        await c.send_bytes(pixel_msg)
                    except Exception:
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
app.router.add_get("/snapshot/{id}", snapshot_viewer)
app.router.add_static("/", STATIC_DIR)
app.router.add_get("/ws", websocket_handler)

async def on_startup(app):
    await init_db()
    if backup_pool:
        asyncio.create_task(backup_loop())

app.on_startup.append(on_startup)

if __name__ == "__main__":
    print(f"r/place 2 running at http://localhost:{PORT}")
    web.run_app(app, port=PORT)
