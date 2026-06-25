const COLORS = [
  "#FFFFFF","#E4E4E4","#888888","#222222",
  "#FFA7D1","#E50000","#E59500","#A06A42",
  "#E5D900","#94E044","#02BE01","#00D3DD",
  "#0083C7","#0000EA","#CF6EE4","#820080",
  "#000000",
];

const COLORS_RGB = COLORS.map(hex => [
  parseInt(hex.slice(1,3), 16),
  parseInt(hex.slice(3,5), 16),
  parseInt(hex.slice(5,7), 16),
]);

const canvasEl = document.getElementById("canvas");
const ctx = canvasEl.getContext("2d");
const statusEl = document.getElementById("status");
const cooldownEl = document.getElementById("cooldown");
const coordEl = document.getElementById("coord");

let W, H;
let pixels;       // Uint8Array of palette indices (W * H)
let scale = 1;
let offsetX = 0, offsetY = 0;
let isPanning = false;
let panStart = null;
let selectedIdx = 0;
let cooldownUntil = 0;
let pendingPixel = null;
let needsRedraw = false;

const CLIENT_ID_KEY = "rplace2_client_id";
let clientId = localStorage.getItem(CLIENT_ID_KEY);
if (!clientId) {
  const buf = new Uint8Array(8);
  crypto.getRandomValues(buf);
  clientId = Array.from(buf, b => b.toString(16).padStart(2, "0")).join("");
  localStorage.setItem(CLIENT_ID_KEY, clientId);
}

function buildPalette() {
  const palette = document.getElementById("palette");
  COLORS.forEach((c, i) => {
    const btn = document.createElement("button");
    btn.style.background = c;
    btn.dataset.idx = i;
    btn.addEventListener("click", () => { selectedIdx = i; highlightPalette(); });
    palette.appendChild(btn);
  });
  highlightPalette();
}

function highlightPalette() {
  document.querySelectorAll("#palette button").forEach((b, i) => {
    b.classList.toggle("active", i === selectedIdx);
  });
}

function setCanvasSize() {
  canvasEl.style.width = (W * scale) + "px";
  canvasEl.style.height = (H * scale) + "px";
}

function render() {
  if (!W || !H || !pixels) return;
  canvasEl.width = W;
  canvasEl.height = H;
  const imageData = ctx.createImageData(W, H);
  const out = imageData.data;
  for (let i = 0, j = 0; i < pixels.length; i++, j += 4) {
    const [r, g, b] = COLORS_RGB[pixels[i]];
    out[j] = r;
    out[j+1] = g;
    out[j+2] = b;
    out[j+3] = 255;
  }
  ctx.putImageData(imageData, 0, 0);
}

function requestRender() {
  if (needsRedraw) return;
  needsRedraw = true;
  requestAnimationFrame(() => {
    needsRedraw = false;
    render();
  });
}

function decodeRLE(data) {
  const out = [];
  let off = 0;
  while (off < data.length) {
    const count = data[off] + 1;
    const idx = data[off+1];
    for (let k = 0; k < count; k++) {
      out.push(idx);
    }
    off += 2;
  }
  return new Uint8Array(out);
}

function worldToCanvas(clientX, clientY) {
  const rect = canvasEl.getBoundingClientRect();
  return {
    x: Math.floor((clientX - rect.left) / scale),
    y: Math.floor((clientY - rect.top) / scale),
  };
}

function placePacket(x, y, idx) {
  return new Uint8Array([
    1,
    (x >> 16) & 0xFF, (x >> 8) & 0xFF, x & 0xFF,
    (y >> 16) & 0xFF, (y >> 8) & 0xFF, y & 0xFF,
    idx,
  ]);
}

function identifyPacket() {
  const buf = new Uint8Array(9);
  buf[0] = 0x10;
  for (let i = 0; i < 8; i++) {
    buf[i + 1] = parseInt(clientId.slice(i * 2, i * 2 + 2), 16);
  }
  return buf;
}

