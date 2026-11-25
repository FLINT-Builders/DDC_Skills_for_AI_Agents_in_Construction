---
name: "lien-waiver-tracker"
description: "Track and manage construction lien waivers. Monitor conditional and unconditional waivers, ensure compliance before payments, and prevent lien exposure."
homepage: "https://datadrivenconstruction.io"
metadata: {"openclaw": {"emoji": "📝", "os": ["darwin", "linux", "win32"], "homepage": "https://datadrivenconstruction.io", "requires": {"bins": ["python3"]}}}
---
# Lien Waiver Tracker

## Overview

Track and manage lien waivers throughout the construction payment process. Ensure proper waivers are received before releasing payments, monitor waiver status by subcontractor, and minimize lien exposure.

## Lien Waiver Types

```
┌─────────────────────────────────────────────────────────────────┐
│                    LIEN WAIVER TYPES                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  CONDITIONAL                        UNCONDITIONAL                │
│  ───────────                        ─────────────                │
│  📋 Progress - Conditional          ✅ Progress - Unconditional  │
│     Effective when paid                Immediately effective     │
│     Use with payment                   Use after check clears    │
│                                                                  │
│  📋 Final - Conditional             ✅ Final - Unconditional     │
│     For final payment                  For final payment         │
│     Upon receipt of funds              After funds received      │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Technical Implementation

```python
import hashlib
import math
import os
import stat
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum

class WaiverType(Enum):
    CONDITIONAL_PROGRESS = "conditional_progress"
    UNCONDITIONAL_PROGRESS = "unconditional_progress"
    CONDITIONAL_FINAL = "conditional_final"
    UNCONDITIONAL_FINAL = "unconditional_final"

class WaiverStatus(Enum):
    REQUESTED = "requested"
    RECEIVED = "received"
    VERIFIED = "verified"
    REJECTED = "rejected"
    MISSING = "missing"

class PaymentStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    HELD = "held"
    RELEASED = "released"

@dataclass
class Subcontractor:
    id: str
    name: str
    trade: str
    contract_amount: float
    contact_name: str
    contact_email: str
    tier: int = 1  # 1 = direct, 2 = sub-sub

@dataclass
class LienWaiver:
    id: str
    subcontractor_id: str
    waiver_type: WaiverType
    payment_application: int  # Pay app number
    through_date: datetime
    amount: float
    status: WaiverStatus = WaiverStatus.REQUESTED
    requested_date: datetime = field(default_factory=datetime.now)
    received_date: Optional[datetime] = None
    received_by: str = ""
    verified_by: str = ""
    verified_date: Optional[datetime] = None
    # Snapshot taken by verify_waiver: (file_path, file_sha256, amount,
    # subcontractor_id, payment_application, waiver_type). Any later change
    # to the file or these fields invalidates the verification.
    verified_record: Optional[Tuple] = None
    file_path: str = ""
    notes: str = ""

@dataclass
class PaymentApplication:
    number: int
    period_end: datetime
    subcontractor_id: str
    amount_requested: float
    amount_approved: float
    retainage: float
    status: PaymentStatus = PaymentStatus.PENDING
    waivers_complete: bool = False
    payment_date: Optional[datetime] = None

@dataclass
class LienExposure:
    subcontractor_id: str
    subcontractor_name: str
    total_paid: float
    unconditional_waivers: float
    conditional_pending: float
    exposure: float

