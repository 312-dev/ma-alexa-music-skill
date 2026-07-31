"""Setup wizard for Ampere.

Mounted by the bridge:

    from setup_ui import bp as setup_bp
    app.register_blueprint(setup_bp)
"""

from .views import bp

__all__ = ["bp"]
