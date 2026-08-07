from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from app.time_utils import utc_now
from app import db
from app.models import (
    User, ClientProfile, LoanApplication, Loan, Investment, Wallet,
    ComplianceCase, ComplianceFlag, ComplianceDecision, AuditLog, Notification
)


RISK_POINTS = {
    'msi_not_verified': 35,
    'card_not_verified': 20,
    'consent_missing': 25,
    'high_amount': 20,
    'high_debt_load': 25,
    'overdue_history': 35,
    'low_scoring': 30,
    'very_new_account': 15,
    'many_recent_applications': 20,
    'high_risk_application': 25,
    'investor_large_wallet': 15,
}


def _money(value) -> Decimal:
    return Decimal(str(value or 0))


def _add_flag(flags, code, title, severity, description, source='TEST rule engine'):
    flags.append({
        'code': code,
        'title': title,
        'severity': severity,
        'description': description,
        'source': source,
        'points': RISK_POINTS.get(code, 0),
    })


def build_checklist(user: User, application: LoanApplication | None = None) -> tuple[list[dict], list[dict], int, str]:
    """Тестовая логика проверки клиентов. Реальные проверки должны выполняться через МСИ, банк, провайдера противодействия мошенничеству и официальные реестры."""
    profile: ClientProfile | None = user.profile
    flags: list[dict] = []

    checklist = [
        {
            'name': 'МСИ / официальная идентификация',
            'status': 'ok' if profile and profile.msi_verified else 'fail',
            'provider': (profile.msi_provider if profile else 'МСИ TEST'),
            'note': 'На платформе хранится только статус проверки и reference, без паспортных данных.',
        },
        {
            'name': 'Токенизация платёжного инструмента',
            'status': 'ok' if profile and profile.card_verified and profile.card_token else 'fail',
            'provider': 'BANK/PAYMENT TEST',
            'note': 'Полный номер карты не хранится, только mask + token.',
        },
        {
            'name': 'Согласия пользователя',
            'status': 'ok' if profile and profile.consent_personal_data and profile.consent_scoring and profile.consent_msi else 'fail',
            'provider': 'Kopilka Legal Core TEST',
            'note': 'В реальности нужны версии согласий, timestamp и audit trail подписания.',
        },
        {
            'name': 'Санкционные / PEP / black-list проверки',
            'status': 'ok',
            'provider': 'SANCTIONS/PEP TEST',
            'note': 'В demo-режиме имитируется. В реальности подключается официальный AML-провайдер/банк.',
        },
        {
            'name': 'Device/IP антифрод',
            'status': 'manual',
            'provider': 'ANTIFRAUD TEST',
            'note': 'Demo-флаги строятся по косвенным признакам. В production нужны fingerprint, velocity-rules и журнал сессий.',
        },
    ]

    if not profile or not profile.msi_verified:
        _add_flag(flags, 'msi_not_verified', 'Нет подтверждения МСИ', 'danger', 'Пользователь не должен выходить на сделку без официальной идентификации.', 'MSI TEST')
    if not profile or not profile.card_verified or not profile.card_token:
        _add_flag(flags, 'card_not_verified', 'Нет токенизированной карты', 'warning', 'Не найден подтверждённый платёжный инструмент.', 'PAYMENT TOKEN TEST')
    if not profile or not (profile.consent_personal_data and profile.consent_scoring and profile.consent_msi):
        _add_flag(flags, 'consent_missing', 'Не все согласия получены', 'danger', 'Нельзя выполнять скоринг/МСИ без явного согласия.', 'LEGAL TEST')

    if user.created_at and user.created_at > utc_now() - timedelta(days=3):
        _add_flag(flags, 'very_new_account', 'Очень новый аккаунт', 'info', 'Аккаунт создан недавно — нужен дополнительный мониторинг.', 'VELOCITY TEST')

    if user.is_borrower:
        recent_apps = LoanApplication.query.filter(
            LoanApplication.user_id == user.id,
            LoanApplication.created_at >= utc_now() - timedelta(days=30)
        ).count()
        if recent_apps >= 5:
            _add_flag(flags, 'many_recent_applications', 'Много заявок за 30 дней', 'warning', f'Найдено заявок за 30 дней: {recent_apps}.', 'VELOCITY TEST')
        overdue_loans = Loan.query.filter_by(user_id=user.id, status='Просрочен').count()
        if overdue_loans:
            _add_flag(flags, 'overdue_history', 'Есть просрочки', 'danger', f'Активных/исторических просрочек: {overdue_loans}.', 'CREDIT HISTORY TEST')
        if application:
            income = _money(profile.monthly_income if profile else 0)
            expenses = _money(profile.monthly_expenses if profile else 0)
            amount = _money(application.amount)
            if amount >= Decimal('2500'):
                _add_flag(flags, 'high_amount', 'Крупная заявка для P2P', 'warning', f'Сумма заявки: {amount} BYN.', 'LIMIT TEST')
            if income and (expenses / income) > Decimal('0.65'):
                _add_flag(flags, 'high_debt_load', 'Высокая долговая/расходная нагрузка', 'warning', 'Расходы превышают 65% дохода.', 'SCORING TEST')
            if int(application.scoring_score or 0) < 600:
                _add_flag(flags, 'low_scoring', 'Низкий scoring score', 'danger', f'Скоринг: {application.scoring_score or 0}/1000.', 'SCORING TEST')
            if application.risk_level == 'Высокий':
                _add_flag(flags, 'high_risk_application', 'Высокий риск заявки', 'danger', 'Заявка требует ручного решения compliance/risk.', 'RISK TEST')
    elif user.is_investor:
        wallet: Wallet | None = user.wallet
        if wallet and _money(wallet.balance) >= Decimal('50000'):
            _add_flag(flags, 'investor_large_wallet', 'Крупный баланс займодавца', 'info', 'Для production потребуется enhanced due diligence источника средств.', 'EDD TEST')

    risk_score = min(100, sum(item.get('points', 0) for item in flags))
    if risk_score >= 65:
        risk_level = 'Высокий'
    elif risk_score >= 30:
        risk_level = 'Средний'
    else:
        risk_level = 'Низкий'

    return checklist, flags, risk_score, risk_level


