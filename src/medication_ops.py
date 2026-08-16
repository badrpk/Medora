from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class Medication:
    sku: str
    name: str
    active_ingredient: str
    strength: str
    form: str


@dataclass(frozen=True)
class StockLot:
    lot_id: str
    sku: str
    quantity: int
    expires_on: date


@dataclass(frozen=True)
class RefillPlan:
    patient_ref: str
    sku: str
    quantity: int
    interval_days: int
    next_due: date


@dataclass(frozen=True)
class DispenseReceipt:
    patient_ref: str
    sku: str
    quantity: int
    lot_id: str
    remaining_lot_quantity: int
    warnings: Tuple[str, ...]
    receipt_hash: str


class MedicationOps:
    """Deterministic pharmacy operations helper.

    This module does not diagnose, prescribe, infer doses, or replace a
    pharmacist/clinician. Interaction warnings are produced only from explicit
    rules supplied to the registry by the caller.
    """

    def __init__(self) -> None:
        self.medications: Dict[str, Medication] = {}
        self.lots: Dict[str, StockLot] = {}
        self.refills: Dict[Tuple[str, str], RefillPlan] = {}
        self.interactions: Set[frozenset[str]] = set()
        self.patient_active_skus: Dict[str, Set[str]] = {}

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.strip().split())

    def register_medication(
        self,
        sku: str,
        name: str,
        active_ingredient: str,
        strength: str,
        form: str,
    ) -> Medication:
        sku = self._clean(sku)
        if not sku:
            raise ValueError("sku is required")
        med = Medication(
            sku=sku,
            name=self._clean(name),
            active_ingredient=self._clean(active_ingredient).lower(),
            strength=self._clean(strength),
            form=self._clean(form).lower(),
        )
        if not all((med.name, med.active_ingredient, med.strength, med.form)):
            raise ValueError("all medication fields are required")
        existing = self.medications.get(sku)
        if existing and existing != med:
            raise ValueError(f"sku already registered with different metadata: {sku}")
        self.medications[sku] = med
        return med

    def add_interaction_rule(self, ingredient_a: str, ingredient_b: str) -> None:
        a = self._clean(ingredient_a).lower()
        b = self._clean(ingredient_b).lower()
        if not a or not b or a == b:
            raise ValueError("interaction rule requires two distinct ingredients")
        self.interactions.add(frozenset((a, b)))

    def receive_stock(
        self,
        lot_id: str,
        sku: str,
        quantity: int,
        expires_on: date,
    ) -> StockLot:
        if sku not in self.medications:
            raise KeyError(f"unknown sku: {sku}")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if expires_on < date.today():
            raise ValueError("cannot receive already-expired stock")
        lot_id = self._clean(lot_id)
        if not lot_id:
            raise ValueError("lot_id is required")
        if lot_id in self.lots:
            raise ValueError(f"duplicate lot_id: {lot_id}")
        lot = StockLot(lot_id, sku, quantity, expires_on)
        self.lots[lot_id] = lot
        return lot

    def expiring_lots(self, within_days: int, *, today: Optional[date] = None) -> List[StockLot]:
        if within_days < 0:
            raise ValueError("within_days must be non-negative")
        today = today or date.today()
        cutoff = today + timedelta(days=within_days)
        return sorted(
            [lot for lot in self.lots.values() if today <= lot.expires_on <= cutoff and lot.quantity > 0],
            key=lambda lot: (lot.expires_on, lot.lot_id),
        )

    def schedule_refill(
        self,
        patient_ref: str,
        sku: str,
        quantity: int,
        interval_days: int,
        next_due: date,
    ) -> RefillPlan:
        if sku not in self.medications:
            raise KeyError(f"unknown sku: {sku}")
        if quantity <= 0 or interval_days <= 0:
            raise ValueError("quantity and interval_days must be positive")
        patient_ref = self._clean(patient_ref)
        if not patient_ref:
            raise ValueError("patient_ref is required")
        plan = RefillPlan(patient_ref, sku, quantity, interval_days, next_due)
        self.refills[(patient_ref, sku)] = plan
        return plan

    def due_refills(self, *, on_or_before: date) -> List[RefillPlan]:
        return sorted(
            [plan for plan in self.refills.values() if plan.next_due <= on_or_before],
            key=lambda plan: (plan.next_due, plan.patient_ref, plan.sku),
        )

    def _warnings(self, patient_ref: str, sku: str) -> List[str]:
        med = self.medications[sku]
        warnings: List[str] = []
        active_skus = self.patient_active_skus.get(patient_ref, set())

        for other_sku in sorted(active_skus):
            if other_sku == sku:
                continue
            other = self.medications[other_sku]
            if other.active_ingredient == med.active_ingredient:
                warnings.append(
                    f"duplicate-active-ingredient:{med.active_ingredient}:{other_sku}"
                )
            pair = frozenset((med.active_ingredient, other.active_ingredient))
            if pair in self.interactions:
                warnings.append(
                    "interaction-rule:" + ":".join(sorted(pair))
                )
        return sorted(set(warnings))

    def dispense(
        self,
        patient_ref: str,
        sku: str,
        quantity: int,
        *,
        on_date: Optional[date] = None,
        allow_warnings: bool = False,
    ) -> DispenseReceipt:
        if sku not in self.medications:
            raise KeyError(f"unknown sku: {sku}")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        patient_ref = self._clean(patient_ref)
        if not patient_ref:
            raise ValueError("patient_ref is required")
        on_date = on_date or date.today()

        warnings = self._warnings(patient_ref, sku)
        if warnings and not allow_warnings:
            raise ValueError("dispense blocked by warnings: " + ",".join(warnings))

        eligible = sorted(
            [
                lot for lot in self.lots.values()
                if lot.sku == sku and lot.quantity >= quantity and lot.expires_on >= on_date
            ],
            key=lambda lot: (lot.expires_on, lot.lot_id),
        )
        if not eligible:
            raise ValueError("insufficient non-expired stock in a single lot")

        lot = eligible[0]
        remaining = lot.quantity - quantity
        self.lots[lot.lot_id] = StockLot(lot.lot_id, lot.sku, remaining, lot.expires_on)
        self.patient_active_skus.setdefault(patient_ref, set()).add(sku)

        payload = {
            "patient_ref": patient_ref,
            "sku": sku,
            "quantity": quantity,
            "lot_id": lot.lot_id,
            "remaining_lot_quantity": remaining,
            "warnings": warnings,
        }
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return DispenseReceipt(
            patient_ref=patient_ref,
            sku=sku,
            quantity=quantity,
            lot_id=lot.lot_id,
            remaining_lot_quantity=remaining,
            warnings=tuple(warnings),
            receipt_hash=digest,
        )

    def advance_refill(self, patient_ref: str, sku: str) -> RefillPlan:
        key = (patient_ref, sku)
        plan = self.refills[key]
        updated = RefillPlan(
            patient_ref=plan.patient_ref,
            sku=plan.sku,
            quantity=plan.quantity,
            interval_days=plan.interval_days,
            next_due=plan.next_due + timedelta(days=plan.interval_days),
        )
        self.refills[key] = updated
        return updated

    def inventory_manifest(self) -> dict:
        meds = [asdict(self.medications[key]) for key in sorted(self.medications)]
        lots = [
            {
                **asdict(self.lots[key]),
                "expires_on": self.lots[key].expires_on.isoformat(),
            }
            for key in sorted(self.lots)
        ]
        payload = {"medications": meds, "lots": lots}
        payload["manifest_hash"] = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return payload
