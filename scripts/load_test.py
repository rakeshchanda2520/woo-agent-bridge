"""Rate-limit behaviour against the real nginx limiter in the Docker stack (5 req/s, burst 5).

Run 1 "polite": default client settings -> the token bucket keeps us under the limit, no 429s.
Run 2 "aggressive": bucket opened to 100 req/s on purpose -> nginx returns real 429s, and the
retry loop (Retry-After + backoff) still gets every request through.

    make load-test
"""

import asyncio
import statistics
import time

from woo_agent_bridge.client import WooClient
from woo_agent_bridge.config import Settings

N = 40


async def run(label: str, **overrides) -> dict:
    woo = WooClient(Settings(**overrides))  # type: ignore[call-arg]
    latencies: list[float] = []

    async def one(i: int) -> bool:
        t = time.monotonic()
        try:
            await woo.get("/products", {"per_page": 1, "page": 1 + i % 5})
            return True
        except Exception as e:  # noqa: BLE001 - we want to count any failure
            print("  failed:", e)
            return False
        finally:
            latencies.append(time.monotonic() - t)

    started = time.monotonic()
    ok = sum(await asyncio.gather(*(one(i) for i in range(N))))
    wall = time.monotonic() - started
    await woo.aclose()
    row = {
        "run": label,
        "ok": f"{ok}/{N}",
        "http_requests": woo.stats["requests"],
        "429s_seen": woo.stats["rate_limited"],
        "retries": woo.stats["retries"],
        "wall_s": round(wall, 1),
        "p50_s": round(statistics.median(latencies), 2),
        "p95_s": round(sorted(latencies)[int(0.95 * len(latencies)) - 1], 2),
    }
    print(row)
    return row


async def main() -> None:
    print(f"{N} concurrent product reads against nginx limit_req 5r/s burst 5\n")
    rows = [await run("polite (defaults)")]
    await asyncio.sleep(2)  # let the nginx bucket refill between runs
    rows.append(await run("aggressive (bucket off)", rate_per_sec=100, burst=100, max_concurrency=20, max_retries=8))

    print("\n| run | ok | HTTP requests | 429s seen | retries | wall s | p50 s | p95 s |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['run']} | {r['ok']} | {r['http_requests']} | {r['429s_seen']} | {r['retries']} | "
            f"{r['wall_s']} | {r['p50_s']} | {r['p95_s']} |"
        )


if __name__ == "__main__":
    asyncio.run(main())
