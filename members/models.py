import secrets
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password, check_password

class Role(models.Model):
    """Assignable roles for kiosk / admin users (Member, Staff, Cashier, Admin)."""

    slug = models.SlugField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=50)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    VALID_SLUGS = frozenset({"admin", "cashier", "staff", "member"})

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Role"
        verbose_name_plural = "Roles"

    def __str__(self):
        return self.name

    @classmethod
    def resolve_slug(cls, slug):
        """Return the Role row for *slug* (any row in the Role table), defaulting to Member."""
        s = (slug or "").strip().lower()
        if not s:
            s = "member"
        obj = cls.objects.filter(slug__iexact=s).first()
        if obj:
            return obj
        fallback = cls.objects.filter(slug="member").first()
        if fallback is None:
            raise RuntimeError("Role table is empty; apply members migrations.")
        return fallback


class MemberType(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Member type"
        verbose_name_plural = "Member types"


class Member(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    username = models.CharField(max_length=150, unique=True, null=True, blank=True)
    rfid_card_number = models.CharField(max_length=50, unique=True, null=True, blank=True)
    # store a hashed 4-digit PIN for member security (not plaintext)
    pin_hash = models.CharField(max_length=128, blank=True, null=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    member_type = models.ForeignKey(MemberType, on_delete=models.SET_NULL, null=True)
    member_role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        related_name="members",
    )
    
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    pin_attempts = models.IntegerField(default=0)
    is_pin_locked = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(auto_now_add=True)
    last_transaction = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        rfid = self.rfid_card_number or 'No RFID'
        return f"{self.first_name} {self.last_name} ({rfid})"

    @property
    def role(self):
        """Role slug for backwards compatibility (admin, cashier, staff, member)."""
        if self.member_role_id:
            return self.member_role.slug
        return "member"

    def get_role_display(self):
        """Human-readable role label (matches former CharFieldchoices API)."""
        if self.member_role_id:
            return self.member_role.name
        return "Member"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def available_balance(self):
        return self.balance

    def add_balance(self, amount):
        self.balance += amount
        self.save()

    def deduct_balance(self, amount):
        if self.balance >= amount:
            self.balance -= amount
            self.save()
            return True
        return False

    def set_pin(self, pin: str):
        """Set a 4-digit PIN for the member. PIN is hashed using Django's password hasher.

        Raises ValueError if PIN is not exactly 4 digits.
        """
        if not isinstance(pin, str) or not pin.isdigit() or len(pin) != 4:
            raise ValueError('PIN must be a 4-digit string')
        self.pin_hash = make_password(pin)
        self.save()

    def check_pin(self, pin: str) -> bool:
        """Validate a candidate PIN against stored hash."""
        if not self.pin_hash:
            return False
        return check_password(pin, self.pin_hash)

    @property
    def masked_rfid(self):
        """Return masked RFID card number showing only first 3 digits followed by asterisks.
        Example: '0008265033' -> '000****'
        """
        if not self.rfid_card_number:
            return 'N/A'
        if len(self.rfid_card_number) <= 3:
            return self.rfid_card_number
        return self.rfid_card_number[:3] + '****'

    def get_senior_profile_safe(self):
        try:
            return self.senior_profile
        except ObjectDoesNotExist:
            return None

    def get_pwd_profile_safe(self):
        try:
            return self.pwd_profile
        except ObjectDoesNotExist:
            return None

    @property
    def active_concession_kind(self):
        """Active concession at kiosk: ``senior``, ``pwd``, or ``None``."""
        sp = self.get_senior_profile_safe()
        if sp and sp.is_active:
            return 'senior'
        pp = self.get_pwd_profile_safe()
        if pp and pp.is_active:
            return 'pwd'
        return None

    class Meta:
        verbose_name = "Member"
        verbose_name_plural = "Members"
        ordering = ['-date_joined']


class ConcessionDiscountPolicy(models.Model):
    """
    Legacy percent-based policy (no longer used at checkout).

    Checkout now uses Segment product discounts (fixed pesos per segment + product discount_group).
    This table is kept for existing databases only; you may ignore or remove rows in admin if unused.
    """

    SLUG_SENIOR = 'senior'
    SLUG_PWD = 'pwd'
    SLUG_CHOICES = [
        (SLUG_SENIOR, 'Senior citizen'),
        (SLUG_PWD, 'PWD (Person with disability)'),
    ]

    slug = models.CharField(max_length=20, choices=SLUG_CHOICES, unique=True)
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('20.00'),
        help_text='Percent off the product list price for eligible members.',
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Concession discount policy'
        verbose_name_plural = 'Concession discount policies'

    def __str__(self):
        return f'{self.get_slug_display()} — {self.discount_percent}%'

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.discount_percent <= 0 or self.discount_percent > 100:
            raise ValidationError({'discount_percent': 'Percent must be between 0 and 100.'})


class SeniorCitizenProfile(models.Model):
    """Registers a member as eligible for senior-citizen concession pricing at the kiosk."""

    member = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name='senior_profile',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Uncheck to suspend senior benefits without deleting the record.',
    )
    osca_id_number = models.CharField(
        max_length=64,
        blank=True,
        help_text='Optional OSCA / ID reference for your records.',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Senior citizen profile'
        verbose_name_plural = 'Senior citizen profiles'

    def __str__(self):
        return f'Senior — {self.member.full_name}'

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.is_active and self.member_id:
            if PWDProfile.objects.filter(member_id=self.member_id, is_active=True).exists():
                raise ValidationError(
                    'This member already has an active PWD profile. '
                    'Only one concession type (senior or PWD) may be active at a time.'
                )


class PWDProfile(models.Model):
    """Registers a member as eligible for PWD concession pricing at the kiosk."""

    member = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name='pwd_profile',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Uncheck to suspend PWD benefits without deleting the record.',
    )
    pwd_id_number = models.CharField(
        max_length=64,
        blank=True,
        help_text='Optional PWD ID reference for your records.',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'PWD profile'
        verbose_name_plural = 'PWD profiles'

    def __str__(self):
        return f'PWD — {self.member.full_name}'

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.is_active and self.member_id:
            qs = SeniorCitizenProfile.objects.filter(member_id=self.member_id, is_active=True)
            if qs.exists():
                raise ValidationError(
                    'This member already has an active senior citizen profile. '
                    'Only one concession type (senior or PWD) may be active at a time.'
                )


class SegmentProductGroupDiscount(models.Model):
    """
    Fixed peso discount per customer segment and product discount_group (see inventory.Product).
    Examples: Seniors/PWDs ₱5 off dairy SKUs; members ₱10 off dairy 1L/500ml, ₱5 off 250ml, etc.
    """

    SEG_SENIOR_PWD = 'senior_pwd'
    SEG_MEMBER_RESELLER = 'member_reseller'
    SEGMENT_CHOICES = [
        (SEG_SENIOR_PWD, 'Seniors and PWDs'),
        (SEG_MEMBER_RESELLER, 'Members / resellers'),
    ]

    segment = models.CharField(max_length=32, choices=SEGMENT_CHOICES)
    discount_group = models.ForeignKey(
        'inventory.ProductDiscountGroup',
        on_delete=models.PROTECT,
        related_name='segment_discount_rules',
    )
    amount_off = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text='Pesos deducted per unit after promotional discounts are applied.',
    )
    label = models.CharField(
        max_length=160,
        blank=True,
        help_text='Optional label on receipts (defaults to amount off).',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['segment', 'discount_group']
        verbose_name = 'Segment product discount'
        verbose_name_plural = 'Segment product discounts'
        unique_together = [['segment', 'discount_group']]

    def __str__(self):
        dg = self.discount_group.name if self.discount_group_id else '—'
        return f'{self.get_segment_display()} — {dg} (₱{self.amount_off})'

    def get_discount_group_display(self):
        """Same contract as Django’s choice-field helper (used in templates)."""
        return self.discount_group.name if self.discount_group_id else '—'

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.amount_off is not None and self.amount_off <= 0:
            raise ValidationError({'amount_off': 'Amount must be greater than zero.'})


class BalanceTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('deposit', 'Deposit'),
        ('deduction', 'Deduction'),
    ]

    transaction_number = models.CharField(
        max_length=32,
        unique=True,
        db_index=True,
        editable=False,
        help_text="Unique reference for this ledger row (audit / support).",
    )
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name='balance_transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_before = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.transaction_number:
            # Uniquely identify each balance movement for security and support.
            for _ in range(8):
                candidate = secrets.token_hex(12).upper()
                if not BalanceTransaction.objects.filter(transaction_number=candidate).exclude(pk=self.pk).exists():
                    self.transaction_number = candidate
                    break
            else:
                raise RuntimeError("Could not allocate a unique transaction_number")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.transaction_number} — {self.member.full_name} - {self.transaction_type} - {self.amount}"

    class Meta:
        verbose_name = "Balance Transaction"
        verbose_name_plural = "Balance Transactions"
        ordering = ['-created_at']


