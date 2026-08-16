from datetime import date, timedelta

import pytest

from src.medication_ops import MedicationOps


def make_ops():
    ops = MedicationOps()
    ops.register_medication("A", "Alpha", "ingredient-a", "10 mg", "tablet")
    ops.register_medication("B", "Beta", "ingredient-b", "20 mg", "tablet")
    ops.register_medication("A2", "Alpha Two", "ingredient-a", "5 mg", "capsule")
    return ops


def test_rejects_expired_stock():
    ops = make_ops()
    with pytest.raises(ValueError):
        ops.receive_stock("lot-old", "A", 10, date.today() - timedelta(days=1))


def test_dispense_uses_earliest_expiry_first():
    ops = make_ops()
    ops.receive_stock("later", "A", 10, date.today() + timedelta(days=20))
    ops.receive_stock("earlier", "A", 10, date.today() + timedelta(days=5))
    receipt = ops.dispense("p1", "A", 2)
    assert receipt.lot_id == "earlier"
    assert receipt.remaining_lot_quantity == 8


def test_duplicate_active_ingredient_warning_blocks_by_default():
    ops = make_ops()
    expiry = date.today() + timedelta(days=30)
    ops.receive_stock("a", "A", 10, expiry)
    ops.receive_stock("a2", "A2", 10, expiry)
    ops.dispense("p1", "A", 1)
    with pytest.raises(ValueError, match="duplicate-active-ingredient"):
        ops.dispense("p1", "A2", 1)


def test_explicit_interaction_rule_blocks_by_default():
    ops = make_ops()
    expiry = date.today() + timedelta(days=30)
    ops.add_interaction_rule("ingredient-a", "ingredient-b")
    ops.receive_stock("a", "A", 10, expiry)
    ops.receive_stock("b", "B", 10, expiry)
    ops.dispense("p1", "A", 1)
    with pytest.raises(ValueError, match="interaction-rule"):
        ops.dispense("p1", "B", 1)


def test_allow_warnings_records_them_in_receipt():
    ops = make_ops()
    expiry = date.today() + timedelta(days=30)
    ops.add_interaction_rule("ingredient-a", "ingredient-b")
    ops.receive_stock("a", "A", 10, expiry)
    ops.receive_stock("b", "B", 10, expiry)
    ops.dispense("p1", "A", 1)
    receipt = ops.dispense("p1", "B", 1, allow_warnings=True)
    assert receipt.warnings
    assert "interaction-rule:ingredient-a:ingredient-b" in receipt.warnings


def test_refill_schedule_and_advance_are_deterministic():
    ops = make_ops()
    due = date.today() + timedelta(days=7)
    plan = ops.schedule_refill("p1", "A", 30, 28, due)
    assert plan.next_due == due
    advanced = ops.advance_refill("p1", "A")
    assert advanced.next_due == due + timedelta(days=28)


def test_due_refills_are_sorted():
    ops = make_ops()
    today = date.today()
    ops.schedule_refill("p2", "A", 10, 10, today + timedelta(days=2))
    ops.schedule_refill("p1", "B", 10, 10, today + timedelta(days=1))
    due = ops.due_refills(on_or_before=today + timedelta(days=2))
    assert [(p.patient_ref, p.sku) for p in due] == [("p1", "B"), ("p2", "A")]


def test_expiring_lots_sorted_by_expiry_then_id():
    ops = make_ops()
    today = date.today()
    ops.receive_stock("z", "A", 5, today + timedelta(days=2))
    ops.receive_stock("a", "B", 5, today + timedelta(days=2))
    lots = ops.expiring_lots(3, today=today)
    assert [lot.lot_id for lot in lots] == ["a", "z"]


def test_receipt_hash_reproducible_for_same_fresh_state():
    expiry = date.today() + timedelta(days=30)
    one = make_ops()
    two = make_ops()
    one.receive_stock("lot", "A", 10, expiry)
    two.receive_stock("lot", "A", 10, expiry)
    r1 = one.dispense("p1", "A", 2)
    r2 = two.dispense("p1", "A", 2)
    assert r1.receipt_hash == r2.receipt_hash


def test_manifest_hash_reproducible():
    expiry = date.today() + timedelta(days=30)
    one = make_ops()
    two = make_ops()
    one.receive_stock("lot", "A", 10, expiry)
    two.receive_stock("lot", "A", 10, expiry)
    assert one.inventory_manifest()["manifest_hash"] == two.inventory_manifest()["manifest_hash"]
