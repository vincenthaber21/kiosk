"""
members_helper.py
=================
Centralised helper utilities for the Members app.

Covers:
    - RFID lookup & validation
    - PIN verification & lockout management
    - Member creation / restoration
    - Balance operations
    - QR-code generation
    - Audit / deletion helpers
    - Response builders (JSON-safe dicts)

Import from views, admin, tasks, or API layers to avoid duplicating logic.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import secrets
from typing import Optional, Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

PIN_MAX_ATTEMPTS: int = 5          # lockout threshold
PIN_LENGTH: int = 4
QR_BOX_SIZE: int = 6
QR_BORDER: int = 2


# ─────────────────────────────────────────────────────────────────────────────
# Response builders
# ─────────────────────────────────────────────────────────────────────────────

def ok(**kwargs) -> dict:
    """Return a success payload dict.  Extra kwargs are merged in."""
    return {"success": True, **kwargs}


def err(error: str, code: str | None = None) -> dict:
    """Return an error payload dict."""
    payload = {"success": False, "error": error}
    if code:
        payload["code"] = code
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# RFID helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_active_member_by_rfid(rfid: str):
    """
    Fetch an active Member by RFID card number.

    Returns:
        Member instance, or None if not found / inactive.
    """
    from members.models import Member  # absolute import

    rfid = (rfid or "").strip()
    if not rfid:
        return None
    try:
        return Member.objects.select_related("user", "member_type", "member_role").get(
            rfid_card_number__iexact=rfid, is_active=True
        )
    except Member.DoesNotExist:
        return None


def validate_rfid_for_login(rfid: str) -> dict:
    """
    Full RFID-to-login validation used by api_validate_rfid_login (and any
    future endpoint that needs the same logic).

    Returns one of:
        ok(username=...) — has an active Django user → proceed to password login
        ok(member_only=True, name=...) — regular member without a user account
        err(...) — not found, inactive, or misconfigured
    """
    from login_helper import validate_rfid

    r = validate_rfid(rfid)
    if not r.get("success"):
        return err(r.get("error", "Invalid RFID"), code=r.get("code") or "error")
    if r.get("member_only"):
        return ok(member_only=True, name=r.get("name"))
    return ok(username=r.get("username"), name=r.get("name"))


# ─────────────────────────────────────────────────────────────────────────────
# Username helpers
# ─────────────────────────────────────────────────────────────────────────────

def generate_unique_member_username(first_name: str, last_name: str) -> str:
    """
    Derive a unique Member.username from first and last name
    (e.g. juan.delacruz, juan.delacruz_01, …).
    """
    from members.models import Member

    def clean(s):
        return re.sub(r'[^a-z0-9]', '', (s or '').strip().lower())

    first = clean(first_name)
    last = clean(last_name)
    if not first and not last:
        return ''

    base = f'{first}.{last}' if first and last else (first or last)
    if not Member.objects.filter(username=base).exists():
        return base

    for n in range(1, 1000):
        candidate = f'{base}_{n:02d}'
        if not Member.objects.filter(username=candidate).exists():
            return candidate

    return f'{base}_{secrets.randbelow(900000) + 100000}'


# ─────────────────────────────────────────────────────────────────────────────
# PIN helpers
# ─────────────────────────────────────────────────────────────────────────────

def verify_member_pin(member, raw_pin: str) -> dict:
    """
    Verify *raw_pin* against the member's stored PIN hash.

    Side-effects:
        - Increments pin_attempts on failure.
        - Locks the member when PIN_MAX_ATTEMPTS is reached.
        - Resets pin_attempts on success.

    Returns:
        ok()  or  err(..., code=...)
    """
    if member.is_pin_locked:
        return err("Account is locked due to too many failed PIN attempts.", code="pin_locked")

    if not member.pin_hash:
        return err("No PIN is set for this member.", code="no_pin")

    if not raw_pin or len(raw_pin) != PIN_LENGTH or not raw_pin.isdigit():
        return err(f"PIN must be exactly {PIN_LENGTH} digits.", code="invalid_pin_format")

    # Delegate to the model's own check method
    if member.check_pin(raw_pin):
        if member.pin_attempts:
            member.pin_attempts = 0
            member.save(update_fields=["pin_attempts"])
        return ok()

    # Failed attempt
    member.pin_attempts = (member.pin_attempts or 0) + 1
    if member.pin_attempts >= PIN_MAX_ATTEMPTS:
        member.is_pin_locked = True
        member.save(update_fields=["pin_attempts", "is_pin_locked"])
        logger.warning("Member %s PIN locked after %d attempts.", member.pk, member.pin_attempts)
        return err("Too many failed attempts. Account is now locked.", code="pin_locked")

    member.save(update_fields=["pin_attempts"])
    remaining = PIN_MAX_ATTEMPTS - member.pin_attempts
    return err(
        f"Incorrect PIN. {remaining} attempt(s) remaining.",
        code="wrong_pin",
    )


def reset_pin_lockout(member) -> None:
    """Unlock a member and clear their failed attempt counter."""
    member.pin_attempts = 0
    member.is_pin_locked = False
    member.save(update_fields=["pin_attempts", "is_pin_locked"])
    logger.info("PIN lockout reset for member %s.", member.pk)


def set_member_pin(member, raw_pin: str) -> dict:
    """
    Validate and set a new PIN on the member object.

    Does **not** call member.save() — caller is responsible.

    Returns:
        ok()  or  err(...)
    """
    if not raw_pin or len(raw_pin) != PIN_LENGTH or not raw_pin.isdigit():
        return err(f"PIN must be exactly {PIN_LENGTH} digits.", code="invalid_pin_format")
    member.set_pin(raw_pin)
    return ok()


# ─────────────────────────────────────────────────────────────────────────────
# Balance helpers
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def credit_balance(member, amount: float, description: str = "") -> dict:
    """Add *amount* to the member's balance and record a BalanceTransaction."""
    return _adjust_balance(member, amount, "deposit", description)


