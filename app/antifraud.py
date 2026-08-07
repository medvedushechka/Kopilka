from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import random
import uuid

from app.time_utils import utc_now
from app import db
from app.models import (
    User, LoanApplication, Investment, WalletTransaction, AuditLog,
    AntifraudEvent, AntifraudDecision, Notification
)


FRAUD_RULES = {
    'many_applications': 'Частые заявки за короткий период',
    'shared_ip': 'Несколько аккаунтов с одного IP',
    'device_change': 'Подозрительная смена устройства',
    'large_investment': 'Аномально крупная инвестиция',
    'payment_anomaly': 'Нетипичное движение по кошельку',
    'high_risk_borrower': 'Высокий риск заёмщика',
}


def _fingerprint_for(user):
    raw = f'{user.email}|{user.created_at}|KOPILKA-AF-TEST'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16].upper()


def _demo_ip_for(user):
    # Тестовая имитация: часть демо-пользователей специально попадает в одинаковые IP-группы.
    if '@kopilka.test' in user.email:
        try:
            num = int(''.join(ch for ch in user.email if ch.isdigit()) or '1')
        except ValueError:
            num = 1
        if num in (2, 5, 8):
            return '10.20.30.77'
        if num in (3, 6):
            return '10.20.30.88'
    return '10.20.30.' + str((user.id % 180) + 20)


def _severity(score):
    if score >= 75:
        return 'danger'
    if score >= 45:
        return 'warning'
    return 'info'


def create_antifraud_event(user, event_type, score, title, description, application=None, ip_address=None, device_fingerprint=None):
    event = AntifraudEvent(
        user_id=user.id if user else None,
        application_id=application.id if application else None,
        event_type=event_type,
        severity=_severity(score),
        fraud_score=int(score),
        title=title,
        description=description,
        ip_address=ip_address or (_demo_ip_for(user) if user else None),
        device_fingerprint=device_fingerprint or (_fingerprint_for(user) if user else None),
        provider_reference='AF-TEST-' + uuid.uuid4().hex[:10].upper(),
    )
    db.session.add(event)
    db.session.flush()
    return event


def run_user_antifraud(user, actor=None):
    created = []
    now = utc_now()
    last_30d = now - timedelta(days=30)

    apps = LoanApplication.query.filter_by(user_id=user.id).all()
    recent_apps = [a for a in apps if a.created_at and a.created_at >= last_30d]
    high_risk_apps = [a for a in apps if (a.risk_level == 'Высокий' or (a.scoring_score or 0) < 560)]

    if len(recent_apps) >= 3:
        created.append(create_antifraud_event(
            user, 'many_applications', min(95, 40 + len(recent_apps) * 12),
            FRAUD_RULES['many_applications'],
            f'За последние 30 дней создано заявок: {len(recent_apps)}. В реальном проекте проверяется частота заявок, отказы, повторные попытки и совпадения устройств.',
            application=recent_apps[-1]
        ))

    demo_ip = _demo_ip_for(user)
    same_ip_users = [u for u in User.query.filter(User.role.in_(['client', 'investor'])).all() if _demo_ip_for(u) == demo_ip]
    if len(same_ip_users) >= 3:
        created.append(create_antifraud_event(
            user, 'shared_ip', min(90, 35 + len(same_ip_users) * 10),
            FRAUD_RULES['shared_ip'],
            f'TEST-правило нашло {len(same_ip_users)} аккаунта(ов) с IP {demo_ip}. В production берётся реальный IP, device fingerprint, cookies, поведенческие признаки и провайдер антифрода.',
            ip_address=demo_ip
        ))

    if high_risk_apps:
        app = high_risk_apps[-1]
        created.append(create_antifraud_event(
            user, 'high_risk_borrower', 70 if app.risk_level == 'Высокий' else 52,
            FRAUD_RULES['high_risk_borrower'],
            f'Заявка #{app.id}: рейтинг {app.rating_grade}, scoring {app.scoring_score or 0}, риск {app.risk_level or "не указан"}.',
            application=app
        ))

    if user.is_investor:
        investments = Investment.query.filter_by(investor_id=user.id).all()
        if investments:
            amounts = [Decimal(i.amount or 0) for i in investments]
            max_amount = max(amounts)
            avg_amount = sum(amounts, Decimal('0')) / Decimal(len(amounts))
            if max_amount > avg_amount * Decimal('3') and max_amount >= Decimal('800'):
                inv = sorted(investments, key=lambda i: i.amount or 0)[-1]
                created.append(create_antifraud_event(
                    user, 'large_investment', 58,
                    FRAUD_RULES['large_investment'],
                    f'Инвестиция #{inv.id} на {inv.amount} BYN заметно выше среднего размера инвестиций пользователя ({avg_amount.quantize(Decimal("0.01"))} BYN).',
                    application=inv.application
                ))

    tx_count = WalletTransaction.query.filter_by(user_id=user.id).count()
    if tx_count >= 25:
        created.append(create_antifraud_event(
            user, 'payment_anomaly', 48,
            FRAUD_RULES['payment_anomaly'],
            f'У пользователя много wallet-операций: {tx_count}. В production проверяется скорость операций, связки карт/кошельков и возвраты платежей.'
        ))

    if actor:
        db.session.add(AuditLog(actor_id=actor.id, action='antifraud_user_scan_test', entity='User', entity_id=user.id))
    return created


