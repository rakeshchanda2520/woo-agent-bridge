"""woo-bridge serve | check | spec"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .client import WooClient
from .config import Settings
from .errors import ConnectorError


async def _check() -> int:
    """Prove the auth flow end to end: transport rules, key validity, read access."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as e:
        missing = ", ".join("WOO_" + str(err["loc"][0]).upper() for err in e.errors())
        print(f"FAILED  missing settings: {missing}", file=sys.stderr)
        print(
            "  hint: run `docker compose run --rm wpcli` for the demo store, or copy .env.example to .env",
            file=sys.stderr,
        )
        return 1
    try:
        woo = WooClient(settings)
        page = await woo.get_page("/orders", {"per_page": 1})
        products = await woo.get_page("/products", {"per_page": 1})
        await woo.aclose()
    except ConnectorError as e:
        print(f"FAILED  {e.code}: {e.message}" + (f"\n  hint: {e.hint}" if e.hint else ""), file=sys.stderr)
        return 1
    print(f"OK  store={settings.base_url}  key=...{settings.consumer_key.get_secret_value()[-4:]}")
    print(f"    orders readable: {page.total}   products readable: {products.total}")
    return 0


async def _spec(out: Path) -> None:
    from .server import INSTRUCTIONS, build_server

    # Spec generation needs no live store; dummy settings never make a request.
    server = build_server(Settings(base_url="https://example.invalid", consumer_key="x", consumer_secret="x"))  # noqa: S106
    tools = [t.model_dump(by_alias=True, exclude_none=True) for t in await server.list_tools()]
    spec = {"server": "woo-agent-bridge", "version": "0.1.0", "instructions": INSTRUCTIONS, "tools": tools}
    out.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(tools)} tools to {out}")


def main() -> None:
    p = argparse.ArgumentParser(prog="woo-bridge", description="Read-only WooCommerce MCP connector")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    sub.add_parser("check", help="verify credentials and read access against the store")
    sp = sub.add_parser("spec", help="write the MCP tool spec JSON")
    sp.add_argument("--out", type=Path, default=Path("docs/tool_spec.json"))
    a = p.parse_args()

    if a.cmd == "serve":
        from .server import main as serve

        serve(a.transport, a.host, a.port)
    elif a.cmd == "check":
        sys.exit(asyncio.run(_check()))
    else:
        asyncio.run(_spec(a.out))


if __name__ == "__main__":
    main()