@transaction.atomic
def debit_balance(member, amount: float, description: str = "") -> dict:
    """Subtract *amount* from the member's balance and record a BalanceTransaction."""
    if member.balance < amount:
        return err("Insufficient balance.", code="insufficient_balance")
    return _adjust_balance(member, -amount, "deduction", description)


def _adjust_balance(member, signed_amount: float, tx_type: str, description: str) -> dict:
    """Internal helper — mutates balance and creates a BalanceTransaction."""
    from members.models import BalanceTransaction  # absolute import

    balance_before = member.balance
    member.balance += signed_amount
    member.last_transaction = timezone.now()
    member.save(update_fields=["balance", "last_transaction"])

    BalanceTransaction.objects.create(
        member=member,
        transaction_type=tx_type,
        amount=abs(signed_amount),
        balance_before=balance_before,
        balance_after=member.balance,
        notes=description,
    )
    return ok(balance=float(member.balance))


# ─────────────────────────────────────────────────────────────────────────────
# QR-code helpers
# ─────────────────────────────────────────────────────────────────────────────

def generate_qr_png_b64(token: str, box_size: int = QR_BOX_SIZE, border: int = QR_BORDER) -> str:
    """
    Generate a PNG QR code for *token* and return it as a base64 string.

    Suitable for embedding in `<img src="data:image/png;base64,...">`.
    """
    import qrcode  # type: ignore

    img = qrcode.make(token, box_size=box_size, border=border)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def get_or_create_member_qr(member) -> Tuple[str, bool]:
    """
    Return (qr_token_str, is_active) for the member, creating the QR record if
    needed.  Depends on mobile_api.models.MemberQRCode.
    """
    from mobile_api.models import MemberQRCode

    qr = MemberQRCode.get_or_create_for_member(member)
    return str(qr.qr_token), qr.is_active


# ─────────────────────────────────────────────────────────────────────────────
# Member creation / restoration
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def restore_deleted_member(deleted_member, restored_by: str) -> dict:
    """
    Attempt to restore a DeletedMember record back into the Member table.

    Performs conflict checks for RFID and e-mail before creating the new row.

    Returns:
        ok(member=<Member instance>)  or  err(...)
    """
    from members.models import Member, MemberType, Role  # absolute import

    if deleted_member.restored:
        return err("This record has already been restored.", code="already_restored")

    # ── Conflict checks ──────────────────────────────────────────────────────
    if Member.objects.filter(rfid_card_number=deleted_member.rfid_card_number).exists():
        return err(
            f"A member with RFID {deleted_member.rfid_card_number} already exists.",
            code="rfid_conflict",
        )

    if deleted_member.email and Member.objects.filter(email=deleted_member.email).exists():
        return err(
            f"A member with e-mail {deleted_member.email} already exists.",
            code="email_conflict",
        )

    # ── Resolve FK references ────────────────────────────────────────────────
    member_type = None
    if deleted_member.member_type_name:
        member_type = MemberType.objects.filter(name=deleted_member.member_type_name).first()

    user = None
    if deleted_member.username:
        user = User.objects.filter(username=deleted_member.username).first()

    # ── Re-create the member ─────────────────────────────────────────────────
    now = timezone.now()
    restored_member = Member.objects.create(
        rfid_card_number=deleted_member.rfid_card_number,
        first_name=deleted_member.first_name,
        last_name=deleted_member.last_name,
        email=deleted_member.email,
        phone=deleted_member.phone,
        member_type=member_type,
        member_role=Role.resolve_slug(deleted_member.role),
        balance=deleted_member.balance,
        user=user,
        pin_hash=deleted_member.pin_hash,
        is_active=True,
        date_joined=deleted_member.original_date_joined or now,
        last_transaction=deleted_member.original_last_transaction,
        created_at=deleted_member.original_created_at or now,
        updated_at=deleted_member.original_updated_at or now,
    )

    # ── Mark the deletion record as restored ─────────────────────────────────
    deleted_member.restored = True
    deleted_member.restored_at = now
    deleted_member.restored_by = restored_by
    deleted_member.save(update_fields=["restored", "restored_at", "restored_by"])

    logger.info("Member %s restored by %s.", restored_member.pk, restored_by)
    return ok(member=restored_member)


