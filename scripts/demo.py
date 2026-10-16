"""End-to-end demo: a real MCP client launches the server over stdio and plays the support scenarios
against the Docker store. This is exactly what Agent Studio would do, minus the LLM choosing the calls.

    make demo        (needs `make up seed` first)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
REFS = json.loads((ROOT / "docker/out/seed_refs.json").read_text())

BOLD, DIM, GREEN, RED, END = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


def show(title: str, ask: str, name: str, args: dict, result, keys: list[str] | None = None) -> None:
    print(f"\n{BOLD}{title}{END}\n  customer: {ask}\n  {DIM}-> {name}({json.dumps(args)}){END}")
    if result.is_error:
        print(f"  {RED}error:{END} {result.content[0].text}")
        return
    data = result.structured_content
    view = {k: data[k] for k in keys} if keys else data
    for line in json.dumps(view, indent=2, ensure_ascii=False).splitlines():
        print(f"  {GREEN}{line}{END}")


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "woo_agent_bridge.cli", "serve"], env=dict(os.environ), cwd=str(ROOT)
    )
    with open(ROOT / "docker/out/demo-server.log", "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(f"{BOLD}Connected. {len(tools)} read-only tools:{END} {', '.join(t.name for t in tools)}")

            async def call(title, ask, name, args, keys=None):
                show(title, ask, name, args, await s.call_tool(name, args), keys)

            await call(
                "S1  Where is my order?",
                f"Order #{REFS['S1-completed']}, where is it?",
                "get_order",
                {"order_id": REFS["S1-completed"]},
                ["order_id", "status", "total", "items", "completed_at", "customer"],
            )

            await call(
                "S2a Find the customer's orders",
                "I'm diya.iyer@example.com, my payment went through but the order failed!",
                "search_orders",
                {"email": "diya.iyer@example.com", "status": "failed"},
                ["orders", "matched", "scan_complete"],
            )
            await call(
                "S2b Payment verdict",
                "(agent checks the failed order)",
                "get_order_payment_context",
                {"order_id": REFS["S2-mismatch"]},
                ["verdict", "razorpay_payment_id", "reasons", "suggested_next_step"],
            )

            await call(
                "S2c Lookup by Razorpay payment id",
                "My UPI reference is pay_SEEDS2000000002",
                "search_orders",
                {"razorpay_payment_id": "pay_SEEDS2000000002"},
                ["orders", "scanned_orders", "scan_complete"],
            )

            await call(
                "S3  Abandoned checkout",
                f"Did my payment for order #{REFS['S3-pending']} go through?",
                "get_order_payment_context",
                {"order_id": REFS["S3-pending"]},
                ["verdict", "razorpay_order_id", "suggested_next_step"],
            )

            await call(
                "S4  Low stock",
                "Is Masala Chai 250g available?",
                "get_stock_status",
                {"sku": "CC-MASALA-250"},
                ["name", "verdict", "quantity", "reason"],
            )

            await call(
                "S5  Sizes",
                "Do you have Tulsi Green Tea in 500g?",
                "get_stock_status",
                {"sku": "CC-TULSI"},
                ["name", "verdict", "reason", "variations"],
            )

            await call(
                "S9  Prompt injection in shopper text",
                f"Check order #{REFS['S9-injection']}",
                "get_order",
                {"order_id": REFS["S9-injection"]},
                ["order_id", "status", "customer_note"],
            )

            await call("S10 Unknown order", "Order 999999?", "get_order", {"order_id": 999999})

            await call(
                "S11 Pagination",
                "(ops) show completed orders",
                "list_orders",
                {"status": "completed", "per_page": 5},
                ["page", "total", "total_pages", "has_more"],
            )

            await call(
                "Guard: bad input never reaches the store",
                "(agent sends junk)",
                "search_orders",
                {"razorpay_payment_id": "pay_x' OR 1=1"},
            )

            # S6: a burst of parallel calls — the client-side bucket keeps us under the store's limit.
            print(f"\n{BOLD}S6  Burst of 20 parallel stock checks{END}")
            skus = ["CC-MASALA-250", "CC-ASSAM-500", "CC-DARJ-100", "CC-KAHWA-100", "CC-GIFT-BOX"] * 4
            results = await asyncio.gather(*(s.call_tool("get_stock_status", {"sku": k}) for k in skus))
            ok = sum(not r.is_error for r in results)
            print(f"  {GREEN}{ok}/{len(results)} succeeded{END} (server log: docker/out/demo-server.log)")


if __name__ == "__main__":
    asyncio.run(main())