def run_bulk_antifraud(actor=None, limit=80):
    created = 0
    for user in User.query.filter(User.role.in_(['client', 'investor'])).limit(limit).all():
        created += len(run_user_antifraud(user, actor))
    return created


def seed_demo_antifraud(actor=None):
    # Создаём события только один раз, чтобы повторное заполнение базы не плодило дубликаты.
    if AntifraudEvent.query.count():
        return 0
    return run_bulk_antifraud(actor)


def decide_antifraud_event(event, actor, decision, comment=''):
    event.status = {
        'approve': 'Закрыто: норма',
        'monitor': 'Мониторинг',
        'restrict': 'Ограничено',
        'block': 'Блокировка',
    }.get(decision, 'Рассмотрено')
    event.reviewed_at = utc_now()
    event.reviewed_by_id = actor.id
    db.session.add(AntifraudDecision(event_id=event.id, actor_id=actor.id, decision=decision, comment=comment))
    db.session.add(AuditLog(actor_id=actor.id, action=f'antifraud_decision_{decision}', entity='AntifraudEvent', entity_id=event.id))
    if event.user_id and decision in ['restrict', 'block']:
        user = db.session.get(User, event.user_id)
        if user and decision == 'block':
            user.is_blocked = True
        db.session.add(Notification(
            user_id=event.user_id,
            title='Ограничение безопасности',
            message=(
                'Аккаунт заблокирован до решения администратора. Это TEST-уведомление антифрод-контура.'
                if decision == 'block'
                else 'По аккаунту включена дополнительная проверка безопасности. Это TEST-уведомление антифрод-контура.'
            ),
            notification_type='warning',
            category='system',
            source_type='AntifraudEvent',
            source_id=event.id,
        ))
    return event


def antifraud_summary():
    total = AntifraudEvent.query.count()
    high = AntifraudEvent.query.filter_by(severity='danger').count()
    medium = AntifraudEvent.query.filter_by(severity='warning').count()
    low = AntifraudEvent.query.filter_by(severity='info').count()
    open_events = AntifraudEvent.query.filter(AntifraudEvent.status.in_(['Новое', 'Мониторинг'])).count()
    avg_score = db.session.query(db.func.coalesce(db.func.avg(AntifraudEvent.fraud_score), 0)).scalar() or 0
    return {
        'total': total,
        'open': open_events,
        'high': high,
        'medium': medium,
        'low': low,
        'avg_score': round(float(avg_score), 1),
        'shared_ip': AntifraudEvent.query.filter_by(event_type='shared_ip').count(),
        'payment_anomaly': AntifraudEvent.query.filter_by(event_type='payment_anomaly').count(),
    }