class CardBalanceRefill(models.Model):
    """Card top-up performed from Django admin (and recorded for API refills)."""

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='card_balance_refills')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_before = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    notes = models.TextField(blank=True, help_text="Optional note shown on the ledger entry.")
    balance_transaction = models.OneToOneField(
        BalanceTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='card_refill',
    )
    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='card_balance_refills',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reversed_balance_refills',
    )
    reversal_balance_transaction = models.OneToOneField(
        BalanceTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='card_refill_reversal',
    )

    class Meta:
        verbose_name = "Refill card balance"
        verbose_name_plural = "Refill card balances"
        ordering = ['-created_at']

    def __str__(self):
        ref = self.balance_transaction.transaction_number if self.balance_transaction_id else "(pending)"
        return f"{ref} — {self.member.full_name} +{self.amount}"


class MemberEditHistory(models.Model):
    """Snapshot of a member's editable fields taken just before each profile update."""

    member = models.ForeignKey(
        'Member',
        on_delete=models.CASCADE,
        related_name='edit_history',
    )
    username = models.CharField(max_length=150, blank=True, null=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    rfid_card_number = models.CharField(max_length=50, null=True, blank=True)
    role = models.CharField(max_length=32, blank=True)

    edited_at = models.DateTimeField(auto_now_add=True)
    edited_by = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = 'Member Edit History'
        verbose_name_plural = 'Member Edit Histories'
        ordering = ['-edited_at']

    def __str__(self):
        return f"Edit snapshot for {self.first_name} {self.last_name} at {self.edited_at}"


class DeletedMember(models.Model):
    """Record of deleted members for easy restoration."""
    # Store original member data
    original_id = models.IntegerField(help_text="Original member ID before deletion")
    rfid_card_number = models.CharField(max_length=50, null=True, blank=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    member_type_name = models.CharField(max_length=100, blank=True, null=True)
    role = models.CharField(max_length=20)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    username = models.CharField(max_length=150, blank=True, null=True)
    pin_hash = models.CharField(max_length=128, blank=True, null=True)
    
    # Metadata
    deleted_at = models.DateTimeField(auto_now_add=True)
    deleted_by = models.CharField(max_length=150, blank=True, null=True, help_text="Username of person who deleted")
    deletion_reason = models.TextField(blank=True, help_text="Optional reason for deletion")
    
    # Store original timestamps
    original_created_at = models.DateTimeField(null=True, blank=True)
    original_updated_at = models.DateTimeField(null=True, blank=True)
    original_date_joined = models.DateTimeField(null=True, blank=True)
    original_last_transaction = models.DateTimeField(null=True, blank=True)
    
    # Restoration tracking
    restored = models.BooleanField(default=False)
    restored_at = models.DateTimeField(null=True, blank=True)
    restored_by = models.CharField(max_length=150, blank=True, null=True)
    
    class Meta:
        verbose_name = "Deleted Member"
        verbose_name_plural = "Deleted Members"
        ordering = ['-deleted_at']
    
    def __str__(self):
        return f"Deleted: {self.first_name} {self.last_name} ({self.rfid_card_number}) - {self.deleted_at}"