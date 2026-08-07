from datetime import datetime, timedelta
from decimal import Decimal
import uuid
from app.time_utils import utc_now
from app import db
from app.models import Loan, User, CollectionCase, CollectionAction, Notification, AuditLog
from app.communications import notify_user


def collection_stage_for_days(days: int) -> str:
    if days >= 30:
        return 'partner_transfer'
    if days >= 15:
        return 'claim'
    if days >= 5:
        return 'warning'
    return 'reminder'


def collection_title_for_stage(stage: str) -> str:
    return {
        'reminder': 'Мягкое напоминание по просрочке',
        'warning': 'Предупреждение о просрочке',
        'claim': 'Досудебная претензия TEST',
        'partner_transfer': 'Передача партнёру TEST',
    }.get(stage, 'Работа с просрочкой')


def ensure_collection_case(loan: Loan, actor=None):
    if loan.overdue_days <= 0 and loan.status != 'Просрочен':
        return None
    case = CollectionCase.query.filter_by(loan_id=loan.id).first()
    stage = collection_stage_for_days(loan.overdue_days)
    if not case:
        case = CollectionCase(
            loan_id=loan.id,
            borrower_id=loan.user_id,
            stage=stage,
            overdue_days=loan.overdue_days,
            outstanding_amount=loan.balance,
            next_action_at=utc_now() + timedelta(days=1),
            external_partner_ref=f'COLL-TEST-{uuid.uuid4().hex[:10].upper()}' if stage == 'partner_transfer' else None,
        )
        db.session.add(case)
        db.session.flush()
        add_collection_action(case, actor, 'case_created', 'system', 'Открыто collection-дело', f'Займ #{loan.id}, просрочка {loan.overdue_days} дн., остаток {loan.balance} BYN')
    else:
        if case.status == 'Закрыта':
            return case
        old_stage = case.stage
        case.stage = stage
        case.overdue_days = loan.overdue_days
        case.outstanding_amount = loan.balance
        case.updated_at = utc_now()
        if stage == 'partner_transfer' and not case.external_partner_ref:
            case.external_partner_ref = f'COLL-TEST-{uuid.uuid4().hex[:10].upper()}'
        if old_stage != stage:
            add_collection_action(case, actor, 'stage_changed', 'system', 'Изменён этап взыскания', f'{old_stage} → {stage}')
    return case


def add_collection_action(case: CollectionCase, actor, action_type: str, channel: str, title: str, message: str = '', status='Создано'):
    action = CollectionAction(
        case_id=case.id,
        actor_id=getattr(actor, 'id', None),
        action_type=action_type,
        channel=channel,
        title=title,
        message=message,
        status=status,
        external_ref=f'COLL-ACT-{uuid.uuid4().hex[:10].upper()}'
    )
    db.session.add(action)
    return action


def run_collections_cycle(actor=None):
    loans = Loan.query.filter(Loan.status.in_(['Активный', 'Просрочен'])).all()
    created_or_updated = 0
    for loan in loans:
        if loan.overdue_days <= 0 and loan.status != 'Просрочен':
            continue
        loan.status = 'Просрочен'
        if loan.application:
            loan.application.status = 'Просрочен'
        case = ensure_collection_case(loan, actor)
        if not case or case.status == 'Закрыта':
            continue
        created_or_updated += 1
        last_auto_action = CollectionAction.query.filter(
            CollectionAction.case_id == case.id,
            CollectionAction.action_type.like('auto_%'),
        ).order_by(CollectionAction.created_at.desc()).first()
        expected_action_type = f'auto_{case.stage}'
        action_due = not case.next_action_at or case.next_action_at <= utc_now()
        stage_changed = not last_auto_action or last_auto_action.action_type != expected_action_type
        if action_due or stage_changed:
            auto_action_for_case(case, actor)
    if actor:
        db.session.add(AuditLog(actor_id=actor.id, action='collections_cycle_run', entity='CollectionCase', entity_id=None))
    return created_or_updated


def auto_action_for_case(case: CollectionCase, actor=None):
    stage = case.stage
    title = collection_title_for_stage(stage)
    borrower = case.borrower
    loan = case.loan
    if stage == 'reminder':
        message = f'По займу #{loan.id} просрочка {case.overdue_days} дн. Остаток к оплате: {case.outstanding_amount} BYN. Это TEST-уведомление, реальные SMS/email не отправлялись.'
        channel = 'push/email TEST'
    elif stage == 'warning':
        message = f'Просрочка по займу #{loan.id} составляет {case.overdue_days} дн. Рекомендуем внести платёж или обратиться в поддержку для урегулирования.'
        channel = 'sms/email TEST'
    elif stage == 'claim':
        message = f'Сформирована TEST-претензия по займу #{loan.id}. Остаток: {case.outstanding_amount} BYN. В реальном проекте документ подписывается/направляется по утверждённому юридическому процессу.'
        channel = 'legal notice TEST'
    else:
        message = f'Дело по займу #{loan.id} подготовлено к TEST-передаче партнёру. Reference: {case.external_partner_ref}. В реальном проекте передача возможна только по договору и требованиям законодательства.'
        channel = 'partner API TEST'
    add_collection_action(case, actor, f'auto_{stage}', channel, title, message, status='Успешно')
    notify_user(borrower.id, title, message, notification_type='warning', category='loan', source_type='collection_case', source_id=case.id, channels=('email','sms','push'))
    case.next_action_at = utc_now() + timedelta(days=3 if stage == 'reminder' else 5)


def close_collection_case(case: CollectionCase, actor=None, comment='Закрыто оператором'):
    if case.status == 'Закрыта':
        return case
    case.status = 'Закрыта'
    case.stage = 'closed'
    case.closed_at = utc_now()
    add_collection_action(case, actor, 'case_closed', 'operator', 'Collection-дело закрыто', comment, status='Закрыто')
    if actor:
        db.session.add(AuditLog(actor_id=actor.id, action='collection_case_closed', entity='CollectionCase', entity_id=case.id))
    return case


def collections_summary():
    cases = CollectionCase.query.all()
    return {
        'cases_total': len(cases),
        'open_cases': sum(1 for c in cases if c.status != 'Закрыта'),
        'reminder': sum(1 for c in cases if c.stage == 'reminder'),
        'warning': sum(1 for c in cases if c.stage == 'warning'),
        'claim': sum(1 for c in cases if c.stage == 'claim'),
        'partner_transfer': sum(1 for c in cases if c.stage == 'partner_transfer'),
        'outstanding': sum((Decimal(c.outstanding_amount or 0) for c in cases if c.status != 'Закрыта'), Decimal('0.00')),
        'actions': CollectionAction.query.count(),
    }


def seed_demo_collections(actor=None):
    # Использует уже созданные демо-займы и не создаёт фиктивные персональные данные.
    created = run_collections_cycle(actor)
    return created


