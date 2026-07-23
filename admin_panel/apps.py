from django.apps import AppConfig
import logging
import os

logger = logging.getLogger(__name__)


class AdminPanelConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'admin_panel'
    
    def ready(self):
        """Called when Django starts. Start the scheduler here."""
        # Prevent running in migrations, tests, or shell
        import sys
        
        # Check if we're in a management command context
        is_management_command = any([
            'migrate' in sys.argv,
            'makemigrations' in sys.argv,
            'test' in sys.argv,
            'shell' in sys.argv,
            'shell_plus' in sys.argv,
            'collectstatic' in sys.argv,
        ])
        
        # Only start scheduler when running the server
        if not is_management_command and (
            'runserver' in sys.argv or 
            'gunicorn' in sys.argv or 
            'uwsgi' in sys.argv or
            os.environ.get('RUN_MAIN') == 'true'  # Django auto-reload check
        ):
            try:
                from .scheduler import start_scheduler
                start_scheduler()
            except Exception as e:
                logger.error(f"Failed to start scheduler: {str(e)}", exc_info=True)

        # Initialize performance helpers (safe after app registry is ready)
        try:
            from admin_panel_helper import initialize_performance_helpers
            initialize_performance_helpers()
        except Exception as e:
            logger.error(f"Failed to initialize performance helpers: {str(e)}", exc_info=True)
