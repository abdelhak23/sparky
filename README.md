# 🔥 Spark — Backend Setup Guide

Full-stack stranger chat platform with:
- **Flask** REST API
- **SQLite** database (via SQLAlchemy)
- **JWT** authentication
- **Socket.IO** real-time chat & matchmaking
- **Stripe** token purchases

---

## 📁 Project Structure

```
spark_backend/
├── app.py                  # Flask app factory + extension init
├── models.py               # SQLAlchemy models (User, ChatSession, ChatMessage, TokenPurchase, Gift, Report)
├── run.py                  # Dev server entry point
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
│
├── routes/
│   ├── auth.py             # POST /api/auth/register|login  GET /api/auth/me
│   ├── payments.py         # Stripe checkout, webhooks, gift sending
│   ├── chat.py             # Session start/end, message history
│   └── users.py            # Profile, stats, reports
│
├── sockets/
│   └── events.py           # Socket.IO: matchmaking, real-time chat, gifts
│
└── static/                 # Frontend HTML + JS (served by Flask)
    ├── strangerdate.html
    ├── login.html
    ├── videochat.html
    ├── voicechat.html
    ├── payment-success.html
    └── spark-api.js         # Shared API client (auth, fetch wrapper, socket)
```

---

## ⚡ Quick Start

### 1. Install dependencies

```bash
cd spark_backend
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your values (see below)
```

### 3. Run the server

```bash
python run.py
```

Open **http://localhost:5000** in your browser.

---

## 🔑 Environment Variables (`.env`)

| Variable | Description | Required |
|---|---|---|
| `SECRET_KEY` | Flask session secret | ✅ |
| `JWT_SECRET_KEY` | JWT signing secret | ✅ |
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_...`) | For payments |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key (`pk_test_...`) | For payments |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret | For webhooks |
| `DATABASE_URL` | SQLite (default) or PostgreSQL URI | Optional |
| `FRONTEND_URL` | Base URL for Stripe redirect (`http://localhost:5000`) | For payments |

Generate secret keys:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## 🗃️ Database

SQLite database is auto-created at `instance/spark.db` on first run.

**Tables:**
- `users` — accounts, token balances, premium status
- `chat_sessions` — each stranger connection
- `chat_messages` — persisted chat messages
- `token_purchases` — Stripe purchase records
- `gifts` — virtual gifts with USD value
- `reports` — user reports for moderation

To reset the database:
```bash
rm instance/spark.db && python run.py
```

---

## 🌐 REST API Reference

### Auth
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| POST | `/api/auth/register` | ❌ | Register new account |
| POST | `/api/auth/login` | ❌ | Login, get JWT |
| GET | `/api/auth/me` | ✅ | Get current user profile |
| PATCH | `/api/auth/me` | ✅ | Update name |
| POST | `/api/auth/change-password` | ✅ | Change password |
| GET | `/api/auth/tokens` | ✅ | Get token balance |

### Payments
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/api/payments/packs` | ❌ | List token packs |
| POST | `/api/payments/create-checkout` | ✅ | Create Stripe checkout session |
| POST | `/api/payments/webhook` | ❌ | Stripe webhook handler |
| POST | `/api/payments/deduct` | ✅ | Deduct tokens |
| POST | `/api/payments/send-gift` | ✅ | Send a virtual gift |
| GET | `/api/payments/gifts` | ❌ | Gift catalog |
| GET | `/api/payments/history` | ✅ | Purchase history |
| POST | `/api/payments/grant-tokens` | ✅ | Dev: grant free tokens |

### Chat
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| POST | `/api/chat/session/start` | ✅ | Start a chat session |
| POST | `/api/chat/session/:id/end` | ✅ | End a session |
| POST | `/api/chat/session/:id/message` | ✅ | Save a message |
| GET | `/api/chat/session/:id/messages` | ✅ | Get message history |
| GET | `/api/chat/sessions` | ✅ | List my sessions |

### Users
| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/api/users/:id` | ✅ | Public profile |
| GET | `/api/users/me/stats` | ✅ | My stats |
| POST | `/api/users/report` | ✅ | Submit a report |
| GET | `/api/users/me/reports` | ✅ | My submitted reports |