class LienWaiverTracker:
    """Track and manage construction lien waivers."""

    def __init__(self, project_id: str, project_name: str):
        self.project_id = project_id
        self.project_name = project_name
        self.subcontractors: Dict[str, Subcontractor] = {}
        self.waivers: Dict[str, LienWaiver] = {}
        self.pay_apps: Dict[str, PaymentApplication] = {}

    def add_subcontractor(self, id: str, name: str, trade: str,
                         contract_amount: float, contact_name: str,
                         contact_email: str, tier: int = 1) -> Subcontractor:
        """Add subcontractor to tracking."""
        sub = Subcontractor(
            id=id,
            name=name,
            trade=trade,
            contract_amount=contract_amount,
            contact_name=contact_name,
            contact_email=contact_email,
            tier=tier
        )
        self.subcontractors[id] = sub
        return sub

    def create_payment_application(self, number: int, period_end: datetime,
                                  subcontractor_id: str, amount_requested: float,
                                  retainage_rate: float = 0.10) -> PaymentApplication:
        """Create payment application record."""
        if subcontractor_id not in self.subcontractors:
            raise ValueError(f"Subcontractor {subcontractor_id} not found")

        retainage = amount_requested * retainage_rate
        amount_approved = amount_requested - retainage

        pay_app = PaymentApplication(
            number=number,
            period_end=period_end,
            subcontractor_id=subcontractor_id,
            amount_requested=amount_requested,
            amount_approved=amount_approved,
            retainage=retainage
        )

        key = f"{subcontractor_id}-{number}"
        self.pay_apps[key] = pay_app

        # Create waiver request
        self.request_waiver(subcontractor_id, number, period_end, amount_approved)

        return pay_app

    def request_waiver(self, subcontractor_id: str, pay_app_number: int,
                      through_date: datetime, amount: float,
                      waiver_type: WaiverType = WaiverType.CONDITIONAL_PROGRESS) -> LienWaiver:
        """Request lien waiver from subcontractor."""
        waiver_id = f"LW-{subcontractor_id}-{pay_app_number}"

        waiver = LienWaiver(
            id=waiver_id,
            subcontractor_id=subcontractor_id,
            waiver_type=waiver_type,
            payment_application=pay_app_number,
            through_date=through_date,
            amount=amount
        )

        self.waivers[waiver_id] = waiver
        return waiver

    def receive_waiver(self, waiver_id: str, file_path: str,
                      verified_by: str = "") -> LienWaiver:
        """Record receipt of lien waiver.

        Receipt only stores the file path; it does not check the document.
        The waiver does not count toward payment release or lien exposure
        until verify_waiver succeeds. `verified_by` is kept for backward
        compatibility and is recorded as the person who logged receipt.
        """
        if waiver_id not in self.waivers:
            raise ValueError(f"Waiver {waiver_id} not found")

        waiver = self.waivers[waiver_id]
        waiver.status = WaiverStatus.RECEIVED
        waiver.received_date = datetime.now()
        waiver.received_by = verified_by
        waiver.file_path = file_path
        waiver.verified_by = ""
        waiver.verified_date = None
        waiver.verified_record = None

        # Check if all waivers for pay app complete
        self._check_pay_app_waivers(waiver.subcontractor_id, waiver.payment_application)

        return waiver

    def verify_waiver(self, waiver_id: str, verified_by: str) -> LienWaiver:
        """Verify a received waiver against its file and pay application.

        The waiver file must be a non-empty regular file and the waiver
        amount must match the approved pay application amount. On success
        the file digest and waiver details are recorded; release checks
        refuse the waiver if any of them change afterwards. On failure the
        waiver is rejected with the reason in `notes`.
        """
        if waiver_id not in self.waivers:
            raise ValueError(f"Waiver {waiver_id} not found")

        waiver = self.waivers[waiver_id]
        if waiver.status not in (WaiverStatus.RECEIVED, WaiverStatus.VERIFIED):
            raise ValueError(
                f"Waiver {waiver_id} is {waiver.status.value}; "
                "it must be received before it can be verified"
            )
        if not verified_by:
            raise ValueError("verified_by is required to verify a waiver")

        problem = None
        digest, file_problem = self._file_digest(waiver.file_path)
        approved = self._approved_amount(waiver)
        if file_problem:
            problem = file_problem
        elif not self._valid_amount(waiver.amount):
            problem = f"invalid waiver amount {waiver.amount!r}"
        elif approved is not None and not self._amounts_match(waiver.amount, approved):
            problem = (f"waiver amount {waiver.amount:,.2f} does not match "
                       f"approved pay application amount {approved:,.2f}")

        if problem:
            return self.reject_waiver(waiver_id, problem)

        waiver.status = WaiverStatus.VERIFIED
        waiver.verified_by = verified_by
        waiver.verified_date = datetime.now()
        waiver.verified_record = self._waiver_record(waiver, digest)

        self._check_pay_app_waivers(waiver.subcontractor_id, waiver.payment_application)

        return waiver

    def reject_waiver(self, waiver_id: str, reason: str) -> LienWaiver:
        """Reject waiver (incorrect amount, wrong form, etc.)."""
        if waiver_id not in self.waivers:
            raise ValueError(f"Waiver {waiver_id} not found")

        waiver = self.waivers[waiver_id]
        waiver.status = WaiverStatus.REJECTED
        waiver.verified_record = None
        waiver.notes = f"Rejected: {reason}"

        self._check_pay_app_waivers(waiver.subcontractor_id, waiver.payment_application)

        return waiver

    def convert_to_unconditional(self, waiver_id: str, payment_date: datetime) -> LienWaiver:
        """Convert conditional waiver to unconditional after payment clears."""
        if waiver_id not in self.waivers:
            raise ValueError(f"Waiver {waiver_id} not found")

        waiver = self.waivers[waiver_id]

        # Create new unconditional waiver
        new_type = (WaiverType.UNCONDITIONAL_PROGRESS
                   if waiver.waiver_type == WaiverType.CONDITIONAL_PROGRESS
                   else WaiverType.UNCONDITIONAL_FINAL)

        return self.request_waiver(
            waiver.subcontractor_id,
            waiver.payment_application,
            waiver.through_date,
            waiver.amount,
            new_type
        )

    @staticmethod
    def _amounts_match(left: float, right: float) -> bool:
        return math.isclose(left, right, rel_tol=0.0, abs_tol=0.01)

    @staticmethod
    def _valid_amount(amount) -> bool:
        return (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and math.isfinite(amount) and amount > 0)

    @staticmethod
    def _file_digest(file_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (sha256, None) for a non-empty regular file, else (None, reason)."""
        if not file_path:
            return None, "no waiver file recorded"
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(file_path, flags)
        except OSError as exc:
            return None, f"waiver file {file_path!r} cannot be opened: {exc.strerror or exc}"
        try:
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return None, f"waiver file {file_path!r} is not a regular file"
                digest = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            return None, f"waiver file {file_path!r} cannot be read: {exc.strerror or exc}"
        if size == 0:
            return None, f"waiver file {file_path!r} is empty"
        return digest.hexdigest(), None

    @staticmethod
    def _waiver_record(waiver: LienWaiver, digest: str) -> Tuple:
        return (waiver.file_path, digest, waiver.amount, waiver.subcontractor_id,
                waiver.payment_application, waiver.waiver_type)

    def _approved_amount(self, waiver: LienWaiver) -> Optional[float]:
        pay_app = self.pay_apps.get(f"{waiver.subcontractor_id}-{waiver.payment_application}")
        return pay_app.amount_approved if pay_app else None

    def _verification_problem(self, waiver: LienWaiver,
                              approved_amount: Optional[float]) -> Optional[str]:
        """Return why the waiver cannot be relied on now, or None if it can.

        Re-reads the waiver file and compares it, the waiver details, and the
        approved pay application amount with what verify_waiver recorded.
        """
        if waiver.status != WaiverStatus.VERIFIED:
            return f"Waiver {waiver.id} not verified (status: {waiver.status.value})"
        if waiver.verified_record is None:
            return f"Waiver {waiver.id} has no verification record; verify it again"
        digest, file_problem = self._file_digest(waiver.file_path)
        if file_problem:
            return f"Waiver {waiver.id}: {file_problem}"
        if self._waiver_record(waiver, digest) != waiver.verified_record:
            return f"Waiver {waiver.id} changed since it was verified; verify it again"
        if approved_amount is not None and not self._amounts_match(waiver.amount, approved_amount):
            return (f"Waiver {waiver.id} amount {waiver.amount:,.2f} does not match "
                    f"approved pay application amount {approved_amount:,.2f}")
        return None

    def _check_pay_app_waivers(self, subcontractor_id: str, pay_app_number: int):
        """Check if all waivers for pay app are verified and still valid."""
        key = f"{subcontractor_id}-{pay_app_number}"

        if key not in self.pay_apps:
            return

        pay_app = self.pay_apps[key]
        approved = pay_app.amount_approved

        # Check all related waivers
        related_waivers = [
            w for w in self.waivers.values()
            if w.subcontractor_id == subcontractor_id
            and w.payment_application == pay_app_number
        ]

        pay_app.waivers_complete = bool(related_waivers) and all(
            self._verification_problem(w, approved) is None
            for w in related_waivers
        )

    def calculate_exposure(self, subcontractor_id: str) -> LienExposure:
        """Calculate lien exposure for subcontractor."""
        if subcontractor_id not in self.subcontractors:
            raise ValueError(f"Subcontractor {subcontractor_id} not found")

        sub = self.subcontractors[subcontractor_id]

        # Sum payments
        total_paid = sum(
            pa.amount_approved for pa in self.pay_apps.values()
            if pa.subcontractor_id == subcontractor_id
            and pa.status == PaymentStatus.RELEASED
        )

        # Sum unconditional waivers that are verified and still match their file
        unconditional = sum(
            w.amount for w in self.waivers.values()
            if w.subcontractor_id == subcontractor_id
            and w.waiver_type in [WaiverType.UNCONDITIONAL_PROGRESS, WaiverType.UNCONDITIONAL_FINAL]
            and self._verification_problem(w, self._approved_amount(w)) is None
        )

        # Sum conditional pending
        conditional = sum(
            w.amount for w in self.waivers.values()
            if w.subcontractor_id == subcontractor_id
            and w.waiver_type in [WaiverType.CONDITIONAL_PROGRESS, WaiverType.CONDITIONAL_FINAL]
            and w.status in [WaiverStatus.RECEIVED, WaiverStatus.VERIFIED]
        )

        # Exposure = Paid - Unconditional waivers
        exposure = total_paid - unconditional

        return LienExposure(
            subcontractor_id=subcontractor_id,
            subcontractor_name=sub.name,
            total_paid=total_paid,
            unconditional_waivers=unconditional,
            conditional_pending=conditional,
            exposure=exposure
        )

    def get_missing_waivers(self) -> List[Dict]:
        """Get list of missing or pending waivers."""
        missing = []

        for waiver in self.waivers.values():
            if waiver.status in [WaiverStatus.REQUESTED, WaiverStatus.MISSING]:
                sub = self.subcontractors.get(waiver.subcontractor_id)
                missing.append({
                    "waiver_id": waiver.id,
                    "subcontractor": sub.name if sub else waiver.subcontractor_id,
                    "pay_app": waiver.payment_application,
                    "amount": waiver.amount,
                    "type": waiver.waiver_type.value,
                    "requested_date": waiver.requested_date,
                    "days_outstanding": (datetime.now() - waiver.requested_date).days
                })

        return sorted(missing, key=lambda x: -x["days_outstanding"])

    def get_waiver_status_by_sub(self, subcontractor_id: str) -> Dict:
        """Get waiver status summary for subcontractor."""
        if subcontractor_id not in self.subcontractors:
            raise ValueError(f"Subcontractor {subcontractor_id} not found")

        sub = self.subcontractors[subcontractor_id]

        waivers = [w for w in self.waivers.values() if w.subcontractor_id == subcontractor_id]

        by_status = {}
        for w in waivers:
            s = w.status.value
            by_status[s] = by_status.get(s, 0) + 1

        return {
            "subcontractor": sub.name,
            "total_waivers": len(waivers),
            "by_status": by_status,
            "total_amount": sum(w.amount for w in waivers),
            "verified_amount": sum(
                w.amount for w in waivers
                if self._verification_problem(w, self._approved_amount(w)) is None
            )
        }

    def can_release_payment(self, subcontractor_id: str, pay_app_number: int) -> Dict:
        """Check if payment can be released."""
        key = f"{subcontractor_id}-{pay_app_number}"

        if key not in self.pay_apps:
            return {"can_release": False, "reason": "Payment application not found"}

        pay_app = self.pay_apps[key]
        approved = pay_app.amount_approved

        # Check for conditional waiver
        waivers = [
            w for w in self.waivers.values()
            if w.subcontractor_id == subcontractor_id
            and w.payment_application == pay_app_number
            and w.waiver_type == WaiverType.CONDITIONAL_PROGRESS
        ]

        if not waivers:
            return {"can_release": False, "reason": "No conditional waiver received"}

        for w in waivers:
            problem = self._verification_problem(w, approved)
            if problem:
                return {"can_release": False, "reason": problem}

        return {"can_release": True, "reason": "All waivers verified", "amount": approved}

    def generate_report(self) -> str:
        """Generate lien waiver status report."""
        lines = [
            "# Lien Waiver Status Report",
            "",
            f"**Project:** {self.project_name}",
            f"**Date:** {datetime.now().strftime('%Y-%m-%d')}",
            "",
            "## Summary",
            "",
            f"- Total Subcontractors: {len(self.subcontractors)}",
            f"- Total Waivers Tracked: {len(self.waivers)}",
            f"- Missing Waivers: {len(self.get_missing_waivers())}",
            "",
            "## Exposure by Subcontractor",
            "",
            "| Subcontractor | Paid | Unconditional | Exposure |",
            "|---------------|------|---------------|----------|"
        ]

        total_exposure = 0
        for sub_id in self.subcontractors:
            exposure = self.calculate_exposure(sub_id)
            total_exposure += exposure.exposure
            lines.append(
                f"| {exposure.subcontractor_name} | ${exposure.total_paid:,.0f} | "
                f"${exposure.unconditional_waivers:,.0f} | ${exposure.exposure:,.0f} |"
            )

        lines.extend([
            f"| **TOTAL** | | | **${total_exposure:,.0f}** |",
            "",
            "## Missing Waivers",
            ""
        ])

        missing = self.get_missing_waivers()
        if missing:
            lines.append("| Subcontractor | Pay App | Amount | Days Outstanding |")
            lines.append("|---------------|---------|--------|------------------|")
            for m in missing[:10]:
                lines.append(
                    f"| {m['subcontractor']} | #{m['pay_app']} | "
                    f"${m['amount']:,.0f} | {m['days_outstanding']} |"
                )
        else:
            lines.append("*No missing waivers*")

        return "\n".join(lines)
```

## Quick Start

```python
# Initialize tracker
tracker = LienWaiverTracker("PRJ-001", "Office Tower")

# Add subcontractors
tracker.add_subcontractor(
    "SUB-001", "ABC Mechanical", "HVAC",
    contract_amount=500000,
    contact_name="John Smith",
    contact_email="john@abcmech.com"
)

tracker.add_subcontractor(
    "SUB-002", "XYZ Electric", "Electrical",
    contract_amount=350000,
    contact_name="Jane Doe",
    contact_email="jane@xyzelectric.com"
)

# Create payment applications
pa1 = tracker.create_payment_application(
    number=1,
    period_end=datetime.now(),
    subcontractor_id="SUB-001",
    amount_requested=50000
)

# Receive waiver, then verify it (the file must exist and the amount must
# match the approved pay application; otherwise the waiver is rejected)
waiver_id = f"LW-SUB-001-1"
tracker.receive_waiver(waiver_id, "/waivers/sub001_pa1.pdf", "PM")
tracker.verify_waiver(waiver_id, "Controller")

# Check if can release payment
result = tracker.can_release_payment("SUB-001", 1)
print(f"Can release: {result['can_release']} - {result['reason']}")

# Calculate exposure
exposure = tracker.calculate_exposure("SUB-001")
print(f"Lien exposure: ${exposure.exposure:,.2f}")

# Generate report
print(tracker.generate_report())
```

## Requirements

```bash
pip install (no external dependencies)
```
