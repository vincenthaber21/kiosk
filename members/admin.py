from django.contrib import admin
from django.contrib.admin.widgets import AutocompleteSelectMultiple
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from import_export.admin import ExportMixin, ImportExportModelAdmin
from .models import (
    Role,
    MemberType,
    Member,
    BalanceTransaction,
    DeletedMember,
    CardBalanceRefill,
    SeniorCitizenProfile,
    PWDProfile,
    SegmentProductGroupDiscount,
    ConcessionDiscountPolicy,
    MemberEditHistory,
)
from .resources import (
    BalanceTransactionResource,
    CardBalanceRefillResource,
    ConcessionDiscountPolicyResource,
    DeletedMemberResource,
    MemberEditHistoryResource,
    MemberResource,
    MemberTypeResource,
    PWDProfileResource,
    RoleResource,
    SegmentProductGroupDiscountResource,
    SeniorCitizenProfileResource,
)
from django import forms
from django.utils.html import format_html, mark_safe
from django.contrib import messages
from django.template.response import TemplateResponse
import qrcode
import qrcode.image.svg
import io
import base64

from inventory.models import Product, ProductDiscount


class SegmentProductGroupDiscountForm(forms.ModelForm):
    """
    Extra product picker uses the same admin autocomplete as ProductDiscount.product
    (inventory.Product via ProductDiscount's registered autocomplete).
    """

    assign_products = forms.ModelMultipleChoiceField(
        queryset=Product.objects.all().order_by('name'),
        required=False,
        label='Products',
        help_text=(
            'Optional. Search and select products to set to this rule\'s discount group '
            '(same picker as Inventory → Product discounts).'
        ),
    )

    class Meta:
        model = SegmentProductGroupDiscount
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        self._admin_site = kwargs.pop('admin_site', admin.site)
        super().__init__(*args, **kwargs)
        self.fields['assign_products'].widget = AutocompleteSelectMultiple(
            ProductDiscount._meta.get_field('product'),
            self._admin_site,
        )
        # Widget defaults choices=() → normalizes to []; AutocompleteMixin.optgroups
        # requires ModelChoiceIterator from the form field.
        self.fields['assign_products'].widget.choices = self.fields['assign_products'].choices
        if self.instance.pk and self.instance.discount_group_id:
            self.fields['assign_products'].initial = list(
                Product.objects.filter(discount_group_id=self.instance.discount_group_id).values_list(
                    'pk', flat=True
                )
            )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('assign_products') and not cleaned_data.get('discount_group'):
            raise forms.ValidationError('Choose a discount group before assigning products.')
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if not commit or not instance.discount_group_id:
            return instance
        selected = self.cleaned_data.get('assign_products')
        if selected is not None:
            Product.objects.filter(pk__in=[p.pk for p in selected]).update(
                discount_group_id=instance.discount_group_id
            )
        return instance


class MemberPinForm(forms.ModelForm):
    pin = forms.CharField(
        required=False,
        max_length=4,
        label='4-digit PIN',
        help_text='Optional. Enter exactly 4 digits to set or change the PIN.',
        widget=forms.PasswordInput(render_value=False),
    )

    class Meta:
        model = Member
        exclude = ['user', 'member_type', 'pin_hash']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('username', 'rfid_card_number', 'email', 'phone', 'pin'):
            if name in self.fields:
                self.fields[name].required = False
        if self.instance and self.instance.pk and self.instance.pin_hash:
            self.fields['pin'].help_text = (
                'PIN is already set. Leave blank to keep it, or enter a new 4-digit PIN to change it.'
            )
        else:
            self.fields['pin'].help_text = (
                'Optional. Enter a 4-digit PIN for kiosk/login use. Leave blank if not needed.'
            )

    def clean_pin(self):
        pin = self.cleaned_data.get('pin')
        if pin:
            if not pin.isdigit() or len(pin) != 4:
                raise forms.ValidationError('PIN must be exactly 4 digits')
        return pin

    def save(self, commit=True):
        pin = self.cleaned_data.get('pin')
        instance = super().save(commit=False)
        if pin:
            instance.set_pin(pin)
            if commit:
                # set_pin already persisted pin_hash; save other edited fields only.
                instance.save()
            return instance
        if commit:
            instance.save()
        return instance


class CardBalanceRefillForm(forms.ModelForm):
    class Meta:
        model = CardBalanceRefill
        fields = ['member', 'amount', 'notes']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # On the change view, readonly fields are omitted from the form.
        if 'member' in self.fields:
            self.fields['member'].queryset = Member.objects.filter(is_active=True).order_by(
                'last_name', 'first_name'
            )

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is not None and amount <= 0:
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount


