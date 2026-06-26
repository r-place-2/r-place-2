# r/place 2

Collaborative pixel canvas inspired by Reddit's r/place.

## Features

- Real-time multiplayer pixel canvas
- 500×500 canvas (expandable via admin panel)
- 17 colors including black
- WebSocket-based binary protocol (efficient)
- Zoom & Pan (mouse wheel + shift-click)
- 5-minute cooldown per user
- Admin panel at `/admin` (password: `Ben2013`)
  - No cooldown for admins
  - Canvas resize (up/down/left/right)
  - Clear canvas
  - Statistics
- Palette-indexed storage (1 byte/pixel, very memory efficient)

## Local Start

```bash
pip install aiohttp websockets
python3 server.py
```

Open http://localhost:3000

## Admin

Visit r-place-2.github.io and enter the password .

Once logged in, place pixels without cooldown and access the admin panel.

## Tunnel (for GitHub Pages)

To expose the server publicly with WebSocket support:

```bash
# Install bore
curl -sL https://github.com/ekzhang/bore/releases/download/v0.5.2/bore-v0.5.2-x86_64-unknown-linux-musl.tar.gz | tar xz

# Start server + tunnel
./bore local 3000 --to bore.pub
```

This gives you a public URL like `bore.pub:12345`. Update the WebSocket URL in the frontend to point to this address.

### !IMPORTANT!

### Since the 26/06/2026 or 06/26/2026 This method is not used anymore and is instead running on a Render Free Plan

## Protocol

Binary WebSocket protocol:

- `0x00` = INIT: canvas data (RLE-compressed palette indices)
- `0x01` = PIXEL: single pixel placement (8 bytes)
- `0x02` = COOLDOWN: remaining cooldown time
- `0x10` = IDENTIFY: client identification
