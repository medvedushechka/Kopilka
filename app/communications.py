from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal
from flask import current_app
from app.time_utils import utc_now
from app import db
from app.models import Notification, NotificationPreference, CommunicationLog, MessageTemplate, User

DEFAULT_TEMPLATES = [
    ('loan_created', 'Заявка создана', 'Ваша заявка создана и отправлена на проверку: {{ message }}', 'loan'),
    ('loan_funded', 'Заявка профинансирована', 'Ваша заявка полностью профинансирована. Следующий шаг — TEST-перевод денег.', 'loan'),
    ('investment_created', 'Инвестиция создана', 'Вы вложили средства в заявку. Детали: {{ message }}', 'investment'),
    ('payment_received', 'Платёж получен', 'Платёж успешно отражён в TEST-контуре Kopilka.', 'payment'),
    ('profit_received', 'Получена прибыль', 'По инвестиции начислен TEST-возврат и прибыль.', 'investment'),
    ('support_reply', 'Ответ поддержки', 'По вашему обращению появился новый ответ оператора.', 'support'),
    ('system_notice', 'Системное уведомление', '{{ message }}', 'system'),
]


def _logs_dir() -> str:
    default_path = os.path.abspath(os.path.join(current_app.root_path, '..', 'logs'))
    path = current_app.config.get('LOG_FOLDER', default_path)
    os.makedirs(path, exist_ok=True)
    return path


def write_test_log(channel: str, recipient: str, subject: str, body: str) -> None:
    filename = {'email': 'emails.log', 'sms': 'sms.log', 'push': 'push.log'}.get(channel, 'communications.log')
    path = os.path.join(_logs_dir(), filename)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write('\n' + '=' * 80 + '\n')
        fh.write(f'{utc_now().isoformat()}Z | {channel.upper()} TEST\n')
        fh.write(f'TO: {recipient}\n')
        fh.write(f'SUBJECT: {subject}\n')
        fh.write('BODY:\n')
        fh.write(body.strip() + '\n')


def seed_message_templates() -> int:
    created = 0
    for code, title, body, category in DEFAULT_TEMPLATES:
        existing = MessageTemplate.query.filter_by(code=code).first()
        if not existing:
            db.session.add(MessageTemplate(code=code, title=title, body=body, category=category))
            created += 1
    return created


def get_or_create_preferences(user_id: int) -> NotificationPreference:
    pref = NotificationPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = NotificationPreference(user_id=user_id)
        db.session.add(pref)
        db.session.flush()
    return pref


def render_template_text(text: str, **context) -> str:
    result = text or ''
    for key, value in context.items():
        result = result.replace('{{ ' + key + ' }}', str(value)).replace('{{' + key + '}}', str(value))
    return result


def send_test_channel(user: User, channel: str, title: str, message: str, category: str = 'system', source_type: str = 'notification', source_id: int | None = None) -> CommunicationLog:
    if channel == 'email':
        recipient = user.email
    elif channel == 'sms':
        recipient = user.phone or '+375000000000'
    else:
        recipient = f'user:{user.id}'
    status = 'sent'
    error_message = None
    try:
        write_test_log(channel, recipient, title, message)
    except Exception as exc:  # Ошибка тестового канала не должна прерывать основную операцию.
        status = 'failed'
        error_message = str(exc)
    log = CommunicationLog(
        user_id=user.id,
        channel=channel,
        recipient=recipient,
        subject=title,
        body=message,
        category=category,
        status=status,
        source_type=source_type,
        source_id=source_id,
        provider='TEST LOG PROVIDER',
        external_id=f'{channel.upper()}-TEST-{utc_now().strftime("%Y%m%d%H%M%S")}-{user.id}',
        error_message=error_message,
        sent_at=utc_now() if status == 'sent' else None,
    )
    db.session.add(log)
    return log


def notify_user(user_id: int, title: str, message: str, notification_type: str = 'info', category: str = 'system', source_type: str = 'system', source_id: int | None = None, channels: tuple[str, ...] = ('email', 'sms', 'push')) -> Notification:
    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        notification_type=notification_type,
        category=category,
        source_type=source_type,
        source_id=source_id,
    )
    db.session.add(notification)
    db.session.flush()
    user = db.session.get(User, user_id)
    if not user:
        return notification
    pref = get_or_create_preferences(user_id)
    if not pref.enabled:
        return notification
    category_allowed = {
        'loan': pref.loan_events,
        'investment': pref.investment_events,
        'payment': pref.payment_events,
        'support': pref.support_events,
        'marketing': pref.marketing_enabled,
        'rating': pref.loan_events,
        'system': True,
    }
    if not category_allowed.get(category, True):
        return notification
    allowed = {
        'email': pref.email_enabled,
        'sms': pref.sms_enabled,
        'push': pref.push_enabled,
    }
    for channel in channels:
        if allowed.get(channel):
            send_test_channel(user, channel, title, message, category=category, source_type=source_type, source_id=source_id)
    return notification


def communications_summary() -> dict:
    total = CommunicationLog.query.count()
    sent = CommunicationLog.query.filter_by(status='sent').count()
    failed = CommunicationLog.query.filter_by(status='failed').count()
    unread = Notification.query.filter_by(is_read=False).count()
    by_channel = {}
    for channel in ['email', 'sms', 'push']:
        by_channel[channel] = CommunicationLog.query.filter_by(channel=channel).count()
    since = utc_now() - timedelta(days=7)
    recent = CommunicationLog.query.filter(CommunicationLog.created_at >= since).count()
    return {
        'notifications': Notification.query.count(),
        'unread': unread,
        'logs_total': total,
        'sent': sent,
        'failed': failed,
        'recent_7d': recent,
        'by_channel': by_channel,
        'templates': MessageTemplate.query.count(),
    }
