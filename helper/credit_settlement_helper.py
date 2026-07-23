"""Settle outstanding member credit (utang) from the admin dashboard."""

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Iterable, List, Tuple

from django.db import transaction as db_transaction
from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from members.models import BalanceTransaction, Member
from transactions.models import CreditPayment, CreditPaymentLine, Transaction, TransactionItem

if TYPE_CHECKING:
    pass


def _credit_outstanding_expression():
    """DB expression: remaining utang per line (vat + vatable − already paid)."""
    return Greatest(
        F("vat_amount")
        + F("vatable_sale")
        - Coalesce(F("credit_amount_paid"), Value(Decimal("0"))),
        Value(Decimal("0")),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _open_credit_sale_filter(prefix="transaction__"):
    return Q(
        **{
            f"{prefix}payment_method": "credit",
            f"{prefix}status": "completed",
            f"{prefix}credit_settled_at__isnull": True,
        }
    )


def unsettled_credit_items(member: Member):
    """Line items on open credit sales that still have an outstanding balance."""
    return (
        TransactionItem.objects.filter(
            transaction__member=member,
            credit_settled_at__isnull=True,
        )
        .filter(_open_credit_sale_filter())
        .annotate(outstanding=_credit_outstanding_expression())
        .filter(outstanding__gt=Decimal("0"))
        .select_related("transaction", "product")
        .order_by("transaction__created_at", "transaction_id", "id")
    )


def unsettled_credit_sales(member: Member):
    """Credit sales with at least one unsettled line item."""
    unsettled = unsettled_credit_items(member)
    return (
        Transaction.objects.filter(member=member)
        .filter(_open_credit_sale_filter(prefix=""))
        .filter(Exists(unsettled.filter(transaction_id=OuterRef("pk"))))
        .prefetch_related("items")
        .order_by("created_at", "id")
    )


def member_credit_outstanding_amount(member: Member) -> Decimal:
    total = (
        unsettled_credit_items(member)
        .aggregate(t=Sum("outstanding"))["t"]
    )
    return Decimal(total or 0).quantize(Decimal("0.01"))


def unsettled_credit_items_queryset():
    """Unsettled credit line items across all members (for aggregates and annotations)."""
    return (
        TransactionItem.objects.filter(credit_settled_at__isnull=True)
        .filter(_open_credit_sale_filter())
        .annotate(outstanding=_credit_outstanding_expression())
        .filter(outstanding__gt=Decimal("0"))
    )


def credit_outstanding_subquery():
    """Scalar subquery: sum of unsettled credit line totals for the outer Member row."""
    return (
        unsettled_credit_items_queryset()
        .filter(transaction__member_id=OuterRef("pk"))
        .values("transaction__member_id")
        .annotate(total=Sum("outstanding"))
        .values("total")[:1]
    )


def annotate_members_credit_outstanding(queryset):
    """Annotate a Member queryset with ``credit_outstanding`` (item-level, supports partial pay)."""
    return queryset.annotate(
        credit_outstanding=Coalesce(
            Subquery(
                credit_outstanding_subquery(),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
            Value(Decimal("0")),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )


def members_with_unsettled_credit_filter():
    """Exists filter: member has at least one unsettled credit line item."""
    return Exists(
        unsettled_credit_items_queryset().filter(transaction__member_id=OuterRef("pk"))
    )


def _item_sort_key(item: TransactionItem):
    return (item.transaction.created_at, item.transaction_id, item.id)


def _line_outstanding(item: TransactionItem) -> Decimal:
    if hasattr(item, "outstanding"):
        return Decimal(item.outstanding).quantize(Decimal("0.01"))
    return item.credit_line_outstanding


def _allocate_payment_fifo(
    items: List[TransactionItem], amount_paid: Decimal
) -> List[Tuple[TransactionItem, Decimal]]:
    """
    Apply *amount_paid* to *items* in sale order (FIFO).
    May partially pay the last affected line.
    """
    amount_paid = Decimal(amount_paid).quantize(Decimal("0.01"))
    if amount_paid <= 0:
        raise ValueError("Amount to pay must be greater than zero.")

    ordered = sorted(items, key=_item_sort_key)
    selected_max = sum((_line_outstanding(i) for i in ordered), Decimal("0")).quantize(
        Decimal("0.01")
    )
    if amount_paid > selected_max:
        raise ValueError(
            f"Amount cannot exceed ₱{selected_max} (outstanding on selected products)."
        )

    remaining = amount_paid
    allocations: List[Tuple[TransactionItem, Decimal]] = []
    for item in ordered:
        if remaining <= 0:
            break
        outstanding = _line_outstanding(item)
        if outstanding <= 0:
            continue
        applied = min(outstanding, remaining).quantize(Decimal("0.01"))
        allocations.append((item, applied))
        remaining = (remaining - applied).quantize(Decimal("0.01"))

    if not allocations:
        raise ValueError("No outstanding balance on the selected products.")

    return allocations


def _finalize_transaction_if_fully_settled(sale: Transaction, payment: CreditPayment, settled_at) -> None:
    if sale.credit_settled_at:
        return
    if sale.items.filter(credit_settled_at__isnull=True).exists():
        return
    sale.credit_settled_at = settled_at
    sale.credit_payment = payment
    sale.save(update_fields=["credit_settled_at", "credit_payment", "updated_at"])


def settle_member_credit(
    *,
    member: Member,
    payment_method: str,
    performed_by,
    authorizing_member: Member | None,
    notes: str = "",
    transaction_ids: Iterable[int] | None = None,
    item_ids: Iterable[int] | None = None,
    amount_paid: Decimal | None = None,
) -> Tuple[CreditPayment, List[Transaction], List[TransactionItem]]:
    """
    Pay off unsettled credit for *member*.

    When *item_ids* is provided, only those line items are candidates for settlement.
    When *transaction_ids* is provided (and *item_ids* is not), all unsettled
    items in those sales are candidates.
    Otherwise all unsettled items are candidates.

    *amount_paid* defaults to the outstanding total on selected lines. Any positive
    amount up to that total is accepted and applied FIFO across the selected lines
    (the last line may be partially paid).
    """
    if payment_method not in ("cash", "debit"):
        raise ValueError("Invalid payment method")

    with db_transaction.atomic():
        member = Member.objects.select_for_update().get(pk=member.pk)
        items_qs = unsettled_credit_items(member).select_for_update()

        if item_ids is not None:
            id_list = list(item_ids)
            if not id_list:
                raise ValueError("Select at least one product to pay.")
            items = list(items_qs.filter(id__in=id_list))
            if len(items) != len(set(id_list)):
                raise ValueError(
                    "One or more selected products are invalid or already settled."
                )
        elif transaction_ids is not None:
            id_list = list(transaction_ids)
            if not id_list:
                raise ValueError("Select at least one credit sale to pay.")
            items = list(items_qs.filter(transaction_id__in=id_list))
            if not items:
                raise ValueError(
                    "One or more selected sales are invalid or already settled."
                )
            found_txn_ids = {i.transaction_id for i in items}
            if found_txn_ids != set(id_list):
                raise ValueError(
                    "One or more selected sales are invalid or already settled."
                )
        else:
            items = list(items_qs)

        if not items:
            raise ValueError("This member has no outstanding credit to pay.")

        selected_total = sum((_line_outstanding(i) for i in items), Decimal("0")).quantize(
            Decimal("0.01")
        )
        if amount_paid is None:
            pay_amount = selected_total
            allocations = _allocate_payment_fifo(items, pay_amount)
        else:
            try:
                pay_amount = Decimal(str(amount_paid)).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError) as exc:
                raise ValueError("Invalid amount to pay.") from exc
            allocations = _allocate_payment_fifo(items, pay_amount)

        amount = pay_amount
        balance_before = None
        balance_after = None
        balance_txn = None

        if payment_method == "debit":
            balance_before = member.balance
            if balance_before < amount:
                raise ValueError(
                    f"Insufficient balance. Available: ₱{balance_before}, required: ₱{amount}"
                )
            member.deduct_balance(amount)
            member.refresh_from_db(fields=["balance"])
            balance_after = member.balance
            note_parts = ["Credit settlement via member debit balance"]
            if notes:
                note_parts.append(notes)
            if authorizing_member:
                note_parts.append(
                    f"(PIN authorised by {authorizing_member.full_name} — "
                    f"{authorizing_member.get_role_display()})"
                )
            balance_txn = BalanceTransaction.objects.create(
                member=member,
                transaction_type="deduction",
                amount=amount,
                balance_before=balance_before,
                balance_after=balance_after,
                notes=". ".join(note_parts),
            )

        payment = CreditPayment.objects.create(
            member=member,
            amount_paid=amount,
            payment_method=payment_method,
            balance_before=balance_before,
            balance_after=balance_after,
            performed_by=performed_by,
            notes=notes,
        )

        settled_at = timezone.now()
        settled_items: List[TransactionItem] = []
        payment_lines: List[CreditPaymentLine] = []

        for item, applied in allocations:
            new_paid = (
                Decimal(item.credit_amount_paid or 0) + applied
            ).quantize(Decimal("0.01"))
            item.credit_amount_paid = new_paid
            item.credit_payment = payment
            update_fields = ["credit_amount_paid", "credit_payment"]
            if new_paid >= item.credit_line_amount:
                item.credit_settled_at = settled_at
                update_fields.append("credit_settled_at")
            item.save(update_fields=update_fields)
            settled_items.append(item)
            payment_lines.append(
                CreditPaymentLine(
                    payment=payment,
                    item=item,
                    amount_applied=applied,
                )
            )

        CreditPaymentLine.objects.bulk_create(payment_lines)

        affected_sale_ids = {i.transaction_id for i in settled_items}
        sales = list(
            Transaction.objects.filter(id__in=affected_sale_ids).order_by(
                "created_at", "id"
            )
        )
        for sale in sales:
            _finalize_transaction_if_fully_settled(sale, payment, settled_at)

        if balance_txn:
            payment.notes = (payment.notes or "").strip()
            extra = f"Balance txn: {balance_txn.transaction_number}"
            payment.notes = f"{payment.notes}\n{extra}".strip() if payment.notes else extra
            payment.save(update_fields=["notes"])

        return payment, sales, settled_items
