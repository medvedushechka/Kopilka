import os

from flask import Flask, flash, redirect, render_template, url_for
from flask_login import LoginManager, current_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect

from config import Config


db = SQLAlchemy()
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Войдите в аккаунт, чтобы продолжить.'
login_manager.login_message_category = 'warning'
login_manager.session_protection = 'strong'


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from app.auth.routes import auth_bp
    from app.main.routes import main_bp
    from app.loans.routes import loans_bp
    from app.admin.routes import admin_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(loans_bp, url_prefix='/cabinet')
    app.register_blueprint(admin_bp, url_prefix='/admin')

    @app.before_request
    def block_disabled_accounts():
        if current_user.is_authenticated and current_user.is_blocked:
            logout_user()
            flash('Аккаунт заблокирован службой безопасности.', 'danger')
            return redirect(url_for('auth.login'))

    @app.context_processor
    def inject_notification_count():
        try:
            from flask_login import current_user
            from app.models import Notification

            if current_user.is_authenticated:
                count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
                return {'unread_notifications_count': count}
        except Exception:
            # Сбой счётчика не должен ломать отрисовку всей страницы.
            pass
        return {'unread_notifications_count': 0}

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        return render_template('errors/csrf.html', reason=error.description), 400

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        return response

    return app