@admin.register(Role)
class RoleAdmin(ImportExportModelAdmin):
    resource_classes = [RoleResource]
    list_display = ["name", "slug", "sort_order", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug"]
    ordering = ["sort_order", "name"]


@admin.register(MemberType)
class MemberTypeAdmin(ImportExportModelAdmin):
    resource_classes = [MemberTypeResource]
    list_display = ['name', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name', 'description']


@admin.register(ConcessionDiscountPolicy)
class ConcessionDiscountPolicyAdmin(ImportExportModelAdmin):
    resource_classes = [ConcessionDiscountPolicyResource]
    list_display = ['slug', 'discount_percent', 'is_active', 'updated_at']
    list_filter = ['is_active', 'slug']
    search_fields = ['notes']


class SeniorCitizenProfileInline(admin.StackedInline):
    model = SeniorCitizenProfile
    extra = 0
    max_num = 1
    can_delete = True


class PWDProfileInline(admin.StackedInline):
    model = PWDProfile
    extra = 0
    max_num = 1
    can_delete = True


@admin.register(SegmentProductGroupDiscount)
class SegmentProductGroupDiscountAdmin(ImportExportModelAdmin):
    resource_classes = [SegmentProductGroupDiscountResource]
    form = SegmentProductGroupDiscountForm
    autocomplete_fields = ('discount_group',)
    list_display = ['segment', 'discount_group', 'amount_off', 'label', 'is_active', 'updated_at']
    list_filter = ['segment', 'discount_group', 'is_active']
    search_fields = ['label']

    def get_form(self, request, obj=None, **kwargs):
        site = self.admin_site

        class SegmentPGDForm(SegmentProductGroupDiscountForm):
            def __init__(self, *args, **inner_kwargs):
                inner_kwargs['admin_site'] = site
                super().__init__(*args, **inner_kwargs)

        kwargs['form'] = SegmentPGDForm
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        return (
            (
                None,
                {
                    'fields': (
                        'segment',
                        'discount_group',
                        'assign_products',
                        'amount_off',
                        'label',
                        'is_active',
                    ),
                    'description': (
                        'Checkout uses each product\'s <strong>Discount group</strong> with these segment rules. '
                        'Pick products below or set the group on each product in Inventory.'
                    ),
                },
            ),
        )


@admin.register(SeniorCitizenProfile)
class SeniorCitizenProfileAdmin(ImportExportModelAdmin):
    resource_classes = [SeniorCitizenProfileResource]
    list_display = ['member', 'is_active', 'osca_id_number', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['member__first_name', 'member__last_name', 'member__rfid_card_number', 'osca_id_number']
    autocomplete_fields = ['member']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(PWDProfile)
class PWDProfileAdmin(ImportExportModelAdmin):
    resource_classes = [PWDProfileResource]
    list_display = ['member', 'is_active', 'pwd_id_number', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['member__first_name', 'member__last_name', 'member__rfid_card_number', 'pwd_id_number']
    autocomplete_fields = ['member']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Member)
class MemberAdmin(ImportExportModelAdmin):
    resource_classes = [MemberResource]
    form = MemberPinForm
    inlines = [SeniorCitizenProfileInline, PWDProfileInline]
    list_display = ['full_name', 'username', 'email', 'rfid_card_number', 'member_role', 'balance', 'is_active', 'pin_set', 'pin_lockout_status', 'qr_code_thumbnail']
    list_filter = ['member_role', 'is_active', 'is_pin_locked']
    search_fields = ['first_name', 'last_name', 'rfid_card_number', 'email', 'user__username']
    readonly_fields = ['created_at', 'updated_at', 'qr_code_display', 'date_joined', 'last_transaction', 'pin_status']
    actions = ['soft_delete_selected', 'hard_delete_selected', 'reset_pin_lockout']

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        if not obj or not obj.pk:
            readonly = [f for f in readonly if f != 'pin_status']
        return readonly

    def get_changeform_initial_data(self, request):
        member_role_id = Role.objects.filter(slug='member').values_list('pk', flat=True).first()
        initial = {
            'balance': '0.00',
            'is_active': True,
        }
        if member_role_id:
            initial['member_role'] = member_role_id
        return initial

    def get_fieldsets(self, request, obj=None):
        security_fields = (
            ('pin_status', 'pin', 'pin_attempts', 'is_pin_locked')
            if obj and obj.pk
            else ('pin', 'pin_attempts', 'is_pin_locked')
        )
        base = [
            (
                None,
                {
                    'fields': ('username', 'rfid_card_number', 'first_name', 'last_name', 'email', 'phone'),
                    'description': (
                        'Only <strong>first name</strong> and <strong>last name</strong> are required. '
                        'Everything else is optional unless your co-op needs it.'
                    ),
                },
            ),
            ('Role', {'fields': ('member_role', 'balance', 'is_active')}),
            ('Security', {'fields': security_fields}),
            ('Timestamps', {'fields': ('date_joined', 'last_transaction', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
        ]
        if obj and obj.pk:
            base.insert(0, ('QR Code', {'fields': ('qr_code_display',), 'description': 'Member\'s unique QR code for fund transfers via the mobile app.'}))
        return base

    def pin_status(self, obj):
        if not obj or not obj.pk:
            return '—'
        if obj.pin_hash:
            return format_html(
                '<span style="color:#2e7d32;font-weight:600;">PIN is set</span>'
                '<br><span style="color:#666;font-size:12px;">'
                'The PIN from Add member (or a previous save) is stored securely. '
                'Leave the PIN field blank to keep it, or enter a new 4-digit PIN to change it.'
                '</span>'
            )
        return format_html(
            '<span style="color:#c62828;font-weight:600;">No PIN set</span>'
            '<br><span style="color:#666;font-size:12px;">'
            'This member cannot log in with a PIN until one is set below.'
            '</span>'
        )
    pin_status.short_description = 'PIN status'

    # ── QR helpers ────────────────────────────────────────────────────────────

    def _get_qr_token(self, obj):
        """Return the member's QR token string, creating the record if needed."""
        from mobile_api.models import MemberQRCode
        qr = MemberQRCode.get_or_create_for_member(obj)
        return str(qr.qr_token), qr.is_active

    def _render_qr_png_b64(self, token, size=200):
        """Generate a PNG QR code and return it as a base64 string."""
        img = qrcode.make(token, box_size=6, border=2)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode()

    def qr_code_display(self, obj):
        """Large QR shown on the member detail/change page."""
        if not obj.pk:
            return '—'
        token, is_active = self._get_qr_token(obj)
        b64 = self._render_qr_png_b64(token, size=200)
        status_color = '#2e7d32' if is_active else '#c62828'
        status_label = 'Active' if is_active else 'Inactive'
        return format_html(
            '<div style="display:inline-block;text-align:center;">'
            '<img src="data:image/png;base64,{}" width="200" height="200" '
            'style="border:1px solid #e0e0e0;border-radius:8px;padding:8px;background:#fff;" />'
            '<br><span style="font-size:11px;color:{};font-weight:600;">{}</span>'
            '<br><span style="font-size:10px;color:#888;font-family:monospace;">{}</span>'
            '</div>',
            b64, status_color, status_label, token[:18] + '…',
        )
    qr_code_display.short_description = 'QR Code'

    def qr_code_thumbnail(self, obj):
        """Small QR shown in the member list view."""
        if not obj.pk:
            return '—'
        try:
            token, is_active = self._get_qr_token(obj)
            b64 = self._render_qr_png_b64(token, size=60)
            border = '2px solid #2e7d32' if is_active else '2px solid #c62828'
            return format_html(
                '<img src="data:image/png;base64,{}" width="60" height="60" '
                'style="border:{};border-radius:4px;background:#fff;" '
                'title="{}" />',
                b64, border, token,
            )
        except Exception:
            return '—'
    qr_code_thumbnail.short_description = 'QR'
    qr_code_thumbnail.allow_tags = True

    def username(self, obj):
        if obj.user:
            username_value = obj.user.username
            # Check if this username is duplicated (used by multiple members)
            duplicate_count = Member.objects.filter(user__username=username_value).exclude(pk=obj.pk).count()
            
            if duplicate_count > 0:
                # Show in red if duplicate
                return format_html(
                    '<span style="color: red; font-weight: bold;">{}</span>',
                    username_value
                )
            return username_value
        return '-'
    username.short_description = 'Username'
    username.admin_order_field = 'user__username'

    def pin_set(self, obj):
        return bool(obj.pin_hash)
    pin_set.boolean = True
    pin_set.short_description = 'PIN set?'

    def pin_lockout_status(self, obj):
        if obj.is_pin_locked:
            return format_html('<span style="color:red;font-weight:bold;">LOCKED ({} attempts)</span>', obj.pin_attempts)
        if obj.pin_attempts > 0:
            return format_html('<span style="color:orange;">{}/5 attempts</span>', obj.pin_attempts)
        return mark_safe('<span style="color:green;">OK</span>')
    pin_lockout_status.short_description = 'PIN Status'

    def reset_pin_lockout(self, request, queryset):
        updated = queryset.update(pin_attempts=0, is_pin_locked=False)
        messages.success(request, f'PIN lockout reset for {updated} member(s).')
    reset_pin_lockout.short_description = 'Reset PIN lockout (unlock selected members)'
    
    def delete_model(self, request, obj):
        """Override delete to record deletion and use soft delete."""
        # Record the deletion before soft-deleting
        self._record_deletion(obj, request.user.username)
        # Soft delete: set is_active to False instead of hard deleting
        obj.is_active = False
        obj.save()
        messages.success(request, f'Member "{obj.full_name}" has been soft-deleted (deactivated). Record saved for restoration.')
    
    def delete_queryset(self, request, queryset):
        """Override bulk delete to record deletions and use soft delete."""
        count = 0
        for obj in queryset:
            self._record_deletion(obj, request.user.username)
            obj.is_active = False
            obj.save()
            count += 1
        messages.success(request, f'{count} member(s) have been soft-deleted (deactivated). Records saved for restoration.')
    
    def _record_deletion(self, member, deleted_by_username):
        """Record member data before deletion."""
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
            deleted_by=deleted_by_username,
            original_created_at=member.created_at,
            original_updated_at=member.updated_at,
            original_date_joined=member.date_joined,
            original_last_transaction=member.last_transaction,
        )
    
    def soft_delete_selected(self, request, queryset):
        """Custom action for soft delete (recommended)."""
        self.delete_queryset(request, queryset)
    soft_delete_selected.short_description = "Soft delete selected members (recommended - allows restoration)"
    
    def hard_delete_selected(self, request, queryset):
        """Custom action for hard delete (permanent) with a confirmation step."""
        if request.POST.get('post') == 'yes':
            count = 0
            for obj in queryset:
                self._record_deletion(obj, request.user.username)
                # Delete protected related records before removing the member
                obj.card_balance_refills.all().delete()
                obj.delete()
                count += 1
            messages.warning(
                request,
                f'{count} member(s) have been permanently deleted from the database. '
                f'A record has been saved in Deleted Members for audit purposes.'
            )
            return None  # return to changelist

        # First pass — show the confirmation page
        return TemplateResponse(
            request,
            'admin/members/member/hard_delete_confirmation.html',
            {
                **self.admin_site.each_context(request),
                'title': 'Confirm Permanent Delete',
                'queryset': queryset,
                'action_checkbox_name': '_selected_action',
                'opts': self.model._meta,
            },
        )
    hard_delete_selected.short_description = "Hard delete selected members (PERMANENT - use with caution)"


@admin.register(BalanceTransaction)
class BalanceTransactionAdmin(ExportMixin, admin.ModelAdmin):
    resource_classes = [BalanceTransactionResource]
    list_display = ['transaction_number', 'member', 'transaction_type', 'amount', 'balance_after', 'created_at']
    list_filter = ['transaction_type', 'created_at']
    search_fields = ['transaction_number', 'member__first_name', 'member__last_name', 'member__rfid_card_number']
    readonly_fields = ['transaction_number', 'created_at']


@admin.register(CardBalanceRefill)
class CardBalanceRefillAdmin(ExportMixin, admin.ModelAdmin):
    resource_classes = [CardBalanceRefillResource]
    form = CardBalanceRefillForm
    list_display = [
        'transaction_number_display',
        'member',
        'amount',
        'balance_before',
        'balance_after',
        'performed_by',
        'created_at',
    ]
    list_filter = ['created_at']
    search_fields = [
        'member__first_name',
        'member__last_name',
        'member__rfid_card_number',
        'balance_transaction__transaction_number',
        'notes',
    ]
    readonly_fields = [
        'transaction_number_display',
        'balance_transaction',
        'balance_before',
        'balance_after',
        'performed_by',
        'created_at',
        'reversed_at',
        'reversed_by',
        'reversal_balance_transaction',
    ]

    def get_readonly_fields(self, request, obj=None):
        ro = list(self.readonly_fields)
        if obj and obj.pk:
            ro.extend(['member', 'amount', 'notes'])
        return ro

    def transaction_number_display(self, obj):
        if obj.balance_transaction_id:
            return obj.balance_transaction.transaction_number
        return '—'

    transaction_number_display.short_description = 'Transaction #'

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            return
        with transaction.atomic():
            member = Member.objects.select_for_update().get(pk=obj.member_id)
            balance_before = member.balance
            member.add_balance(obj.amount)
            member.refresh_from_db(fields=['balance'])
            balance_after = member.balance
            notes_full = 'Card balance refill (admin)'
            if obj.notes:
                notes_full += f'. {obj.notes}'
            notes_full += f' — by {request.user.get_username()}'
            bt = BalanceTransaction.objects.create(
                member=member,
                transaction_type='deposit',
                amount=obj.amount,
                balance_before=balance_before,
                balance_after=balance_after,
                notes=notes_full,
            )
            obj.balance_before = balance_before
            obj.balance_after = balance_after
            obj.balance_transaction = bt
            obj.performed_by = request.user
            super().save_model(request, obj, form, change)


@admin.register(DeletedMember)
class DeletedMemberAdmin(ExportMixin, admin.ModelAdmin):
    resource_classes = [DeletedMemberResource]
    list_display = ['first_name', 'last_name', 'rfid_card_number', 'role', 'deleted_at', 'deleted_by', 'restored']
    list_filter = ['restored', 'deleted_at', 'role']
    search_fields = ['first_name', 'last_name', 'rfid_card_number', 'email', 'username']
    readonly_fields = ['deleted_at', 'original_created_at', 'original_updated_at', 'original_date_joined', 
                       'original_last_transaction', 'restored', 'restored_at', 'restored_by']
    actions = ['restore_selected_members']
    
    def has_add_permission(self, request):
        return False  # Can't manually add deleted members
    
    def restore_selected_members(self, request, queryset):
        """Restore selected deleted members."""
        restored_count = 0
        for deleted_member in queryset.filter(restored=False):
            try:
                # Check if member with same RFID already exists
                if Member.objects.filter(rfid_card_number=deleted_member.rfid_card_number).exists():
                    messages.warning(request, 
                        f'Cannot restore {deleted_member.first_name} {deleted_member.last_name}: '
                        f'Member with RFID {deleted_member.rfid_card_number} already exists.')
                    continue
                
                # Check if email conflicts
                if deleted_member.email and Member.objects.filter(email=deleted_member.email).exists():
                    messages.warning(request,
                        f'Cannot restore {deleted_member.first_name} {deleted_member.last_name}: '
                        f'Member with email {deleted_member.email} already exists.')
                    continue
                
                # Restore member
                member_type = None
                if deleted_member.member_type_name:
                    try:
                        member_type = MemberType.objects.get(name=deleted_member.member_type_name)
                    except MemberType.DoesNotExist:
                        pass
                
                # Create or find user if username was provided
                user = None
                if deleted_member.username:
                    try:
                        user = User.objects.get(username=deleted_member.username)
                    except User.DoesNotExist:
                        pass
                
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
                    date_joined=deleted_member.original_date_joined or timezone.now(),
                    last_transaction=deleted_member.original_last_transaction,
                    created_at=deleted_member.original_created_at or timezone.now(),
                    updated_at=deleted_member.original_updated_at or timezone.now(),
                )
                
                # Mark as restored
                deleted_member.restored = True
                deleted_member.restored_at = timezone.now()
                deleted_member.restored_by = request.user.username
                deleted_member.save()
                
                restored_count += 1
                messages.success(request, f'Successfully restored: {restored_member.full_name} (RFID: {restored_member.rfid_card_number})')
            except Exception as e:
                messages.error(request, f'Error restoring {deleted_member.first_name} {deleted_member.last_name}: {str(e)}')
        
        if restored_count > 0:
            messages.success(request, f'Successfully restored {restored_count} member(s).')
    
    restore_selected_members.short_description = "Restore selected deleted members"


@admin.register(MemberEditHistory)
class MemberEditHistoryAdmin(ExportMixin, admin.ModelAdmin):
    resource_classes = [MemberEditHistoryResource]
    list_display = ['member', 'first_name', 'last_name', 'rfid_card_number', 'role', 'edited_by', 'edited_at']
    list_filter = ['edited_at', 'role']
    search_fields = ['first_name', 'last_name', 'rfid_card_number', 'username', 'edited_by']
    readonly_fields = [
        'member', 'username', 'first_name', 'last_name', 'email', 'phone',
        'rfid_card_number', 'role', 'edited_at', 'edited_by',
    ]
    ordering = ['-edited_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
