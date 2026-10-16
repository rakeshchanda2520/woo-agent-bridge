# woo-agent-bridge

**A read-only MCP connector that lets a Razorpay Agent Studio agent answer order, payment and stock questions from a merchant's WooCommerce store, with no ability to change anything.**

> **Assignment:** Build a private connector for a merchant tool.**
> **Tool chosen:** WooCommerce, which many Razorpay merchants already use through Razorpay's official `woo-razorpay` checkout plugin.

---

## TL;DR for reviewers

| | |
|---|---|
| **What I built** | An MCP server with **8 read-only tools** (list / get / search for orders and products, plus two "smart" tools) built around the three questions that fill a D2C merchant's support inbox: *Where is my order? Money was debited but my order failed. Is this in stock in my size?* |
| **Why it matters for Razorpay** | The headline tool, `get_order_payment_context`, reads what the Razorpay plugin writes on each order. It flags **"paid, but the order shows failed"** (usually a missed webhook) as `mismatch` and tells the agent to escalate rather than promise a refund. |
| **How you can verify it** | One Docker command gives you a **real WooCommerce 11.2 store** with fake data, HTTPS and a real rate limiter. No accounts, keys or mocks needed. [Setup and run guide ↓](#setup-and-run-guide) |
| **Evidence** | 59 offline tests + 4 live tests pass · end-to-end demo through a real MCP client · load test against real HTTP 429s: **40/40 succeed with 0 rate-limit hits** (vs ~100 without client-side pacing) |
| **Safety** | No write tools, Read-only key, HTTPS enforced, PII masked, prompt-injection text fenced, inputs validated before any request, no secrets in the repo (CI scan) |
| **Honest limits** | Fake data, local store; Agent Studio itself not available to test against. [Full list ↓](#limitations-and-what-id-do-next) |

### Six things I verified rather than assumed

| # | Finding | How I verified it | What I did about it |
|---|---|---|---|
| 1 | **The Razorpay plugin stores the payment id in the order's `transaction_id`** and the Razorpay order id in meta `razorpay_order_id<wc_order_id>` | Read the [plugin source](https://github.com/razorpay/razorpay-woocommerce) (`woo-razorpay.php`) | Payment verdicts are built on exactly these fields; the seeder writes orders the same way |
| 2 | **Over plain HTTP, WooCommerce ignores Basic auth**, so valid keys get `401`. Another public WooCommerce submission hit exactly this | Live store + WooCommerce behaviour | HTTPS demo store; the connector refuses plain HTTP by default and its 401 message names the cause |
| 3 | **WooCommerce's `search` is fuzzy and can't find payment ids.** `search=19` matched *every* order (through `+919…` phone numbers), and `transaction_id` isn't searched | Probed the live API | Every filter is re-checked exactly on our side; payment-id lookup does a bounded scan and reports `scan_complete` |
| 4 | **WooCommerce's "click Approve" key flow (`wc-auth`) can't run locally**: it requires an `https://` callback and posts via `wp_safe_remote_post`, which blocks localhost | Read `class-wc-auth.php` (lines 200, 279) | Paste-a-Read-key flow + `woo-bridge check`; `wc-auth` documented as the production path for Agent Studio |
| 5 | **Raw order JSON contains fields an agent must never see**: `order_key`, and a `payment_url` that lets anyone pay or view the order | Inspected live responses | Stripped from every response; a test asserts they never appear |
| 6 | **Client-side pacing costs nothing in speed but saves the merchant's server ~3.5× load** | `scripts/load_test.py` against nginx's real 429s | Pacing is on by default |

---

## Assignment requirements → where each is met

| Requirement (from the assignment form) | Where | Proof |
|---|---|---|
| Connector lets an Agent Studio agent **read** orders / inventory | 8 MCP tools, all `readOnlyHint: true` | [Tool reference](#tool-reference) |
| Working **OAuth or API-key** authentication flow | WooCommerce Read-only key over HTTPS; `woo-bridge check` proves it | [Authentication](#1-authentication-api-key-over-https-only) · `uv run woo-bridge check` |
| **list / get / search** primitives | `list_orders` · `get_order` · `search_orders` · `list_products` · `get_product` · `search_products` | `tests/test_tools.py`, `tests/test_live.py` |
| **Handle rate limits** | Token bucket + `Retry-After` + jittered backoff + structured `RATE_LIMITED` error | [Rate limits](#2-rate-limits-polite-by-default-resilient-when-it-isnt) · `scripts/load_test.py` |
| **MCP tool specification** | [`docs/tool_spec.json`](docs/tool_spec.json), generated from code; a test fails if it drifts | `uv run woo-bridge spec` |
| Short doc: what the agent **can and cannot** do | [`docs/AGENT_CAPABILITIES.md`](docs/AGENT_CAPABILITIES.md) | — |
| Setup / run instructions, assumptions, limitations | This README + [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | — |
| No secrets, API keys or real customer data | All data fake; keys generated locally and git-ignored; CI secret scan | `.github/workflows/ci.yml` |

---

## The merchant problem

*Chai Chronicles Co.* (fictional) is a D2C tea brand on WooCommerce with Razorpay checkout and a 3-person support team. The tickets are dominated by three questions:

| Customer asks | A human today | With this connector, the agent calls | Agent gets |
|---|---|---|---|
| "Where is my order?" | Searches wp-admin | `search_orders(email)` → `get_order` | status, items, timeline |
| "Money debited, order **failed**!" | wp-admin + Razorpay dashboard | `get_order_payment_context` | `mismatch` + evidence + "escalate" |
| "Tulsi tea in 500g?" | Opens the product, checks each size | `get_stock_status(sku)` | `varies`: 250g in stock, 500g out |

The money question is the expensive one: it's urgent, and a wrong answer means a double refund or a lost customer. So the connector gives the agent **evidence and a verdict, never the power to act**. [How I'd measure impact →](docs/SCENARIOS.md#measurement-plan)

---

## How it works

```mermaid
flowchart LR
    C(["👤 Customer"]) -->|"My payment went through<br/>but the order failed!"| A["🤖 Agent<br/>(Agent Studio)"]
    A -->|"MCP: get_order_payment_context(19)"| B["🔌 woo-agent-bridge"]
    B -->|"HTTPS · Read-only key<br/>≤ 4 req/s · retries on 429"| W[("🛒 WooCommerce<br/>+ Razorpay plugin data")]
    W -->|raw order JSON| B
    B -->|"verdict: mismatch<br/>reasons · next step"| A
    A -->|"I can see your payment. A teammate will<br/>confirm and fix the order today."| C
```

| Step | Module | Responsibility |
|---|---|---|
| 1 | `server.py` | Defines the 8 tools. **Validates input** (types, enums, regex, bounds) so junk never reaches the store |
| 2 | `client.py` | The **only** module doing HTTP: HTTPS auth, token-bucket pacing, concurrency cap, retries, pagination, error → code mapping |
| 3 | `verdicts.py` | **Pure business rules** ("is this paid?", "is this sellable?"): dicts in, verdicts out, no I/O |
| 4 | `models.py` | Projects raw WooCommerce JSON into **slim, safe** shapes: strips private fields, masks PII, fences shopper text |
| 5 | `guards.py` | Rate limiter, masking, untrusted-text fence, log redaction |

Dependencies point one way (`server` → `verdicts` / `models` → `client`), so the business rules are tested with plain dictionaries and no mocking.

<details>
<summary><b>Sequence of one call, including a 429 retry</b></summary>

```mermaid
sequenceDiagram
    participant Agent
    participant Server as server.py
    participant Client as client.py
    participant Store as WooCommerce (nginx)
    Agent->>Server: get_order_payment_context(order_id=19)
    Server->>Server: validate input
    Server->>Client: GET /orders/19
    Client->>Client: wait for rate-limit token + concurrency slot
    Client->>Store: HTTPS (Basic auth, Read-only key)
    alt 429 / 5xx / network error
        Store-->>Client: 429, Retry-After: 1
        Client->>Client: wait Retry-After (or jittered backoff), retry ≤ 4
    end
    Store-->>Client: order JSON
    Client-->>Server: dict
    Server->>Server: verdicts.payment_context() → "mismatch"<br/>models: strip + mask + fence
    Server-->>Agent: { verdict, reasons, suggested_next_step, advisory, as_of }
```
</details>

### What the agent actually receives

```json
{
  "order_id": 19,
  "verdict": "mismatch",
  "order_status": "failed",
  "razorpay_payment_id": "pay_SEEDS2000000002",
  "razorpay_order_id": "order_SEEDS2000000002",
  "reasons": [
    "Order status is 'failed'.",
    "Razorpay payment id is pay_SEEDS2000000002 recorded on the order.",
    "A payment id exists but the order did not move to a paid status (often a missed webhook or a late capture)."
  ],
  "suggested_next_step": "Escalate to a human: verify pay_SEEDS2000000002 in the Razorpay dashboard, then either mark the order paid or initiate a refund. Do not promise either yet.",
  "advisory": "Derived from WooCommerce records only. Razorpay is the source of truth for money movement; ...",
  "as_of": "2026-10-16T11:03:08+00:00"
}
```

---

## Key design decisions

### 1. Authentication: API key over HTTPS only

- The merchant creates a WooCommerce key with **Read** permission, so even a leaked key can't change data.
- Over HTTPS the connector uses HTTP Basic auth. **Plain HTTP is refused** (`INSECURE_TRANSPORT`), because WooCommerce ignores Basic auth there (finding #2) and the only alternative is putting keys in the URL. `WOO_ALLOW_INSECURE_HTTP=true` exists for local experiments only.
- `woo-bridge check` proves the whole flow: transport rule, key validity, read access to orders and products.
- **Production path:** WooCommerce's `wc-auth` consent screen (merchant clicks Approve, scope `read`) behind Agent Studio's public HTTPS callback, with keys stored in a per-merchant vault (finding #4).

### 2. Rate limits: polite by default, resilient when it isn't

WooCommerce has no built-in limiter; merchants get one from hosting, a WAF or Cloudflare. The demo puts **nginx `limit_req` (5 req/s, burst 5)** in front of the store, so 429s are real.

- **Prevent:** a token bucket (4 req/s, burst 4) and at most 4 concurrent requests.
- **Recover:** retry on 429/502/503/504 and network errors. Honour `Retry-After` (seconds or HTTP-date), otherwise full-jitter exponential backoff, at most 4 retries. A 429 slows **all** queued calls, not just the failing one.
- **Degrade cleanly:** if the store never recovers, the agent gets `{"code": "RATE_LIMITED", "retryable": true, "retry_after_s": …}`, not a stack trace. Client errors (401/403/404/400) fail fast and are never retried.

`scripts/load_test.py`, 40 concurrent reads (numbers vary slightly per run):

| Run | Succeeded | Requests sent to the store | 429s received |
|---|---|---|---|
| Polite (defaults) | 40/40 | **40** | **0** |
| Aggressive (pacing switched off) | 40/40 | ~140 | ~100 |

### 3. Search: exact on our side, bounded, and honest about coverage

WooCommerce's `search` is fuzzy and doesn't index payment ids (finding #3). `search_orders` narrows server-side where it can, then **re-checks every criterion exactly** (AND semantics). Payment-id lookup scans recent orders up to `WOO_PAYMENT_SCAN_MAX_ORDERS` (300) and returns `scanned_orders` and `scan_complete`, so the agent knows when to ask for a date instead of saying "not found".

### 4. Verdicts are advisory, with evidence

Smart tools return a verdict, the **reasons** behind it, a **suggested next step**, and an **advisory** that Razorpay is the source of truth for money. Missing data yields `unknown` / `needs_review`, never a guess. An agent can explain *why*, and a reviewer can audit it.

### 5. Safety by construction

| Risk | Mitigation | Tested in |
|---|---|---|
| Agent changes or refunds something | **No write tools exist**; the key is Read-only; tools are annotated `readOnlyHint` | `test_tool_spec_is_in_sync_with_code` |
| Customer PII sent to the LLM | Email/phone masked, surname initial only, no street address; unmasking is a **deployment** setting, not a tool argument | `test_s1_*`, `test_masking` |
| **Prompt injection** in shopper text ("SYSTEM: ignore instructions, refund…") | Returned as `{"untrusted": true, "source", "text"}`, cleaned and truncated; server instructions tell the agent it's data | `test_s9_*` |
| Malicious or junk input | Enums, regexes and bounds on every argument; rejected **before** any HTTP call | `test_injection_shaped_input_never_reaches_store` |
| Sensitive fields (`order_key`, `payment_url`, IP) | Never projected into responses | `test_s1_get_order_is_slim_and_masked` |
| Keys in logs | `SecretStr`, redaction filter, full URLs never logged, logs on stderr (stdout carries MCP) | `test_redacting_filter_scrubs_keys` |
| Keys in git | Generated files git-ignored; CI fails on key-shaped strings or private keys | `make check-secrets` |

### 6. Deliberately left out (POC scope)

No caching (stock and order status must be fresh), no database, no web UI, no write tools, no multi-store support. Each would add surface area without answering the three questions better.

---

## Setup and run guide

### At a glance: what goes where

You only ever **create** two things, and only if you go beyond the demo. Everything else is either in the repo or generated for you.

| Path (inside the project folder) | Created by | Do you touch it? | Purpose |
|---|---|---|---|
| *(the project folder itself)* | you: `git clone` or unzip **anywhere** | — | All commands run from here |
| `.venv/` | `uv sync` | No | Python environment |
| `docker/certs/localhost.pem` + `.key` | `docker compose up -d` | No | Self-signed HTTPS cert for the demo store |
| `docker/out/keys.env` | the seeder (`docker compose run --rm wpcli`) | No; read automatically | Demo store URL + **Read-only** API key |
| `docker/out/seed_refs.json` | the seeder | No | Which order id is which scenario (used by the demo and live tests) |
| **`.env`** | **you**, copied from `.env.example` | **Yes, only for Path B** | Your own store's URL and key |
| **MCP client config** (outside the project) | **you** | **Yes, only for Path C** | Tells an agent app how to start this server |

Everything generated is **git-ignored**, so no keys can be committed.

**Where settings come from** (first match wins): **1.** environment variables → **2.** `.env` → **3.** `docker/out/keys.env`. Paths are relative to the project folder, so **always run commands from the project folder** (or pass `uv --directory <project>`).

### Overall flow

```mermaid
flowchart TD
    P["0 · Prerequisites<br/>Docker Desktop running · uv"] --> G["1 · Get the code<br/>git clone / unzip → cd into it → uv sync"]
    G --> Q{"Which store?"}
    Q -->|"Path A · recommended for review"| A["Demo store in Docker<br/>docker compose up -d<br/>docker compose run --rm wpcli"]
    Q -->|"Path B"| B["Your own WooCommerce<br/>create Read key → put it in .env"]
    A --> C["2 · Verify auth<br/>uv run woo-bridge check"]
    B --> C
    C --> D["3 · See it work<br/>demo · tests · MCP Inspector"]
    D --> E["4 · Optional · Path C<br/>plug into an agent app"]
    D --> F["5 · Clean up<br/>docker compose down -v"]
```

### Step 0: Prerequisites

| Need | Why | Check |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/), **running** | Runs the demo WooCommerce store (Path A) | `docker compose version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Installs Python 3.11+ and dependencies | `uv --version` |
| Port **8443** free | The demo store listens on `https://localhost:8443` | — |
| Node.js *(optional)* | Only for the visual MCP Inspector | `node --version` |

Commands below work in **PowerShell, macOS and Linux** terminals.

### Step 1: Get the code and install

```bash
git clone <repo-url> woo-agent-bridge
cd woo-agent-bridge
uv sync
```
(Or unzip the folder anywhere, `cd` into it, and run `uv sync`. Folder paths with spaces are fine.)

### Path A: Demo store (recommended; nothing to create or edit)

| # | Command | What happens | You should see |
|---|---|---|---|
| A1 | `docker compose up -d` | Starts MariaDB + WordPress + **nginx (HTTPS, 5 req/s limit)** at `https://localhost:8443`, and generates `docker/certs/*` | containers `db`, `wordpress`, `nginx` running |
| A2 | `docker compose run --rm wpcli` | ~1–2 min. Installs **WooCommerce 11.2.1**, 6 products, 44 orders written the way the Razorpay plugin writes them, and a **Read-only** key into `docker/out/keys.env` | last line: `done. keys written to docker/out/keys.env` |
| A3 | `uv run woo-bridge check` | Proves auth end to end (HTTPS rule, key, read access) | `OK  store=https://localhost:8443 … orders readable: 44   products readable: 6` |
| A4 | `uv run python scripts/demo.py` | A **real MCP client** starts the server and plays every support scenario | each scenario's tool call and JSON answer |
| A5 | `uv run pytest -m "live or not live"` | All offline + live tests | `63 passed` |
| A6 | `uv run python scripts/load_test.py` | Rate-limit behaviour against real 429s | `40/40` in both runs; `0` 429s by default |

With `make` (macOS/Linux): `make setup up seed check demo`. Every target is listed in the [Makefile](Makefile).

> Re-running A2 is safe. It reuses the data but **issues a new key** each time, so open a new terminal if you exported old keys as environment variables.

### Path B: Your own WooCommerce store

| # | Where | What to do |
|---|---|---|
| B1 | Your store's **wp-admin** → *WooCommerce → Settings → Advanced → REST API → Add key* | Description: `woo-agent-bridge`; User: an admin or shop manager; **Permissions: Read**. Click *Generate API key* and copy the **Consumer key** (`ck_…`) and **Consumer secret** (`cs_…`). They're shown only once |
| B2 | wp-admin → *Settings → Permalinks* | Anything **except "Plain"**, otherwise `/wp-json/` doesn't work |
| B3 | **Project folder** (next to `pyproject.toml`) | Copy the template: `cp .env.example .env` (PowerShell: `Copy-Item .env.example .env`) |
| B4 | **`.env`** | Fill in **all three** lines below (settings merge one by one, so a partial `.env` would pair your store with the demo key). Leave the rest commented unless needed |
| B5 | Terminal, in the project folder | `uv run woo-bridge check` → `OK … orders readable: N` |

```dotenv
# .env  (project root; git-ignored, never commit it)
WOO_BASE_URL=https://your-store.example.com     # store root: https, no trailing /wp-json
WOO_CONSUMER_KEY=ck_xxxxxxxxxxxxxxxxxxxxxxxx
WOO_CONSUMER_SECRET=cs_xxxxxxxxxxxxxxxxxxxxxxxx
# WOO_CA_BUNDLE=path/to/cert.pem               # only if the store uses a self-signed certificate
```

`.env` always takes priority over the demo's `docker/out/keys.env`, so both can coexist. The scripted demo and live tests rely on the demo store's seeded orders; against your own store, use the **MCP Inspector** below to call the tools.

### Path C: Plug it into an agent app

The server is started **by the agent app**, so you add one entry to that app's MCP config file:

| App | Config file location | Then |
|---|---|---|
| **Claude Desktop** (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` | Restart Claude Desktop |
| **Claude Desktop** (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` | Restart Claude Desktop |
| **Cursor** | `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project) | Reload Cursor |
| **Claude Code** | none; one command: `claude mcp add woocommerce -- uv --directory /abs/path/to/woo-agent-bridge run woo-bridge serve` | `/mcp` to confirm |
| **MCP Inspector** (debug UI) | none: `npx @modelcontextprotocol/inspector uv run woo-bridge serve` (run from the project folder) | opens in the browser |
| **Agent Studio / any hosted platform** | none: run `uv run woo-bridge serve --transport streamable-http --port 8765` and register `http://<host>:8765/mcp` | put it behind the platform's auth |

Entry to add (Claude Desktop / Cursor format):

```json
{
  "mcpServers": {
    "woocommerce": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/woo-agent-bridge", "run", "woo-bridge", "serve"]
    }
  }
}
```

- Replace `/absolute/path/to/woo-agent-bridge` with **your** project folder. On Windows, use forward slashes (`D:/projects/woo-agent-bridge`) or doubled backslashes (`D:\\projects\\woo-agent-bridge`).
- **No keys in this file are needed.** `--directory` makes the server start inside the project folder, so it picks up `.env` (Path B) or `docker/out/keys.env` (Path A) automatically. To keep keys out of the project instead, add `"env": {"WOO_BASE_URL": "...", "WOO_CONSUMER_KEY": "...", "WOO_CONSUMER_SECRET": "..."}`; environment variables win over both files.
- Then ask the agent things like *"Order 19 says failed but the customer says they paid. What happened?"*

### Step 5: Stop and clean up

| Command | Effect |
|---|---|
| `docker compose down` | Stops the demo store, keeps its data |
| `docker compose down -v` | Stops it **and deletes** the store data |
| delete `docker/out/keys.env`, `docker/certs/*`, `.env` | Removes every local key and cert |

### What the demo plays

| ID | Customer says | Tool(s) called | Expected answer |
|---|---|---|---|
| S1 | "Where is my order?" | `get_order` | `completed`, items, masked contact |
| S2 | "I paid but my order failed!" | `search_orders(email, status)` → `get_order_payment_context` | **`mismatch`** + escalate |
| S2c | "My UPI reference is pay_…" | `search_orders(razorpay_payment_id)` | the matching order |
| S3 | "Did my payment go through?" | `get_order_payment_context` | `payment_pending` (abandoned checkout) |
| S4 | "Is Masala Chai available?" | `get_stock_status(sku)` | `low_stock`: only 2 left |
| S5 | "Tulsi in 500g?" | `get_stock_status(sku)` | `varies`: 250g yes, 500g no |
| S9 | Order note tries a prompt injection | `get_order` | note fenced as `untrusted`; no refund tool exists |
| S10 | "Order 999999?" | `get_order` | structured `NOT_FOUND` |
| S11 | "Show completed orders" | `list_orders` | page 1 of 4, `has_more: true` |
| S6 | 20 questions at once | `get_stock_status` ×20 | 20/20 succeed |

### Click through it yourself (optional, needs Node.js)

```bash
npx @modelcontextprotocol/inspector uv run woo-bridge serve
```
In the browser tab: **Connect → Tools →** pick a tool → **Execute Tool**. Try `get_order_payment_context` with order **19** (`mismatch`) or **20** (`payment_pending`); `get_stock_status` with sku **CC-TULSI**; or `get_order` with **26** (prompt injection, fenced). The Inspector also shows the raw MCP messages.

The store's admin is at `https://localhost:8443/wp-admin` (`admin` / `admin-local-only`, a throwaway local-demo password).

---

## Testing

| Layer | What it proves | Command | Result |
|---|---|---|---|
| Unit + scenario (offline) | Verdict rules, retries, masking, input guards, every tool through the real MCP layer | `uv run pytest -v` | **59 passed** in ~8 s, no network |
| Live contract | Same behaviour against the Docker store | `uv run pytest -v -m live` | **4 passed** |
| End to end | Real MCP client ↔ server over stdio | `uv run python scripts/demo.py` | all scenarios answered |
| Load | Pacing and retries against real 429s | `uv run python scripts/load_test.py` | 40/40, 0 429s by default |
| Auth failure paths | Wrong secret → `AUTH_FAILED`; `http://` → `INSECURE_TRANSPORT`; nothing configured → clear hint | `uv run woo-bridge check` with bad env vars | clear message + hint |
| Static | Lint, format, secret scan | `uv run ruff check .` · `make check-secrets` | clean |

Offline tests use **real WooCommerce responses recorded from the demo store** ([`tests/fixtures/`](tests/fixtures/)), faked at the HTTP layer with respx. The rate limiter is tested with a fake clock, so nothing actually sleeps.

| Test file | Focus |
|---|---|
| [`test_verdicts.py`](tests/test_verdicts.py) | Every payment and stock verdict branch, including recorded real orders |
| [`test_client.py`](tests/test_client.py) | HTTPS-only auth, `Retry-After`, give-up shape, 401/403/404/400/500 mapping, HTML-instead-of-JSON |
| [`test_tools.py`](tests/test_tools.py) | Scenarios S1–S11 via MCP; exact-match search over fuzzy upstream; bounded scans; injection-shaped input never sent; spec ↔ code sync |
| [`test_guards.py`](tests/test_guards.py) | Token-bucket timing, masking, fencing, log redaction |
| [`test_live.py`](tests/test_live.py) | Verdicts, payment-id search, stock, pagination on the live store |

---

## Tool reference

All tools are read-only. Full JSON Schemas: [`docs/tool_spec.json`](docs/tool_spec.json).

| Tool | Primitive | Inputs | Answers |
|---|---|---|---|
| `list_orders` | list | `status`, `created_after`, `created_before`, `page`, `per_page` (≤50) | "Today's failed orders" |
| `get_order` | get | `order_id` | "Where is my order?" |
| `search_orders` | search | `email`, `phone`, `customer_name`, `order_number`, `razorpay_payment_id`, `razorpay_order_id`, `status`, dates, `limit` | Find a customer's order |
| `get_order_payment_context` | smart | `order_id` | "Money debited, order failed" |
| `list_products` | list | `stock_status`, `category_id`, `page`, `per_page` | "What's out of stock?" |
| `get_product` | get | `product_id` | Price, stock, every variation |
| `search_products` | search | `query`, `sku`, `stock_status`, `limit` | Find by name or SKU |
| `get_stock_status` | smart | `product_id` or `sku` | "Can I buy this now, in my size?" |

**Payment verdicts** (`get_order_payment_context`):

| WooCommerce status | Razorpay payment id recorded? | Verdict | Agent should |
|---|---|---|---|
| processing / completed | yes | `paid` | answer delivery questions |
| failed / pending / cancelled / on-hold | **yes** | **`mismatch`** | **escalate**; money likely captured, order not updated |
| pending / on-hold | no | `payment_pending` | ask if they paid; escalate with the Razorpay order id |
| failed | no | `payment_failed` | explain auto-reversal; offer a retry |
| cancelled | no | `cancelled_unpaid` | suggest a new order |
| refunded | any | `refunded` | share refund timeline |
| non-Razorpay (e.g. COD) | — | `not_razorpay` | answer from order status |
| anything else | no | `needs_review` | escalate; don't guess |

**Stock verdicts** (`get_stock_status`): `in_stock` · `low_stock` · `out_of_stock` · `backorder` · `varies` (per-size list included) · `unknown`.

**Errors** share one shape: `{"error": {"code", "message", "retryable", "retry_after_s"?, "hint"?}}` with codes `NOT_FOUND`, `BAD_REQUEST`, `RATE_LIMITED`, `UPSTREAM_ERROR`, `AUTH_FAILED`, `FORBIDDEN`, `INSECURE_TRANSPORT`.

---

## Limitations and what I'd do next

**Current limits** (full list with long-term fixes in [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md)):
- Payment-id search scans recent orders (bounded, and the agent is told when coverage is partial).
- `mismatch` is inferred from WooCommerce data; Razorpay remains the source of truth.
- Keys are pasted rather than obtained via the `wc-auth` consent screen (needs a public callback).
- Rate limiter is per process; several replicas would need a shared limiter.
- Results come from a local store with fake data. Agent Studio itself wasn't available, though the server speaks standard MCP over stdio and streamable HTTP.

**With a real merchant, as an FDE I would:**
1. **Shadow mode, week 1:** the agent drafts, humans send. Measure time-to-first-correct-answer, wrong-answer rate, and `mismatch` escalation precision ([measurement plan](docs/SCENARIOS.md#measurement-plan)).
2. **Close the money loop:** confirm `mismatch` via Razorpay's Payments API (`GET /payments/:id`) so the agent can distinguish captured / authorised / failed by itself.
3. **One-click onboarding:** `wc-auth` consent flow (scope `read`) behind Agent Studio's callback.
4. **Next most-asked gap:** courier tracking from Shiprocket/Delhivery plugin data.

---

## Reference

### Configuration

All settings are `WOO_*` variables, read in this order (first match wins): environment variables → `.env` → `docker/out/keys.env`. Template: [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `WOO_BASE_URL` | required | Store URL, e.g. `https://shop.example.com` |
| `WOO_CONSUMER_KEY` / `WOO_CONSUMER_SECRET` | required | Read-only API key pair |
| `WOO_CA_BUNDLE` | — | Certificate for self-signed stores (set by the demo) |
| `WOO_ALLOW_INSECURE_HTTP` | `false` | Allow plain HTTP (local experiments only) |
| `WOO_RATE_PER_SEC` / `WOO_BURST` | `4` / `4` | Client-side pacing |
| `WOO_MAX_CONCURRENCY` / `WOO_MAX_RETRIES` / `WOO_TIMEOUT_S` | `4` / `4` / `15` | Concurrency, retries, per-request timeout |
| `WOO_MASK_PII` | `true` | Mask customer contact details |
| `WOO_PAYMENT_SCAN_MAX_ORDERS` | `300` | Scan bound for Razorpay-id searches |

### Connect it to an agent

See [Path C](#path-c-plug-it-into-an-agent-app).

### Project structure

```
src/woo_agent_bridge/
  server.py      8 MCP tools: validate → fetch → project/verdict
  client.py      only HTTP code: auth, pacing, retries, pagination, error mapping
  verdicts.py    pure payment + stock rules
  models.py      slim output shapes, masking, fencing
  guards.py      token bucket, PII masking, untrusted-text fence, log redaction
  config.py      WOO_* settings          errors.py   one error shape
  cli.py         woo-bridge serve | check | spec
tests/           59 offline + 4 live tests; fixtures recorded from the demo store
scripts/         demo.py (MCP client, all scenarios) · load_test.py (real 429s)
docker/          nginx.conf (HTTPS + limit) · seed/ (WooCommerce + Razorpay-shaped data + Read key)
                 out/, certs/  → generated locally, git-ignored
docs/            AGENT_CAPABILITIES.md · LIMITATIONS.md · SCENARIOS.md · tool_spec.json · PLAN.md
```

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot connect to the Docker daemon` | Start Docker Desktop |
| Port 8443 already in use | Change `"8443:443"` in `docker-compose.yml` and `WOO_BASE_URL` |
| `check` → `missing settings: WOO_BASE_URL, ...` | Seed the store: `docker compose run --rm wpcli` |
| `AUTH_FAILED` after reseeding | Each seed issues a new key; open a new terminal so old env vars are gone |
| `certificate verify failed` | Keep `WOO_CA_BUNDLE=docker/certs/localhost.pem` (set by the seeder) |
| Live tests `skipped` | Run steps 2–3 first |
| `make: command not found` (Windows) | Use the plain `uv` / `docker` commands above |

### Glossary

| Term | Meaning |
|---|---|
| **MCP** | Model Context Protocol, an open standard for giving AI agents tools. This project is an MCP server |
| **Agent Studio** | Razorpay's platform for building AI agents; it would call these tools |
| **WooCommerce** | E-commerce plugin for WordPress, with a REST API at `/wp-json/wc/v3` |
| **`woo-razorpay`** | Razorpay's official WooCommerce checkout plugin; writes payment and order ids onto each WooCommerce order |
| **Read-only key** | WooCommerce `ck_…` / `cs_…` pair created with **Read** permission |
| **429** | HTTP "Too Many Requests"; a store's way of saying slow down |