---

## ⚡ Socket.IO Events

Connect with JWT token:
```js
const socket = io({ query: { token: 'your-jwt-token' } });
```

### Client → Server
| Event | Payload | Description |
|---|---|---|
| `find_match` | `{ type: "video"\|"audio", filters: {} }` | Join matchmaking queue |
| `cancel_search` | — | Leave queue |
| `send_message` | `{ content, type, session_id }` | Send chat message |
| `send_reaction` | `{ reaction }` | Send a reaction |
| `send_gift` | `{ gift_type, session_id }` | Send a virtual gift |
| `skip` | — | Skip current partner |
| `leave_room` | — | Leave current room |
| `webrtc_offer` | SDP offer | WebRTC signalling |
| `webrtc_answer` | SDP answer | WebRTC signalling |
| `webrtc_ice_candidate` | ICE candidate | WebRTC signalling |

### Server → Client
| Event | Description |
|---|---|
| `connected` | Auth confirmation + token balance |
| `searching` | Added to queue |
| `match_found` | Match found, room_id + partner info |
| `search_cancelled` | Queue left |
| `new_message` | Incoming chat message |
| `reaction_received` | Incoming reaction |
| `gift_received` | Incoming gift |
| `tokens_updated` | New token balance |
| `partner_disconnected` | Partner left/disconnected |
| `error` | Error message |

---

## 💳 Stripe Setup

1. Create account at [stripe.com](https://stripe.com)
2. Get test keys from **Dashboard → Developers → API Keys**
3. Add to `.env`:
   ```
   STRIPE_SECRET_KEY=sk_test_...
   STRIPE_PUBLISHABLE_KEY=pk_test_...
   ```
4. For webhooks (local testing), use [Stripe CLI](https://stripe.com/docs/stripe-cli):
   ```bash
   stripe listen --forward-to localhost:5000/api/payments/webhook
   ```
   Copy the webhook secret into `.env` as `STRIPE_WEBHOOK_SECRET`

**Test card:** `4242 4242 4242 4242` — any future expiry, any CVC

---

## 🏭 Production Deployment

1. Set `FLASK_ENV=production` in `.env`
2. Use PostgreSQL: `DATABASE_URL=postgresql://user:pass@host/spark`
3. Use **Gunicorn + eventlet**:
   ```bash
   gunicorn --worker-class eventlet -w 1 "app:create_app()"
   ```
4. Put **Nginx** in front for SSL
5. Use **Redis** for Socket.IO message queue in multi-worker setups:
   ```python
   socketio = SocketIO(message_queue='redis://')
   ```
6. Remove or protect the `/api/payments/grant-tokens` endpoint

---

## 🧪 Testing the API (curl examples)

```bash
# Register
curl -X POST http://localhost:5000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"first_name":"Alex","email":"alex@test.com","password":"password123"}'

# Login
curl -X POST http://localhost:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"alex@test.com","password":"password123"}'

# Get profile (replace TOKEN)
curl http://localhost:5000/api/auth/me \
  -H "Authorization: Bearer TOKEN"

# Grant free tokens (dev only)
curl -X POST http://localhost:5000/api/payments/grant-tokens \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount": 100}'
```

---

## 📹 Cross-Device Video / Audio (TURN Server)

WebRTC works fine on the **same network** using STUN only. For calls between **different devices on different networks** (e.g. phone ↔ laptop on different Wi-Fi/4G) you need a **TURN relay server**.

### Using a TURN server
To support calls between devices on different networks, you will need a TURN server.
You can get your own TURN server (from providers like Twilio or Xirsys) and update your `.env`:

```
TURN_URL=your-turn-host
TURN_USER=your_username
TURN_PASS=your_credential
```
The backend serves these to the frontend at `GET /api/users/turn-credentials` — no frontend changes needed.

### Why video shows name but no camera
This is the classic STUN-only symptom:
- STUN discovers your public IP but can't relay media when both peers are behind **symmetric NAT** (most home routers and mobile networks)
- TURN relays all media through a server, bypassing NAT — this is what fixes cross-device calls
- The app now fetches fresh TURN credentials before every call and handles ICE restart automatically if the connection drops
