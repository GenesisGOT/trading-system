# Multi-Agent AI Trading System
### TradingAgents × Vibe-Trading × Robinhood MCP · $0 to deploy and run

A cloud-hosted trading system where five AI agents debate stock picks, a Fund Manager makes the final call, and Vibe-Trading executes orders through Robinhood's official MCP OAuth integration — all running 24/7 for free.

```
┌─────────────────────────────────────────────────────────────┐
│  Every N minutes (default: 60)                              │
│                                                             │
│  For each symbol in MANDATE_ALLOWED_SYMBOLS:                │
│                                                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐      │
│  │Fundamental│ │Technical │ │Sentiment │ │  News    │      │
│  │ Analyst  │ │ Analyst  │ │ Analyst  │ │ Analyst  │      │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘      │
│       └────────────┴────────────┴─────────────┘            │
│                          │                                  │
│                   ┌──────▼──────┐                           │
│                   │Bull vs Bear │ (debate rounds)           │
│                   │  Research   │                           │
│                   │  Manager    │                           │
│                   └──────┬──────┘                           │
│                          │                                  │
│              ┌───────────▼───────────┐                      │
│              │  Risk Debate (3 roles)│                      │
│              │  Fund Manager decides │                      │
│              │  BUY / HOLD / SELL    │                      │
│              └───────────┬───────────┘                      │
│                          │                                  │
│              ┌───────────▼───────────┐                      │
│              │   Mandate Gate        │ (hard caps check)    │
│              │   Vibe-Trading SDK    │                      │
│              │   Robinhood MCP OAuth │                      │
│              └───────────┬───────────┘                      │
│                          │                                  │
│         SQLite audit + Discord/Telegram webhook             │
└─────────────────────────────────────────────────────────────┘
```

---

## $0 Cost Breakdown

| Component | Free Option | Limits |
|-----------|------------|--------|
| **Cloud server** | Oracle Cloud Always Free | 4 ARM vCPU · 24 GB RAM · forever |
| **LLM (brains)** | Groq free tier | 30 RPM · 14,400 req/day · Llama 3.3 70B |
| **Database** | SQLite (on-disk) | unlimited |
| **Notifications** | Discord webhooks | free |
| **Code hosting** | GitHub | free |
| **Total** | **$0/month** | |

> Groq's 30 RPM limit is 1,800 requests/hour. Analyzing 5 stocks every 60 minutes uses ~50–100 requests per cycle — well within limits.

---

## Prerequisites