@transaction.atomic
def soft_delete_member(member, deleted_by: str) -> None:
    """
    Soft-delete a member: record a DeletedMember snapshot then deactivate.

    Safe to call from admin actions, API endpoints, or management commands.
    """
    record_member_deletion(member, deleted_by)
    member.is_active = False
    member.save(update_fields=["is_active"])
    logger.info("Member %s soft-deleted by %s.", member.pk, deleted_by)


def record_member_deletion(member, deleted_by: str) -> None:
    """
    Persist a DeletedMember snapshot for the given member without modifying
    the member itself.  Called before both soft- and hard-deletes.
    """
    from members.models import DeletedMember  # absolute import

    DeletedMember.objects.create(
        original_id=member.id,
        rfid_card_number=member.rfid_card_number,
        first_name=member.first_name,
        last_name=member.last_name,
        email=member.email,
        phone=member.phone,
        member_type_name=member.member_type.name if member.member_type else None,
        role=member.role,
        balance=member.balance,
        username=member.user.username if member.user else None,
        pin_hash=member.pin_hash,
        deleted_by=deleted_by,
        original_created_at=member.created_at,
        original_updated_at=member.updated_at,
        original_date_joined=member.date_joined,
        original_last_transaction=member.last_transaction,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_username_taken_by_other_member(username: str, exclude_member_pk=None) -> bool:
    """
    Return True if *username* is already linked to a Member other than
    *exclude_member_pk* (used when editing an existing member).
    """
    from members.models import Member  # absolute import

    qs = Member.objects.filter(user__username=username)
    if exclude_member_pk:
        qs = qs.exclude(pk=exclude_member_pk)
    return qs.exists()


_RFID_EMPTY_LITERALS = frozenset({"none", "null", "n/a", "na", "undefined"})


def normalize_rfid(value) -> Optional[str]:
    """Strip whitespace; return None when empty or a placeholder like 'None'."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() in _RFID_EMPTY_LITERALS:
        return None
    return s


def rfids_equivalent(left, right) -> bool:
    """True when both RFIDs represent the same card (case-insensitive)."""
    a = normalize_rfid(left)
    b = normalize_rfid(right)
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a.lower() == b.lower()


def rfid_is_taken_by_other(rfid: str, exclude_member_pk=None) -> bool:
    """Return True if *rfid* is assigned to a different member."""
    from members.models import Member  # absolute import

    normalized = normalize_rfid(rfid)
    if not normalized:
        return False
    qs = Member.objects.filter(rfid_card_number__iexact=normalized)
    if exclude_member_pk is not None:
        qs = qs.exclude(pk=exclude_member_pk)
    return qs.exists()


def validate_rfid_unique(rfid: str, exclude_member_pk=None) -> dict:
    """Check that an RFID card number is not already in use."""
    normalized = normalize_rfid(rfid)
    if not normalized:
        return ok()
    if rfid_is_taken_by_other(normalized, exclude_member_pk=exclude_member_pk):
        return err(
            f"RFID '{normalized}' is already assigned to another member.",
            code="rfid_taken",
        )
    return ok()


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation helpers
# ─────────────────────────────────────────────────────────────────────────────

def member_to_dict(member, include_sensitive: bool = False) -> dict:
    """
    Serialise a Member instance to a plain dict suitable for JSON responses.

    *include_sensitive* adds internal fields (pin_hash, pin_attempts) —
    only use when the caller is trusted (e.g. admin API).
    """
    data = {
        "id": member.pk,
        "full_name": member.full_name,
        "first_name": member.first_name,
        "last_name": member.last_name,
        "email": member.email,
        "phone": member.phone,
        "rfid_card_number": member.rfid_card_number,
        "role": member.role,
        "balance": float(member.balance),
        "is_active": member.is_active,
        "member_type": member.member_type.name if member.member_type else None,
        "username": member.user.username if member.user else None,
        "date_joined": member.date_joined.isoformat() if member.date_joined else None,
        "last_transaction": member.last_transaction.isoformat() if member.last_transaction else None,
        "pin_set": bool(member.pin_hash),
        "is_pin_locked": member.is_pin_locked,
    }
    if include_sensitive:
        data["pin_attempts"] = member.pin_attempts
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Request parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_json_body(request) -> Tuple[Optional[dict], Optional[dict]]:
    """
    Parse a Django request's JSON body.

    Returns:
        (data_dict, None)        — on success
        (None, err_response_dict) — on failure (caller should return this as JsonResponse)
    """
    try:
        data = json.loads(request.body)
        return data, None
    except (json.JSONDecodeError, ValueError):
        return None, err("Invalid or missing JSON body.", code="bad_json")


def require_fields(data: dict, *fields: str) -> Optional[dict]:
    """
    Ensure *fields* are all present and non-empty in *data*.

    Returns None when all fields pass, or an err() dict naming the first
    missing field.
    """
    for field in fields:
        if not data.get(field):
            return err(f"'{field}' is required.", code=f"missing_{field}")
    return None