canvasEl.addEventListener("mousedown", (e) => {
  if (e.button === 1 || e.shiftKey) {
    isPanning = true;
    panStart = { x: e.clientX - offsetX, y: e.clientY - offsetY };
    canvasEl.style.cursor = "grabbing";
    return;
  }
  if (e.button !== 0) return;

  const { x, y } = worldToCanvas(e.clientX, e.clientY);

  if (x < 0 || x >= W || y < 0 || y >= H) return;
  if (!pixels) return;
  if (Date.now() < cooldownUntil) return;

  const pos = y * W + x;
  pendingPixel = { x, y, idx: pixels[pos] };
  pixels[pos] = selectedIdx;
  requestRender();
  ws.send(placePacket(x, y, selectedIdx));
});

canvasEl.addEventListener("mousemove", (e) => {
  const { x, y } = worldToCanvas(e.clientX, e.clientY);
  coordEl.textContent = x >= 0 && x < W && y >= 0 && y < H ? `${x}, ${y}` : "";
  if (isPanning) {
    offsetX = e.clientX - panStart.x;
    offsetY = e.clientY - panStart.y;
    canvasEl.style.transform = `translate(${offsetX}px, ${offsetY}px)`;
  }
});

window.addEventListener("mouseup", () => {
  if (isPanning) { isPanning = false; canvasEl.style.cursor = "crosshair"; }
});

canvasEl.addEventListener("wheel", (e) => {
  e.preventDefault();
  scale = Math.max(0.5, Math.min(20, scale + (e.deltaY > 0 ? -0.1 : 0.1)));
  setCanvasSize();
});

document.getElementById("zoom-in").addEventListener("click", () => {
  scale = Math.min(20, scale + 0.5);
  setCanvasSize();
});
document.getElementById("zoom-out").addEventListener("click", () => {
  scale = Math.max(0.5, scale - 0.5);
  setCanvasSize();
});
document.getElementById("reset-view").addEventListener("click", () => {
  scale = 1; offsetX = 0; offsetY = 0;
  canvasEl.style.transform = "";
  setCanvasSize();
});

const params = new URLSearchParams(location.search);
const backend = params.get("backend") || location.host;
const protocol = location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = params.get("ws") || `${protocol}//${backend}/ws`;
const ws = new WebSocket(wsUrl);
ws.binaryType = "arraybuffer";

ws.onopen = () => {
  statusEl.textContent = "Online";
  statusEl.className = "online";
  ws.send(identifyPacket());
};

ws.onclose = () => {
  statusEl.textContent = "Offline";
  statusEl.className = "offline";
};

ws.onmessage = (e) => {
  const buf = new Uint8Array(e.data);
  const type = buf[0];

  if (type === 0) {
    W = (buf[1] << 8) | buf[2];
    H = (buf[3] << 8) | buf[4];
    pixels = decodeRLE(buf.subarray(5));
    setCanvasSize();
    requestRender();
    return;
  }

  if (type === 1) {
    const x = (buf[1] << 16) | (buf[2] << 8) | buf[3];
    const y = (buf[4] << 16) | (buf[5] << 8) | buf[6];
    const idx = buf[7];
    const pos = y * W + x;
    pixels[pos] = idx;

    if (pendingPixel && x === pendingPixel.x && y === pendingPixel.y) {
      pendingPixel = null;
    }
    requestRender();
    return;
  }

  if (type === 2) {
    if (pendingPixel) {
      const pos = pendingPixel.y * W + pendingPixel.x;
      pixels[pos] = pendingPixel.idx;
      pendingPixel = null;
      requestRender();
    }
    const ms = (buf[1] << 24) | (buf[2] << 16) | (buf[3] << 8) | buf[4];
    cooldownUntil = Date.now() + ms;
    cooldownEl.textContent = "";
    tickCooldown();
  }
};

function tickCooldown() {
  const remaining = cooldownUntil - Date.now();
  if (remaining <= 0) { cooldownEl.textContent = ""; return; }
  const sec = Math.ceil(remaining / 1000);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  cooldownEl.textContent = `Cooldown: ${m}:${s.toString().padStart(2, "0")}`;
  setTimeout(tickCooldown, 1000);
}

buildPalette();