- [ ] [Oracle Cloud account](https://cloud.oracle.com/free) (free, requires credit card for identity verification only — never charged)
- [ ] [Groq API key](https://console.groq.com) (free, no credit card)
- [ ] [Robinhood account](https://robinhood.com) with Agentic Trading enabled
- [ ] Robinhood MCP OAuth URL (from Robinhood's Agentic Trading onboarding)
- [ ] Discord server (optional, for trade notifications)

---

## Quick Deploy — Oracle Cloud Always Free

### Step 1 — Create a Free Oracle Cloud VM

1. Sign up at [cloud.oracle.com/free](https://cloud.oracle.com/free)
2. Go to **Compute → Instances → Create Instance**
3. Change shape: click **Edit** → **Ampere** → `VM.Standard.A1.Flex`
   - Set **OCPUs: 4**, **Memory: 24 GB** (maximum free allocation)
4. Under **Networking**, ensure a public IP is assigned
5. Download the SSH private key when prompted
6. Click **Create**
7. Once running, open port 8000 in the security list:
   - **Networking → Virtual Cloud Networks → your VCN → Security Lists → Ingress Rules**
   - Add: `TCP · Source 0.0.0.0/0 · Destination port 8000`

### Step 2 — SSH into the VM and install Docker

```bash
ssh -i ~/your-key.pem ubuntu@<YOUR_VM_PUBLIC_IP>

# Install Docker (one command)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Install Docker Compose
sudo apt-get install -y docker-compose-plugin
```

### Step 3 — Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/trading-system.git
cd trading-system

# Create your config
cp .env.example .env
nano .env
```

Fill in `.env` (minimum required fields):

```bash
GROQ_API_KEY=gsk_...                    # from console.groq.com (free)
ROBINHOOD_MCP_URL=https://...           # from Robinhood Agentic Trading
MANDATE_ALLOWED_SYMBOLS=AAPL,MSFT       # tickers you allow trading
MANDATE_MAX_ORDER_USD=100               # max $ per order
MANDATE_DAILY_CAP_USD=500               # max $ per day
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...   # optional
DRY_RUN=true                            # START WITH THIS — verify before live
```

### Step 4 — Deploy (one command)

```bash
docker compose up -d --build
```

That's it. The system is now running 24/7.

```bash
# Check it's alive
curl http://localhost:8000/health

# Watch logs
docker compose logs -f

# Check status
curl http://localhost:8000/status | python3 -m json.tool
```

---

## Go Live Checklist

Work through this in order before setting `DRY_RUN=false`:

- [ ] System running: `curl http://localhost:8000/health` returns `{"status":"ok"}`
- [ ] Groq key working: check docker logs for "TradingAgents analysis" lines with no errors
- [ ] Mandate looks right: `curl http://localhost:8000/mandate`
- [ ] Robinhood OAuth connected: check `curl http://localhost:8000/status` → `broker_status.sdk_available`
- [ ] Test notification: `curl -X POST "http://localhost:8000/notify/test"`
- [ ] Review a few decisions in dry-run: `curl http://localhost:8000/decisions`
- [ ] Kill switch works: `curl -X POST http://localhost:8000/halt -H 'Content-Type: application/json' -d '{"reason":"test"}'`
- [ ] Resume works: `curl -X POST http://localhost:8000/resume`
- [ ] **Set `DRY_RUN=false` in `.env`, then `docker compose up -d`**

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness check |
| `/status` | GET | Full system status, mandate config, broker state |
| `/halt` | POST | **Kill switch** — stops all trading immediately |
| `/resume` | POST | Re-enable trading after halt |
| `/trigger` | POST | Run the agent loop right now (outside the schedule) |
| `/decisions` | GET | Recent agent decisions from audit ledger |
| `/executions` | GET | Recent trade execution records |
| `/mandate` | GET | Current mandate limits and daily usage |
| `/notify/test` | POST | Send a test notification to Discord/Telegram |

### Kill switch

```bash
curl -X POST http://YOUR_IP:8000/halt \
  -H "Content-Type: application/json" \
  -d '{"reason": "market volatility — halting manually"}'
```

This sets both an in-process flag (stops the scheduler loop) **and** Vibe-Trading's filesystem halt sentinel, so the broker layer is doubly blocked.

---

## Mandate Configuration

The mandate is your safety boundary — orders that violate any limit are **blocked before reaching Robinhood**:

| Variable | Description | Default |
|----------|-------------|---------|
| `MANDATE_ALLOWED_SYMBOLS` | Comma-separated tickers allowed to trade | `AAPL,MSFT,GOOGL` |
| `MANDATE_MAX_ORDER_USD` | Max notional per single order | `500` |
| `MANDATE_DAILY_CAP_USD` | Max total USD submitted per calendar day | `2000` |
| `MANDATE_MAX_EXPOSURE_USD` | Max total open position value | `10000` |
| `MANDATE_MAX_TRADES_PER_DAY` | Max number of orders per day | `5` |

---

## Agent Architecture

### TradingAgents (the brain)
- **Fundamental Analyst** — income statement, balance sheet, cashflow ratios
- **Technical Analyst** — SMA, EMA, MACD, RSI, Bollinger Bands, ATR
- **Sentiment Analyst** — StockTwits + Reddit aggregation, scored 0–10
- **News Analyst** — macro and company-specific news trends
- **Bull/Bear Researchers** — structured investment debate (configurable rounds)
- **Research Manager** — judge: Buy | Overweight | Hold | Underweight | Sell
- **Risk Debators** (3 roles) — position sizing and risk analysis
- **Fund Manager (Portfolio Manager)** — final decision, price target, thesis

### Vibe-Trading (the executor)
- Robinhood MCP OAuth integration — credentials cached at boot, never agent-accessible
- 6-stage mandate enforcement gate (symbol · instrument type · order size · exposure · leverage · daily count)
- Filesystem kill switch — `live/HALT` sentinel, untrippable by the LLM
- Full audit trail written before and after each order

### Decision mapping

| TradingAgents Rating | Action | Order |
|---------------------|--------|-------|
| Buy | BUY | Market buy, notional = `MANDATE_MAX_ORDER_USD` |
| Overweight | BUY | Same |
| Hold | HOLD | No order |
| Underweight | SELL | Market sell, up to `MANDATE_MAX_ORDER_USD` |
| Sell | SELL | Same |

---

## Notifications

**Discord** — add a webhook to any channel:
1. Channel Settings → Integrations → Webhooks → New Webhook → Copy URL
2. Set `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`

**Telegram** — create a bot:
```
1. Message @BotFather → /newbot → follow prompts → copy the token
2. Message @userinfobot → copy your chat_id
3. Set TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_ID=...
```

Every trade (submitted, blocked, or error) sends a push notification.

---

## Alternative Free LLM Providers

If you hit Groq rate limits (unlikely at hourly intervals):

```bash
# OpenRouter — 28+ free models, no credit card
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
DEEP_THINK_LLM=meta-llama/llama-3.3-70b-instruct:free
QUICK_THINK_LLM=meta-llama/llama-3.1-8b-instruct:free

# Google Gemini Flash — also free (10 RPM)
LLM_PROVIDER=google
GOOGLE_API_KEY=...       # aistudio.google.com — free
DEEP_THINK_LLM=gemini-1.5-flash
QUICK_THINK_LLM=gemini-1.5-flash-8b
```

---

## Local Development

```bash
# Python 3.11+
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt
pip install git+https://github.com/TauricResearch/TradingAgents.git
pip install git+https://github.com/HKUDS/Vibe-Trading.git

cp .env.example .env
# edit .env

uvicorn app.main:app --reload
```

The app starts, initializes SQLite, and the scheduler fires its first analysis after `LOOP_INTERVAL_MINUTES`.

---

## Viewing the Audit Ledger

The SQLite database is at `/data/trading_audit.db` inside the container:

```bash
# Copy it out
docker compose cp trading-brain:/data/trading_audit.db ./audit.db

# Query it
sqlite3 audit.db "SELECT ticker, rating, action, confidence, created_at FROM agent_decisions ORDER BY id DESC LIMIT 20;"
sqlite3 audit.db "SELECT ticker, side, status, notional_usd, order_id FROM trade_executions ORDER BY id DESC LIMIT 20;"
```

---

## Updating

```bash
cd trading-system
git pull
docker compose up -d --build
```

---

## Project Structure

```
trading-system/
├── app/
│   ├── main.py              # FastAPI app, all routes, lifespan
│   ├── config.py            # All settings from environment variables
│   ├── scheduler.py         # APScheduler background worker + halt/resume
│   ├── database.py          # SQLite audit ledger (decisions + executions)
│   ├── notifications.py     # Discord + Telegram webhooks
│   ├── agents/
│   │   └── runner.py        # TradingAgents wrapper → AgentResult
│   └── broker/
│       └── connector.py     # Vibe-Trading SDK wrapper → OrderResult
├── Dockerfile               # Single image: TradingAgents + Vibe-Trading + our app
├── docker-compose.yml       # One-command deploy
├── requirements.txt
├── .env.example             # All config vars documented
└── README.md
```

---

## Disclaimer

This system executes real trades with real money. Always:
- Start with `DRY_RUN=true` and verify decisions look sane before going live
- Set conservative mandate limits (`MANDATE_MAX_ORDER_USD`, `MANDATE_DAILY_CAP_USD`)
- Keep the kill switch URL bookmarked: `POST /halt`
- Past LLM analysis does not guarantee future returns
- You are responsible for all trading activity in your Robinhood account
