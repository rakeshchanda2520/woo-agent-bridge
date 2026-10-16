# Assumptions, limitations and the long-term fix

This is a POC. Each item says what is true today, why, and what I would do for production.

| # | Limitation | Why it's like this | Long-term fix |
|---|---|---|---|
| 1 | **Keys are pasted, not obtained by a consent screen.** | WooCommerce's `/wc-auth/v1/authorize` flow requires an `https://` callback and posts the keys with `wp_safe_remote_post`, which blocks localhost and private IPs (verified in `class-wc-auth.php`). It can't complete against a laptop. | Agent Studio hosts a public HTTPS callback; the merchant clicks "Connect WooCommerce" → Approve (scope `read`) → keys land in a secrets vault per merchant. |
| 2 | **Payment-id lookup scans recent orders** (default 300). | WooCommerce's `search` doesn't cover `transaction_id` or plugin meta (verified on the live store). | Ask for a date to narrow the scan; or a tiny companion WP plugin exposing an indexed `?transaction_id=` filter; or resolve via Razorpay's API (`payment → notes → woocommerce order id`). |
| 3 | **`mismatch` is a WooCommerce-side inference.** | The connector only sees what the plugin wrote; a payment id on a failed order strongly suggests a missed webhook but doesn't prove capture. | Call Razorpay `GET /payments/:id` to return captured/authorized/failed and the refund state. Then the agent can say "you'll get it back in 5–7 days" with evidence. |
| 4 | **Order-number lookup assumes number == id.** | True in core WooCommerce; sequential-order-number plugins break it. The tool checks `number` and returns no match rather than the wrong order. | Detect the plugin and query its meta key. |
| 5 | **`search` is fuzzy upstream.** | WooCommerce's search is a LIKE across many fields (`19` matched every `+919…` phone). | Already mitigated: every filter is re-checked exactly on our side. Ceiling: one search fetches at most `WOO_PAYMENT_SCAN_MAX_ORDERS`. |
| 6 | **Rate limiter is per process.** | Simple and correct for one connector instance per store. | Shared limiter (Redis/GCRA) keyed by store if several replicas or other integrations share the host's budget. |
| 7 | **Store-wide low-stock default assumed to be 2.** | WooCommerce's default for `woocommerce_notify_low_stock_amount`; products with their own threshold use it. | Read `/settings/products` once at startup. |
| 8 | **Variations: first 100 per product.** | Enough for size/flavour catalogues. | Paginate variations. |
| 9 | **No shipment tracking.** | Not in WooCommerce core; lives in courier plugins (Shiprocket, Delhivery …). | Read the courier plugin's order meta, or the courier's API. |
| 10 | **Write-scope keys are not detected.** | WooCommerce exposes no endpoint to read a key's own permissions. Safety holds anyway: the connector has no write tools. | Make Agent Studio's onboarding create the key with `scope=read` (item 1). |
| 11 | **PII masking is field-based.** | Masks known contact fields; free text (`customer_note`) may still contain a phone number a shopper typed. | Add a PII scrubber on untrusted text, or drop notes unless the deployment needs them. |
| 12 | **Demo data is synthetic.** | No real merchant data allowed. Results (load test, scenarios) are against the seeded Docker store, **not production**. | Shadow-mode measurement on a real store (see [SCENARIOS.md](SCENARIOS.md#measurement-plan)). |
| 13 | **Agent Studio runtime integration is assumed, not tested.** | I don't have access to Agent Studio. The server speaks standard MCP over stdio and streamable HTTP and was driven end to end by a real MCP client (`make demo`). | Register it in Agent Studio and run the golden scenarios there. |

## Assumptions

- The merchant takes payments with the official **`woo-razorpay`** plugin (v4.x). Its storage (`payment_method = razorpay`, payment id in `transaction_id`, Razorpay order id in meta `razorpay_order_id<wc_id>`) was read from the plugin's source, and the seeder mirrors it.
- The store is on WooCommerce 7+ with the REST API at `/wp-json/wc/v3` (pretty permalinks on). Tested on **WooCommerce 11.2.1 / WordPress 7.1.3**, where HPOS is the default.
- The store is served over HTTPS (the production norm). Plain HTTP is refused unless explicitly allowed.