def run_compliance_check(user: User, application: LoanApplication | None = None, actor: User | None = None) -> ComplianceCase:
    checklist, flags, risk_score, risk_level = build_checklist(user, application)
    status = 'Требует решения' if risk_level in ['Средний', 'Высокий'] else 'Пройдено'
    case = ComplianceCase(
        user_id=user.id,
        application_id=application.id if application else None,
        subject_role='investor' if user.is_investor else 'borrower',
        status=status,
        risk_level=risk_level,
        risk_score=risk_score,
        checklist_json=json.dumps(checklist, ensure_ascii=False, indent=2),
        provider_reference='COMPLIANCE-TEST-' + uuid.uuid4().hex[:10].upper(),
        summary='TEST AML/KYC проверка. Паспортные данные, сканы и полные реквизиты карт не хранятся в Kopilka.',
    )
    db.session.add(case)
    db.session.flush()
    for flag in flags:
        db.session.add(ComplianceFlag(
            case_id=case.id,
            code=flag['code'],
            title=flag['title'],
            severity=flag['severity'],
            description=flag['description'],
            source=flag['source'],
        ))
    if actor:
        db.session.add(AuditLog(actor_id=actor.id, action='compliance_check_run_test', entity='ComplianceCase', entity_id=case.id))
    return case


def run_bulk_compliance(actor: User | None = None) -> int:
    created = 0
    # Проверяем последние заявки и всех демо-займодавцев.
    for app in LoanApplication.query.order_by(LoanApplication.created_at.desc()).limit(120).all():
        run_compliance_check(app.user, app, actor)
        created += 1
    for investor in User.query.filter_by(role='investor').limit(80).all():
        run_compliance_check(investor, None, actor)
        created += 1
    db.session.commit()
    return created


def compliance_summary() -> dict:
    total = ComplianceCase.query.count()
    high = ComplianceCase.query.filter_by(risk_level='Высокий').count()
    medium = ComplianceCase.query.filter_by(risk_level='Средний').count()
    low = ComplianceCase.query.filter_by(risk_level='Низкий').count()
    pending = ComplianceCase.query.filter(ComplianceCase.status.in_(['Новая', 'Требует решения'])).count()
    sanctions = ComplianceFlag.query.filter(ComplianceFlag.code.in_(['sanctions_match', 'pep_match'])).count()
    return {
        'total': total,
        'high': high,
        'medium': medium,
        'low': low,
        'pending': pending,
        'sanctions': sanctions,
    }


def decide_case(case: ComplianceCase, actor: User, decision: str, comment: str = '') -> ComplianceDecision:
    mapping = {
        'approve': 'Пройдено',
        'manual_review': 'На ручной проверке',
        'reject': 'Отклонено',
        'freeze': 'Заморожено',
    }
    case.status = mapping.get(decision, 'На ручной проверке')
    case.reviewed_at = utc_now()
    case.reviewed_by_id = actor.id
    item = ComplianceDecision(case_id=case.id, actor_id=actor.id, decision=decision, comment=comment)
    db.session.add(item)
    db.session.add(AuditLog(actor_id=actor.id, action=f'compliance_decision_{decision}', entity='ComplianceCase', entity_id=case.id))
    db.session.add(Notification(
        user_id=case.user_id,
        title='Compliance TEST решение',
        message=f'По вашей проверке принято решение: {case.status}. Это demo-контур без хранения паспортных данных.',
        notification_type='info'
    ))
    return item
