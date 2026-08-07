import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'local-demo-key-change-before-deploy')
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(BASE_DIR, 'instance', 'kopilka.sqlite3'),
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 4
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'instance', 'uploads')
    LOG_FOLDER = os.environ.get('LOG_FOLDER', os.path.join(BASE_DIR, 'logs'))
    ALLOW_DEMO_RESET = env_flag('ALLOW_DEMO_RESET', True)

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = env_flag('COOKIE_SECURE', False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = env_flag('COOKIE_SECURE', False)
