# VoiceVid 🎙️📹

A real-time voice & video calling web app built with Flask, Flask-SocketIO, and WebRTC.

---

## ▶ Run Locally

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. (Optional) Create a `.env` file
```bash
cp .env.example .env
# Edit .env as needed — defaults work fine for local dev
```

### 3. Start the server
```bash
python app.py
```

Visit **http://localhost:5000**

Demo accounts: `alice / demo1234` and `bob / demo1234`

---

## 🚀 Deploy to Render

### Option A — render.yaml (recommended)

1. Push this project to a GitHub repository.
2. In the [Render dashboard](https://dashboard.render.com/), click **New → Blueprint**.
3. Connect your GitHub repo — Render will read `render.yaml` automatically.
4. Click **Apply**. Render will build and deploy.

### Option B — Manual web service

1. New → **Web Service** → connect your repo.
2. **Runtime:** Python 3
3. **Build command:**
   ```
   pip install -r requirements.txt
   ```
4. **Start command:**
   ```
   gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app
   ```
5. **Environment variables** (set in Render dashboard):

| Variable | Value |
|---|---|
| `SECRET_KEY` | Any long random string (use "Generate" in Render) |
| `RENDER` | `1` |
| `DATABASE_URL` | PostgreSQL URL from a Render DB (optional, falls back to SQLite) |
| `TURN_URL` | TURN server URL (optional, see below) |
| `TURN_USERNAME` | TURN credential username (optional) |
| `TURN_CREDENTIAL` | TURN credential password (optional) |

---

## 🔧 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | **Yes (prod)** | Flask session secret. Use a long random string in production. |
| `RENDER` | No | Set to `1` on Render to enable secure cookies & disable debug mode. |
| `DATABASE_URL` | No | PostgreSQL connection string. Defaults to SQLite locally. |
| `PORT` | No | Server port. Set automatically by Render. Default: `5000`. |
| `TURN_URL` | No | TURN server URL for WebRTC NAT traversal (e.g. `turn:turn.example.com:3478`). |
| `TURN_USERNAME` | No | TURN server username. |
| `TURN_CREDENTIAL` | No | TURN server password. |

---

## 🛰 WebRTC & TURN servers

For local-to-local calls on the same network, STUN is enough.  
For calls between **different networks** (e.g. two users on Render's public URL), a **TURN server** is strongly recommended.

Free options:
- [Metered TURN](https://www.metered.ca/tools/openrelay/) — free tier available
- [Cloudflare Calls](https://developers.cloudflare.com/calls/) — generous free tier
- Self-hosted: [coturn](https://github.com/coturn/coturn)

---

## 📦 Architecture notes

- **Async mode:** automatically detects `eventlet` → `gevent` → `threading`.  
  Render deployments use `eventlet` (listed in `requirements.txt`).
- **Database:** SQLite for local dev, PostgreSQL on Render (set `DATABASE_URL`).
- **SocketIO:** ping timeout/interval tuned for Render's 30 s idle proxy timeout.
