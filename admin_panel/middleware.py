from urllib.parse import urlencode

from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages


class SecureAdminMiddleware:
    """
    Middleware to secure Django admin by requiring authentication and admin permissions.
    This ensures only authenticated admin users can access /admin/
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Check if the request is for the admin panel
        # Exclude /admin/login/ and /admin/logout/ as they are handled separately
        excluded_paths = ['/admin/login/', '/admin/logout/']
        if request.path.startswith('/admin/') and request.path not in excluded_paths:
            # Check if user is authenticated
            if not request.user.is_authenticated:
                messages.warning(request, 'Please log in to access the admin panel.')
                login_url = reverse('root_login')
                next_url = request.get_full_path()
                return redirect(f'{login_url}?{urlencode({"next": next_url})}')
            
            # Check if user has admin permissions
            # Import here to avoid circular imports
            from admin_panel.views import can_access_django_admin
            if not can_access_django_admin(request.user):
                messages.error(request, 'You do not have permission to access the admin panel.')
                return redirect('root_login')
        
        response = self.get_response(request)
        return response

