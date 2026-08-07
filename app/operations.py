from datetime import datetime, timedelta
import random
from app.time_utils import utc_now
from app import db
from app.models import User, LoanApplication, SupportTicket, SupportMessage, ManualReviewItem, OperatorNote, ResponseTemplate, AuditLog

DEFAULT_TEMPLATES = [
    ('Проверка заявки', 'Здравствуйте! Ваша заявка находится на ручной проверке. Обычно это занимает до 30 минут в demo-режиме.'),
    ('МСИ TEST', 'Идентификация проходит через МСИ TEST. В реальном проекте здесь подключается официальный провайдер, а Kopilka хранит только статус и reference.'),
    ('Платёж в обработке', 'Платёжная операция находится в обработке TEST-шлюза. В реальном контуре статус приходит от банка/платёжного провайдера по webhook.'),
    ('Просрочка', 'По займу есть просрочка. Рекомендуем связаться с поддержкой для согласования продления или погашения.'),
]


def seed_response_templates():
    created = 0
    for title, body in DEFAULT_TEMPLATES:
        if not ResponseTemplate.query.filter_by(title=title).first():
            db.session.add(ResponseTemplate(title=title, body=body, category='support'))
            created += 1
    return created


def create_ticket(user, subject, category='general', priority='medium', message=None, related_application_id=None):
    ticket = SupportTicket(
        user_id=user.id,
        related_application_id=related_application_id,
        subject=subject,
        category=category,
        priority=priority,
        status='Новая',
        sla_due_at=utc_now() + timedelta(hours={'low': 24, 'medium': 8, 'high': 2, 'critical': 1}.get(priority, 8)),
    )
    db.session.add(ticket)
    db.session.flush()
    if message:
        db.session.add(SupportMessage(ticket_id=ticket.id, author_id=user.id, message=message, is_internal=False))
    return ticket


def create_manual_review(application, reason, priority='medium'):
    existing = ManualReviewItem.query.filter_by(application_id=application.id).filter(ManualReviewItem.status.in_(['Новая', 'В работе'])).first()
    if existing:
        return existing
    item = ManualReviewItem(
        application_id=application.id,
        borrower_id=application.user_id,
        reason=reason,
        priority=priority,
        status='Новая',
        sla_due_at=utc_now() + timedelta(hours={'low': 24, 'medium': 8, 'high': 2, 'critical': 1}.get(priority, 8)),
    )
    db.session.add(item)
    return item


def operations_summary():
    now = utc_now()
    return {
        'tickets_total': SupportTicket.query.count(),
        'tickets_open': SupportTicket.query.filter(SupportTicket.status.in_(['Новая', 'В работе', 'Ожидает клиента'])).count(),
        'tickets_critical': SupportTicket.query.filter_by(priority='critical').filter(SupportTicket.status != 'Закрыта').count(),
        'tickets_overdue_sla': SupportTicket.query.filter(SupportTicket.sla_due_at < now, SupportTicket.status != 'Закрыта').count(),
        'manual_queue': ManualReviewItem.query.filter(ManualReviewItem.status.in_(['Новая', 'В работе'])).count(),
        'manual_overdue_sla': ManualReviewItem.query.filter(ManualReviewItem.sla_due_at < now, ManualReviewItem.status != 'Закрыта').count(),
        'operator_notes': OperatorNote.query.count(),
        'templates': ResponseTemplate.query.filter_by(is_active=True).count(),
    }


def seed_demo_operations(admin_user=None):
    seed_response_templates()
    created = 0
    borrowers = User.query.filter_by(role='client').limit(10).all()
    subjects = [
        ('Не вижу статус финансирования заявки', 'loan', 'medium'),
        ('Нужно уточнить платёж по займу', 'payment', 'high'),
        ('Вопрос по МСИ TEST', 'kyc', 'medium'),
        ('Не открывается договор', 'legal', 'low'),
        ('Просьба проверить заявку вручную', 'manual_review', 'high'),
    ]
    for idx, user in enumerate(borrowers[:8]):
        if SupportTicket.query.filter_by(user_id=user.id).first():
            continue
        subject, category, priority = subjects[idx % len(subjects)]
        app = LoanApplication.query.filter_by(user_id=user.id).order_by(LoanApplication.created_at.desc()).first()
        ticket = create_ticket(
            user=user,
            subject=subject,
            category=category,
            priority=priority,
            message=f'DEMO обращение #{idx + 1}: нужна помощь оператора по теме «{subject}».',
            related_application_id=app.id if app else None,
        )
        if idx % 3 == 0 and admin_user:
            ticket.status = 'В работе'
            ticket.assigned_to_id = admin_user.id
            db.session.add(SupportMessage(ticket_id=ticket.id, author_id=admin_user.id, message='Принято в работу. Проверяем данные в TEST-контурах.', is_internal=False))
        created += 1
    for app in LoanApplication.query.filter(LoanApplication.status.in_(['На проверке', 'На витрине', 'Частично профинансирована'])).limit(12).all():
        reason = 'TEST очередь: дополнительная проверка заявки, скоринга или compliance-флагов перед сделкой.'
        item = create_manual_review(app, reason=reason, priority=random.choice(['medium', 'high', 'low']))
        if admin_user and item.id and random.random() > 0.5:
            item.assigned_to_id = admin_user.id
            item.status = 'В работе'
    db.session.add(AuditLog(actor_id=admin_user.id if admin_user else None, action='operations_demo_seeded', entity='SupportTicket', entity_id=None))
    return created
