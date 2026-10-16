"""Business rules, tested with plain dicts and real recorded orders. No HTTP, no mocks."""

import pytest
from conftest import load

from woo_agent_bridge.verdicts import payment_context, stock_report, stock_verdict


def order(**kw):
    base = {
        "id": 1,
        "status": "processing",
        "payment_method": "razorpay",
        "transaction_id": "",
        "total": "100.00",
        "currency": "INR",
        "meta_data": [],
    }
    return base | kw


# ---------- payment ----------


def test_s2_failed_order_with_payment_id_is_mismatch():
    ctx = payment_context(load("order_mismatch"))
    assert ctx.verdict == "mismatch"
    assert ctx.razorpay_payment_id == "pay_SEEDS2000000002"
    assert ctx.razorpay_order_id == "order_SEEDS2000000002"
    assert "Escalate" in ctx.suggested_next_step


def test_s1_completed_with_payment_id_is_paid():
    assert payment_context(load("order_paid")).verdict == "paid"


def test_cod_is_not_razorpay():
    assert payment_context(load("order_cod")).verdict == "not_razorpay"


@pytest.mark.parametrize(
    "status,tx,expected",
    [
        ("pending", "", "payment_pending"),
        ("on-hold", "", "payment_pending"),
        ("failed", "", "payment_failed"),
        ("cancelled", "", "cancelled_unpaid"),
        ("refunded", "pay_ABC123456", "refunded"),
        ("pending", "pay_ABC123456", "mismatch"),
        ("cancelled", "pay_ABC123456", "mismatch"),
        ("completed", "", "needs_review"),  # paid status but no payment reference: don't guess
    ],
)
def test_payment_matrix(status, tx, expected):
    assert payment_context(order(status=status, transaction_id=tx)).verdict == expected


def test_non_razorpay_transaction_id_is_not_treated_as_payment():
    assert payment_context(order(status="failed", transaction_id="txn_from_other_gateway")).verdict == "payment_failed"


def test_magic_checkout_meta_key_is_recognised():
    o = order(status="pending", meta_data=[{"key": "razorpay_order_id_1cc1", "value": "order_1CCXYZ123"}])
    assert payment_context(o).razorpay_order_id == "order_1CCXYZ123"


# ---------- stock ----------


@pytest.mark.parametrize(
    "item,expected",
    [
        ({"manage_stock": True, "stock_quantity": 40, "backorders": "no"}, "in_stock"),
        ({"manage_stock": True, "stock_quantity": 2, "low_stock_amount": 5}, "low_stock"),
        ({"manage_stock": True, "stock_quantity": 2, "low_stock_amount": None}, "low_stock"),  # store default 2
        ({"manage_stock": True, "stock_quantity": 3, "low_stock_amount": None}, "in_stock"),
        ({"manage_stock": True, "stock_quantity": 0, "backorders": "no"}, "out_of_stock"),
        ({"manage_stock": True, "stock_quantity": -1, "backorders": "notify"}, "backorder"),
        ({"manage_stock": False, "stock_quantity": None, "stock_status": "instock"}, "in_stock"),
        ({"manage_stock": False, "stock_quantity": None, "stock_status": "onbackorder"}, "backorder"),
        ({"manage_stock": False, "stock_quantity": None}, "unknown"),
    ],
)
def test_stock_matrix(item, expected):
    assert stock_verdict(item)[0] == expected


def test_s4_recorded_low_stock_product():
    report = stock_report(load("products_sku_masala")[0], [])
    assert (report.verdict, report.quantity) == ("low_stock", 2)


def test_s5_variable_product_reports_per_size():
    report = stock_report(load("product_variable"), load("variations"))
    assert report.verdict == "varies"
    by_size = {v["label"]: v["verdict"] for v in report.variations}
    assert by_size == {"250g": "in_stock", "500g": "out_of_stock"}
    assert "250g" in report.reason
