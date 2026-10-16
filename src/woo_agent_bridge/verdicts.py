"""Pure business rules. No HTTP, no I/O: plain dicts in, verdicts out. Easy to test, easy to argue about."""

from typing import Any

from .models import (
    PaymentContext,
    StockReport,
    StockVerdict,
    now_iso,
    razorpay_order_id,
    razorpay_payment_id,
    utc,
)

# WooCommerce's store-wide default for "notify on low stock" (woocommerce_notify_low_stock_amount).
# ponytail: assumed default; read /settings/products if a merchant changed it.
DEFAULT_LOW_STOCK = 2


def stock_verdict(item: dict[str, Any]) -> tuple[StockVerdict, str]:
    """Verdict for one sellable unit (simple product or a single variation)."""
    qty = item.get("stock_quantity")
    status = item.get("stock_status")
    if not item.get("manage_stock") or qty is None:
        # Store doesn't count units for this item; only the status flag is meaningful.
        mapped: dict[str, StockVerdict] = {
            "instock": "in_stock",
            "outofstock": "out_of_stock",
            "onbackorder": "backorder",
        }
        if status in mapped:
            return mapped[status], f"Stock not tracked by quantity; store flag is '{status}'."
        return "unknown", "Stock is not tracked and no status flag is set."

    if qty <= 0:
        if item.get("backorders", "no") != "no":
            return "backorder", f"{qty} units on hand; store accepts backorders (ships when restocked)."
        return "out_of_stock", f"{qty} units on hand and backorders are not allowed."
    low = item.get("low_stock_amount")
    low = DEFAULT_LOW_STOCK if low in (None, "") else int(low)
    if qty <= low:
        return "low_stock", f"Only {qty} left (low-stock threshold is {low})."
    return "in_stock", f"{qty} units available."


def stock_report(product: dict[str, Any], variations: list[dict[str, Any]]) -> StockReport:
    if product.get("type") == "variable" and variations:
        rows = []
        for v in variations:
            verdict, reason = stock_verdict(v)
            label = ", ".join(a.get("option", "") for a in v.get("attributes", [])) or v.get("sku", "")
            rows.append(
                {
                    "variation_id": v["id"],
                    "sku": v.get("sku", ""),
                    "label": label,
                    "verdict": verdict,
                    "quantity": v.get("stock_quantity"),
                    "reason": reason,
                }
            )
        verdicts = {r["verdict"] for r in rows}
        overall: StockVerdict = verdicts.pop() if len(verdicts) == 1 else "varies"
        sellable = [r["label"] for r in rows if r["verdict"] in ("in_stock", "low_stock")]
        reason = f"Available in: {', '.join(sellable)}." if sellable else "No variation is currently sellable."
        return StockReport(
            product_id=product["id"],
            name=product.get("name", ""),
            sku=product.get("sku", ""),
            verdict=overall,
            quantity=None,
            reason=reason,
            variations=rows,
            as_of=now_iso(),
        )

    verdict, reason = stock_verdict(product)
    return StockReport(
        product_id=product["id"],
        name=product.get("name", ""),
        sku=product.get("sku", ""),
        verdict=verdict,
        quantity=product.get("stock_quantity"),
        reason=reason,
        as_of=now_iso(),
    )


PAID_STATUSES = {"processing", "completed"}
WAITING_STATUSES = {"pending", "on-hold", "checkout-draft"}


def payment_context(order: dict[str, Any]) -> PaymentContext:
    status = order.get("status", "")
    method = order.get("payment_method", "")
    pay_id = razorpay_payment_id(order)
    rzp_order = razorpay_order_id(order)
    reasons: list[str] = []

    def result(verdict, next_step: str) -> PaymentContext:
        return PaymentContext(
            order_id=order["id"],
            verdict=verdict,
            order_status=status,
            payment_method=method,
            total=order.get("total", "0"),
            currency=order.get("currency", ""),
            razorpay_payment_id=pay_id,
            razorpay_order_id=rzp_order,
            paid_at=utc(order.get("date_paid_gmt")),
            reasons=reasons,
            suggested_next_step=next_step,
            as_of=now_iso(),
        )

    if method != "razorpay":
        reasons.append(f"Order was placed with payment method '{method or 'none'}', not Razorpay.")
        return result("not_razorpay", "Answer from the order status; Razorpay records won't exist for this order.")

    reasons.append(f"Order status is '{status}'.")
    reasons.append(f"Razorpay payment id {'is ' + pay_id if pay_id else 'is NOT'} recorded on the order.")
    if rzp_order:
        reasons.append(f"Razorpay order {rzp_order} was created at checkout.")

    if status == "refunded":
        return result("refunded", "Tell the customer the order was refunded; share refund timelines per policy.")
    if pay_id and status in PAID_STATUSES:
        return result("paid", "Payment is recorded; answer fulfilment questions from the order status.")
    if pay_id:
        reasons.append(
            "A payment id exists but the order did not move to a paid status (often a missed webhook "
            "or a late capture)."
        )
        return result(
            "mismatch",
            f"Escalate to a human: verify {pay_id} in the Razorpay dashboard, then either "
            "mark the order paid or initiate a refund. Do not promise either yet.",
        )
    if status == "failed":
        return result(
            "payment_failed",
            "Tell the customer no payment was recorded; if money was debited, banks "
            "usually auto-reverse failed UPI/card debits. Offer to retry checkout.",
        )
    if status in WAITING_STATUSES:
        reasons.append("Checkout was started but no payment was recorded.")
        return result(
            "payment_pending",
            "Ask whether the customer completed payment; if money was debited, "
            f"escalate with Razorpay order {rzp_order or '(none recorded)'}.",
        )
    if status == "cancelled":
        return result("cancelled_unpaid", "Order was cancelled before payment; the customer can place a new order.")
    reasons.append(f"Status '{status}' with no payment id is unusual for a Razorpay order.")
    return result("needs_review", "Escalate to a human to check the order and Razorpay dashboard.")
