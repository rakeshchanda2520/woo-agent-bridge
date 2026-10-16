"""Agent-facing output shapes and the projections from raw WooCommerce JSON.

Projections are deliberately slim: fewer tokens, fewer hallucinations, and no fields an agent
should never see (order_key, payment_url, customer IP/user-agent, raw meta_data).
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .guards import UntrustedText, fence, mask_email, mask_phone, strip_html


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# ---------- Orders ----------


class Customer(BaseModel):
    name: str
    email: str
    phone: str
    city: str
    state: str
    country: str
    pii_masked: bool


class LineItem(BaseModel):
    name: str
    sku: str
    quantity: int
    total: str
    product_id: int
    variation_id: int | None = None


class OrderSummary(BaseModel):
    order_id: int
    number: str
    status: str
    created_at: str
    total: str
    currency: str
    payment_method: str
    customer_name: str
    item_count: int


class OrderDetail(OrderSummary):
    customer: Customer
    items: list[LineItem]
    shipping_total: str
    discount_total: str
    refunded_total: str
    paid_at: str | None
    completed_at: str | None
    razorpay_payment_id: str | None = Field(None, description="WooCommerce transaction_id, set by the Razorpay plugin")
    razorpay_order_id: str | None
    customer_note: UntrustedText | None
    as_of: str


class OrderPage(BaseModel):
    orders: list[OrderSummary]
    page: int
    per_page: int
    total: int
    total_pages: int
    has_more: bool
    as_of: str


class OrderSearchResult(BaseModel):
    orders: list[OrderSummary]
    matched: int
    scanned_orders: int
    scan_complete: bool = Field(description="False means older orders were not searched; narrow by date to go further")
    as_of: str


_RZP_ORDER_META = re.compile(r"^razorpay_order_id(_1cc)?\d+$")


def razorpay_order_id(order: dict[str, Any]) -> str | None:
    """The Razorpay plugin stores its order id under meta key 'razorpay_order_id<wc_order_id>'."""
    for m in order.get("meta_data", []):
        if _RZP_ORDER_META.match(str(m.get("key", ""))) and m.get("value"):
            return str(m["value"])
    return None


def razorpay_payment_id(order: dict[str, Any]) -> str | None:
    tx = order.get("transaction_id") or ""
    return tx if tx.startswith("pay_") else None


def utc(gmt: str | None) -> str | None:
    """WooCommerce *_gmt fields are UTC but carry no offset; make that explicit for the agent."""
    return f"{gmt}Z" if gmt else None


def _name(addr: dict[str, Any], mask: bool) -> str:
    first, last = addr.get("first_name", ""), addr.get("last_name", "")
    if mask and last:
        last = last[0] + "."
    return f"{first} {last}".strip() or "Guest"


def to_order_summary(o: dict[str, Any], mask: bool) -> OrderSummary:
    return OrderSummary(
        order_id=o["id"],
        number=str(o.get("number", o["id"])),
        status=o["status"],
        created_at=utc(o.get("date_created_gmt")) or o.get("date_created") or "",
        total=o.get("total", "0"),
        currency=o.get("currency", ""),
        payment_method=o.get("payment_method", ""),
        customer_name=_name(o.get("billing", {}), mask),
        item_count=sum(int(li.get("quantity", 0)) for li in o.get("line_items", [])),
    )


def to_order_detail(o: dict[str, Any], mask: bool) -> OrderDetail:
    b = o.get("billing", {})
    customer = Customer(
        name=_name(b, mask),
        email=mask_email(b.get("email", "")) if mask else b.get("email", ""),
        phone=mask_phone(b.get("phone", "")) if mask else b.get("phone", ""),
        city=b.get("city", ""),
        state=b.get("state", ""),
        country=b.get("country", ""),
        pii_masked=mask,
    )
    refunded = sum(abs(float(r.get("total", 0) or 0)) for r in o.get("refunds", []))
    return OrderDetail(
        **to_order_summary(o, mask).model_dump(),
        customer=customer,
        items=[
            LineItem(
                name=li["name"],
                sku=li.get("sku") or "",
                quantity=li["quantity"],
                total=li["total"],
                product_id=li["product_id"],
                variation_id=li.get("variation_id") or None,
            )
            for li in o.get("line_items", [])
        ],
        shipping_total=o.get("shipping_total", "0"),
        discount_total=o.get("discount_total", "0"),
        refunded_total=f"{refunded:.2f}",
        paid_at=utc(o.get("date_paid_gmt")),
        completed_at=utc(o.get("date_completed_gmt")),
        razorpay_payment_id=razorpay_payment_id(o),
        razorpay_order_id=razorpay_order_id(o),
        customer_note=fence(o.get("customer_note"), "customer_note"),
        as_of=now_iso(),
    )


# ---------- Products & stock ----------

StockVerdict = Literal["in_stock", "low_stock", "out_of_stock", "backorder", "varies", "unknown"]


class ProductSummary(BaseModel):
    product_id: int
    name: str
    sku: str
    type: str
    price: str
    stock_status: str
    stock_quantity: int | None
    parent_id: int | None = None


class VariationSummary(BaseModel):
    variation_id: int
    sku: str
    attributes: dict[str, str]
    price: str
    stock_status: str
    stock_quantity: int | None


class ProductDetail(ProductSummary):
    categories: list[str]
    manage_stock: bool
    backorders: str
    low_stock_threshold: int | None
    description: UntrustedText | None
    variations: list[VariationSummary]
    as_of: str


class ProductPage(BaseModel):
    products: list[ProductSummary]
    page: int
    per_page: int
    total: int
    total_pages: int
    has_more: bool
    as_of: str


class StockReport(BaseModel):
    product_id: int
    name: str
    sku: str
    verdict: StockVerdict
    quantity: int | None
    reason: str
    variations: list[dict[str, Any]] = Field(default_factory=list, description="Per-variation verdicts")
    advisory: str = "Read from WooCommerce at as_of; stock can change before checkout."
    as_of: str


def to_product_summary(p: dict[str, Any]) -> ProductSummary:
    return ProductSummary(
        product_id=p["id"],
        name=p.get("name", ""),
        sku=p.get("sku", ""),
        type=p.get("type", ""),
        price=str(p.get("price", "")),
        stock_status=p.get("stock_status", ""),
        stock_quantity=p.get("stock_quantity"),
        parent_id=p.get("parent_id") or None,
    )


def to_variation(v: dict[str, Any]) -> VariationSummary:
    return VariationSummary(
        variation_id=v["id"],
        sku=v.get("sku", ""),
        attributes={a.get("name", ""): a.get("option", "") for a in v.get("attributes", [])},
        price=str(v.get("price", "")),
        stock_status=v.get("stock_status", ""),
        stock_quantity=v.get("stock_quantity"),
    )


def to_product_detail(p: dict[str, Any], variations: list[dict[str, Any]]) -> ProductDetail:
    return ProductDetail(
        **to_product_summary(p).model_dump(),
        categories=[c.get("name", "") for c in p.get("categories", [])],
        manage_stock=bool(p.get("manage_stock")),
        backorders=p.get("backorders", "no"),
        low_stock_threshold=p.get("low_stock_amount"),
        description=fence(strip_html(p.get("short_description") or p.get("description")), "product_description"),
        variations=[to_variation(v) for v in variations],
        as_of=now_iso(),
    )


# ---------- Payment context ----------

PaymentVerdict = Literal[
    "paid",
    "refunded",
    "mismatch",
    "payment_pending",
    "payment_failed",
    "cancelled_unpaid",
    "not_razorpay",
    "needs_review",
]


class PaymentContext(BaseModel):
    order_id: int
    verdict: PaymentVerdict
    order_status: str
    payment_method: str
    total: str
    currency: str
    razorpay_payment_id: str | None
    razorpay_order_id: str | None
    paid_at: str | None
    reasons: list[str]
    suggested_next_step: str
    advisory: str = (
        "Derived from WooCommerce records only. Razorpay is the source of truth for money movement; "
        "never promise a refund or confirm capture from this alone."
    )
    as_of: str
