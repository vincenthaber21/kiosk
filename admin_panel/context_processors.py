"""Inject kiosk branding into all Django templates (system name from Kiosk Config)."""


def kiosk_branding(request):
    try:
        from admin_panel.models import KioskConfig

        name = (KioskConfig.get().system_name or '').strip() or 'Self Checkout'
    except Exception:
        name = 'Self Checkout'
    return {'kiosk_system_name': name}
