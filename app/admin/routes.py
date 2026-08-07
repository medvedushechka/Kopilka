from datetime import datetime, timedelta, date
import uuid
from decimal import Decimal, ROUND_HALF_UP
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, current_app
from flask_login import login_required, current_user
from app.time_utils import utc_now
from app import db
from app.http_utils import redirect_back
from app.demo_seed import create_demo_data
from app.finance import (
    BORROWER_PAYOUT, ESCROW_INVESTMENTS, add_gateway_operation, add_ledger,
    add_platform_fee, finance_summary, new_external_id, reconciliation_report,
    rebuild_finance_ledger_from_existing_data,
)
from app.legal import seed_legal_templates, ensure_loan_documents, sign_document_test, legal_summary
from app.compliance import compliance_summary, run_bulk_compliance, run_compliance_check, decide_case
from app.operations import operations_summary, seed_response_templates, seed_demo_operations, create_manual_review
from app.communications import notify_user, seed_message_templates, communications_summary
from app.antifraud import antifraud_summary, run_bulk_antifraud, run_user_antifraud, decide_antifraud_event, seed_demo_antifraud
from app.collections import collections_summary, run_collections_cycle, close_collection_case, add_collection_action, seed_demo_collections
from app.models import User, LoanApplication, Loan, Transaction, AuditLog, Notification, CreditHistory, Investment, PlatformLedger, LedgerEntry, PaymentGatewayOperation, WalletTransaction, LegalDocumentTemplate, DealDocument, SignatureEvent, ComplianceCase, ComplianceFlag, ComplianceDecision, SupportTicket, SupportMessage, ManualReviewItem, OperatorNote, ResponseTemplate, CommunicationLog, MessageTemplate, NotificationPreference, AntifraudEvent, AntifraudDecision, CollectionCase, CollectionAction

admin_bp = Blueprint('admin', __name__)

ALLOWED_TICKET_STATUSES = {'Новая', 'В работе', 'Ожидает клиента', 'Закрыта'}
ALLOWED_TICKET_PRIORITIES = {'low', 'medium', 'high', 'critical'}


def admin_required():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


def notify(user_id, title, message, notification_type='info'):
    category = 'system'
    text = (title + ' ' + message).lower()
    if 'инвест' in text or 'профинанс' in text:
        category = 'investment'
    elif 'плат' in text or 'кошел' in text or 'деньг' in text or 'возврат' in text:
        category = 'payment'
    elif 'поддерж' in text or 'обращен' in text:
        category = 'support'
    elif 'заявк' in text or 'займ' in text or 'договор' in text:
        category = 'loan'
    elif 'рейтинг' in text or 'скоринг' in text:
        category = 'rating'
    return notify_user(user_id, title, message, notification_type=notification_type, category=category)


def set_ticket_status(ticket, requested_status):
    if requested_status not in ALLOWED_TICKET_STATUSES:
        return False
    ticket.status = requested_status
    ticket.closed_at = utc_now() if requested_status == 'Закрыта' else None
    return True


def get_or_create_credit_history(user_id):
    history = CreditHistory.query.filter_by(user_id=user_id).first()
    if not history:
        history = CreditHistory(user_id=user_id)
        db.session.add(history)
        db.session.flush()
    return history


@admin_bp.route('/')
@login_required
def dashboard():
    admin_required()
    stats = {
        'clients': User.query.filter_by(role='client').count(),
        'investors': User.query.filter_by(role='investor').count(),
        'marketplace': LoanApplication.query.filter(LoanApplication.status.in_(['На витрине', 'Частично профинансирована'])).count(),
        'investments': Investment.query.count(),
        'platform_income': db.session.query(db.func.coalesce(db.func.sum(PlatformLedger.amount), 0)).scalar(),
        'applications': LoanApplication.query.count(),
        'pending': LoanApplication.query.filter_by(status='На проверке').count(),
        'approved': LoanApplication.query.filter_by(status='Одобрено').count(),
        'active_loans': Loan.query.filter_by(status='Активный').count(),
        'closed_loans': Loan.query.filter_by(status='Закрыт').count(),
        'transactions_processing': Transaction.query.filter_by(status='В обработке').count(),
        'gateway_processing': PaymentGatewayOperation.query.filter(PaymentGatewayOperation.status.in_(['Создан', 'В обработке'])).count(),
        'ledger_entries': LedgerEntry.query.count(),
        'overdue_loans': Loan.query.filter(Loan.due_date < date.today(), Loan.status.in_(['Активный', 'Просрочен'])).count(),
        'low_risk': LoanApplication.query.filter_by(risk_level='Низкий').count(),
        'legal_documents': DealDocument.query.count(),
        'legal_waiting': DealDocument.query.filter_by(status='Ожидает подписи').count(),
        'compliance_pending': ComplianceCase.query.filter(ComplianceCase.status.in_(['Новая', 'Требует решения'])).count(),
        'compliance_high': ComplianceCase.query.filter_by(risk_level='Высокий').count(),
        'support_open': SupportTicket.query.filter(SupportTicket.status.in_(['Новая', 'В работе', 'Ожидает клиента'])).count(),
        'manual_queue': ManualReviewItem.query.filter(ManualReviewItem.status.in_(['Новая', 'В работе'])).count(),
        'antifraud_open': AntifraudEvent.query.filter(AntifraudEvent.status.in_(['Новое', 'Мониторинг'])).count(),
        'antifraud_high': AntifraudEvent.query.filter_by(severity='danger').count(),
        'collections_open': CollectionCase.query.filter(CollectionCase.status != 'Закрыта').count(),
        'collections_partner': CollectionCase.query.filter_by(stage='partner_transfer').count(),
    }
    applications = LoanApplication.query.order_by(LoanApplication.created_at.desc()).limit(80).all()
    active_loans = Loan.query.filter(Loan.status.in_(['Активный', 'Просрочен'])).order_by(Loan.due_date.asc()).limit(30).all()
    transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(20).all()
    return render_template('admin/dashboard.html', stats=stats, applications=applications, active_loans=active_loans, transactions=transactions)



def _money(value):
    return Decimal(value or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _percent(part, total):
    part = Decimal(part or 0)
    total = Decimal(total or 0)
    if total <= 0:
        return Decimal('0.00')
    return (part / total * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


@admin_bp.route('/bi')
@login_required
def bi_dashboard():
    admin_required()

    today = date.today()
    month_start = date(today.year, today.month, 1)

    users_total = User.query.count()
    borrowers_count = User.query.filter_by(role='client').count()
    investors_count = User.query.filter_by(role='investor').count()
    applications_count = LoanApplication.query.count()
    loans_count = Loan.query.count()
    active_loans_count = Loan.query.filter(Loan.status.in_(['Активный', 'Просрочен'])).count()
    closed_loans_count = Loan.query.filter_by(status='Закрыт').count()
    overdue_loans_count = Loan.query.filter(
        db.or_(Loan.status == 'Просрочен', db.and_(Loan.due_date < today, Loan.status == 'Активный'))
    ).count()

    issued_total = _money(db.session.query(db.func.coalesce(db.func.sum(Loan.principal), 0)).scalar())
    repaid_total = _money(db.session.query(db.func.coalesce(db.func.sum(Loan.repaid_amount), 0)).scalar())
    outstanding_total = _money(sum((loan.balance for loan in Loan.query.filter(Loan.status.in_(['Активный', 'Просрочен'])).all()), Decimal('0.00')))
    overdue_amount = _money(sum((loan.balance for loan in Loan.query.filter(db.or_(Loan.status == 'Просрочен', db.and_(Loan.due_date < today, Loan.status == 'Активный'))).all()), Decimal('0.00')))

    investments_total = _money(db.session.query(db.func.coalesce(db.func.sum(Investment.amount), 0)).filter(Investment.status.in_(['Активна', 'Завершена'])).scalar())
    active_investments_total = _money(db.session.query(db.func.coalesce(db.func.sum(Investment.amount), 0)).filter_by(status='Активна').scalar())
    expected_returns_total = _money(db.session.query(db.func.coalesce(db.func.sum(Investment.expected_return), 0)).scalar())
    investor_expected_profit = _money(expected_returns_total - investments_total)

    platform_income_total = _money(db.session.query(db.func.coalesce(db.func.sum(PlatformLedger.amount), 0)).scalar())
    investor_fees_total = _money(db.session.query(db.func.coalesce(db.func.sum(Investment.platform_fee), 0)).scalar())
    borrower_fees_total = _money(sum((app.platform_borrower_fee for app in LoanApplication.query.filter(LoanApplication.status.in_(['Активный займ', 'Закрыт', 'Просрочен'])).all()), Decimal('0.00')))
    month_income = _money(db.session.query(db.func.coalesce(db.func.sum(PlatformLedger.amount), 0)).filter(PlatformLedger.created_at >= datetime.combine(month_start, datetime.min.time())).scalar())

    conversion_funded = _percent(LoanApplication.query.filter(LoanApplication.status.in_(['Активный займ', 'Закрыт', 'Просрочен'])).count(), applications_count)
    default_rate = _percent(overdue_loans_count, active_loans_count + closed_loans_count)
    collection_rate = _percent(CollectionCase.query.filter_by(status='Закрыта').count(), CollectionCase.query.count())
    avg_loan_amount = _money(issued_total / Decimal(loans_count)) if loans_count else Decimal('0.00')
    avg_term_days = int(db.session.query(db.func.coalesce(db.func.avg(LoanApplication.term_days), 0)).scalar() or 0)
    avg_investor_yield = _percent(investor_expected_profit, investments_total)

    status_rows = db.session.query(LoanApplication.status, db.func.count(LoanApplication.id)).group_by(LoanApplication.status).all()
    rating_counter = {}
    for app in LoanApplication.query.all():
        rating_counter[app.rating_grade] = rating_counter.get(app.rating_grade, 0) + 1
    rating_order = ['A+', 'A', 'B+', 'B', 'C', 'D']
    risk_rows = db.session.query(LoanApplication.risk_level, db.func.count(LoanApplication.id)).group_by(LoanApplication.risk_level).all()

    issued_months = db.session.query(db.func.strftime('%Y-%m', Loan.issued_at), db.func.coalesce(db.func.sum(Loan.principal), 0)).group_by(db.func.strftime('%Y-%m', Loan.issued_at)).order_by(db.func.strftime('%Y-%m', Loan.issued_at)).all()
    income_months = db.session.query(db.func.strftime('%Y-%m', PlatformLedger.created_at), db.func.coalesce(db.func.sum(PlatformLedger.amount), 0)).group_by(db.func.strftime('%Y-%m', PlatformLedger.created_at)).order_by(db.func.strftime('%Y-%m', PlatformLedger.created_at)).all()
    investments_months = db.session.query(db.func.strftime('%Y-%m', Investment.created_at), db.func.coalesce(db.func.sum(Investment.amount), 0)).group_by(db.func.strftime('%Y-%m', Investment.created_at)).order_by(db.func.strftime('%Y-%m', Investment.created_at)).all()

    top_investors = db.session.query(User, db.func.coalesce(db.func.sum(Investment.amount), 0).label('total'), db.func.count(Investment.id).label('count')).join(Investment, Investment.investor_id == User.id).group_by(User.id).order_by(db.desc('total')).limit(8).all()
    top_borrowers = db.session.query(User, db.func.coalesce(db.func.sum(Loan.principal), 0).label('total'), db.func.count(Loan.id).label('count')).join(Loan, Loan.user_id == User.id).group_by(User.id).order_by(db.desc('total')).limit(8).all()

    system_health = {
        'payment_errors': PaymentGatewayOperation.query.filter(PaymentGatewayOperation.status.in_(['Ошибка', 'failed', 'failed_callback', 'manual_review'])).count(),
        'gateway_processing': PaymentGatewayOperation.query.filter(PaymentGatewayOperation.status.in_(['Создан', 'В обработке'])).count(),
        'support_open': SupportTicket.query.filter(SupportTicket.status.in_(['Новая', 'В работе', 'Ожидает клиента'])).count(),
        'manual_review': ManualReviewItem.query.filter(ManualReviewItem.status.in_(['Новая', 'В работе'])).count(),
        'compliance_open': ComplianceCase.query.filter(ComplianceCase.status.in_(['Новая', 'Требует решения', 'На ручной проверке'])).count(),
        'fraud_open': AntifraudEvent.query.filter(AntifraudEvent.status.in_(['Новое', 'Мониторинг'])).count(),
        'collections_open': CollectionCase.query.filter(CollectionCase.status != 'Закрыта').count(),
        'pending_signatures': DealDocument.query.filter_by(status='Ожидает подписи').count(),
    }

    kpis = {
        'users_total': users_total,
        'borrowers_count': borrowers_count,
        'investors_count': investors_count,
        'applications_count': applications_count,
        'loans_count': loans_count,
        'active_loans_count': active_loans_count,
        'closed_loans_count': closed_loans_count,
        'overdue_loans_count': overdue_loans_count,
        'issued_total': issued_total,
        'repaid_total': repaid_total,
        'outstanding_total': outstanding_total,
        'overdue_amount': overdue_amount,
        'investments_total': investments_total,
        'active_investments_total': active_investments_total,
        'platform_income_total': platform_income_total,
        'investor_fees_total': investor_fees_total,
        'borrower_fees_total': borrower_fees_total,
        'month_income': month_income,
        'conversion_funded': conversion_funded,
        'default_rate': default_rate,
        'collection_rate': collection_rate,
        'avg_loan_amount': avg_loan_amount,
        'avg_term_days': avg_term_days,
        'avg_investor_yield': avg_investor_yield,
        'investor_expected_profit': investor_expected_profit,
    }

    charts = {
        'issued_months': {'labels': [r[0] or '—' for r in issued_months], 'values': [float(r[1] or 0) for r in issued_months]},
        'income_months': {'labels': [r[0] or '—' for r in income_months], 'values': [float(r[1] or 0) for r in income_months]},
        'investments_months': {'labels': [r[0] or '—' for r in investments_months], 'values': [float(r[1] or 0) for r in investments_months]},
        'statuses': {'labels': [r[0] or 'Без статуса' for r in status_rows], 'values': [int(r[1]) for r in status_rows]},
        'ratings': {'labels': rating_order, 'values': [rating_counter.get(r, 0) for r in rating_order]},
        'risks': {'labels': [r[0] or 'Не указан' for r in risk_rows], 'values': [int(r[1]) for r in risk_rows]},
    }

    return render_template(
        'admin/bi.html',
        kpis=kpis,
        charts=charts,
        system_health=system_health,
        top_investors=top_investors,
        top_borrowers=top_borrowers,
    )


@admin_bp.route('/overdue/check', methods=['POST'])
@login_required
def overdue_check():
    admin_required()
    today = date.today()
    loans = Loan.query.filter(Loan.due_date < today, Loan.status.in_(['Активный', 'Просрочен'])).all()
    processed = 0
    for loan in loans:
        was_overdue = loan.status == 'Просрочен'
        start_date = loan.last_penalty_date or loan.due_date
        days = max(0, (today - start_date).days)
        if days <= 0 and loan.status == 'Просрочен':
            continue
        daily_penalty = (Decimal(loan.principal) * Decimal('0.003')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        penalty = (daily_penalty * Decimal(max(days, 1))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        loan.penalty_amount = Decimal(loan.penalty_amount or 0) + penalty
        loan.last_penalty_date = today
        loan.status = 'Просрочен'
        if loan.application:
            loan.application.status = 'Просрочен'
        history = get_or_create_credit_history(loan.user_id)
        if not was_overdue:
            history.overdue_count += 1
        tx = Transaction(
            loan_id=loan.id,
            user_id=loan.user_id,
            operation_type='Начисление просрочки',
            amount=penalty,
            status='Успешно',
            comment=f'TEST начисление за {max(days, 1)} дн. просрочки'
        )
        db.session.add(tx)
        notify(loan.user_id, 'Есть просрочка', f'По займу #{loan.id} начислена тестовая просрочка {penalty} BYN. Остаток: {loan.balance} BYN.', 'warning')
        processed += 1
    db.session.add(AuditLog(actor_id=current_user.id, action='overdue_check_test', entity='Loan', entity_id=None, ip_address=request.remote_addr))
    db.session.commit()
    flash(f'Проверка просрочек выполнена. Обработано займов: {processed}.', 'success')
    return redirect(url_for('admin.dashboard'))

@admin_bp.route('/application/<int:app_id>')
@login_required
def application_detail(app_id):
    admin_required()
    app = db.get_or_404(LoanApplication, app_id)
    return render_template('admin/application_detail.html', app=app)


@admin_bp.route('/application/<int:app_id>/approve', methods=['POST'])
@login_required
def approve(app_id):
    admin_required()
    app = db.get_or_404(LoanApplication, app_id)
    if app.status not in ['На проверке', 'Одобрено'] or app.loan or app.funded_amount > Decimal('0.00'):
        flash('Ручная выдача доступна только для непрофинансированной заявки на проверке.', 'warning')
        return redirect(url_for('admin.application_detail', app_id=app.id))

    app.status = 'Активный займ'
    due_date = (utc_now() + timedelta(days=app.term_days)).date()
    loan = Loan(
        application_id=app.id,
        user_id=app.user_id,
        principal=app.amount,
        daily_rate=app.daily_rate,
        due_date=due_date,
    )
    db.session.add(loan)
    db.session.flush()

    history = get_or_create_credit_history(app.user_id)
    history.total_loans += 1
    history.max_principal = max(Decimal(history.max_principal or 0), Decimal(app.amount))

    payout_id = new_external_id('MANUAL-PAYOUT')
    borrower_fee = app.platform_borrower_fee
    db.session.add(Transaction(
        loan_id=loan.id,
        user_id=app.user_id,
        operation_type='Ручная тестовая выдача займа',
        amount=app.amount,
        status='В обработке',
        external_id=payout_id,
        comment='Ручная выдача через административный тестовый сценарий',
    ))
    add_gateway_operation(
        'Ручная выдача займа заёмщику',
        app.user_id,
        app.amount,
        loan_id=loan.id,
        status='В обработке',
        external_id=payout_id,
        comment='Тестовая ручная выдача без финансирования займодавцами',
    )
    add_ledger(
        'manual_borrower_payout_created',
        ESCROW_INVESTMENTS,
        BORROWER_PAYOUT,
        app.amount,
        user_id=app.user_id,
        loan_id=loan.id,
        external_id=payout_id,
        comment='Создана ручная выплата заёмщику',
    )
    add_platform_fee('manual_borrower_fee', app.id, borrower_fee, f'Комиссия ручной выдачи по заявке #{app.id}')

    notify(app.user_id, 'Заявка одобрена вручную', f'Займ #{loan.id} создан. Тестовая выплата {payout_id} отправлена в обработку.', 'success')
    ensure_loan_documents(loan)
    notify(app.user_id, 'Документы сформированы', f'По займу #{loan.id} создан договорный пакет.', 'info')
    db.session.add(AuditLog(actor_id=current_user.id, action='application_approved_and_loan_issued', entity='LoanApplication', entity_id=app.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Ручная тестовая выдача создана и отражена в финансовом журнале.', 'success')
    return redirect(url_for('admin.application_detail', app_id=app.id))


@admin_bp.route('/transaction/<int:tx_id>/success', methods=['POST'])
@login_required
def transaction_success(tx_id):
    admin_required()
    tx = db.get_or_404(Transaction, tx_id)
    if tx.status == 'Успешно':
        flash('Транзакция уже была отмечена успешной.', 'info')
        return redirect(url_for('admin.dashboard'))
    tx.status = 'Успешно'
    if tx.external_id:
        gateway_operation = PaymentGatewayOperation.query.filter_by(external_id=tx.external_id).first()
        if gateway_operation:
            gateway_operation.status = 'Успешно'
            gateway_operation.processed_at = utc_now()
    notify(tx.user_id, 'Деньги зачислены', f'Тестовая банковская операция {tx.external_id or tx.id} успешно выполнена.', 'success')
    db.session.add(AuditLog(actor_id=current_user.id, action='transaction_marked_success', entity='Transaction', entity_id=tx.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Транзакция отмечена успешной.', 'success')
    return redirect_back('admin.dashboard')


@admin_bp.route('/application/<int:app_id>/reject', methods=['POST'])
@login_required
def reject(app_id):
    admin_required()
    app = db.get_or_404(LoanApplication, app_id)
    if app.status not in ['На проверке', 'Одобрено', 'На витрине'] or app.loan or app.funded_amount > Decimal('0.00'):
        flash('Нельзя отклонить заявку после начала финансирования или выдачи займа.', 'warning')
        return redirect(url_for('admin.application_detail', app_id=app.id))
    app.status = 'Отказано'
    app.admin_comment = request.form.get('admin_comment', '').strip()
    notify(app.user_id, 'По заявке отказ', app.admin_comment or 'По заявке принято отрицательное решение.', 'warning')
    db.session.add(AuditLog(actor_id=current_user.id, action='application_rejected', entity='LoanApplication', entity_id=app.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Заявка отклонена.', 'success')
    return redirect(url_for('admin.application_detail', app_id=app.id))


@admin_bp.route('/application/<int:app_id>/manual-review', methods=['POST'])
@login_required
def manual_review(app_id):
    admin_required()
    app = db.get_or_404(LoanApplication, app_id)
    if app.loan or app.funded_amount > Decimal('0.00'):
        flash('Нельзя вернуть на проверку заявку, по которой уже есть финансирование или займ.', 'warning')
        return redirect(url_for('admin.application_detail', app_id=app.id))
    app.status = 'На проверке'
    app.admin_comment = request.form.get('admin_comment', '').strip()
    notify(app.user_id, 'Заявка на ручной проверке', app.admin_comment or 'Заявка передана специалисту для дополнительной проверки.', 'info')
    db.session.commit()
    flash('Заявка переведена на ручную проверку.', 'success')
    return redirect(url_for('admin.application_detail', app_id=app.id))

@admin_bp.route('/application/<int:app_id>/publish', methods=['POST'])
@login_required
def publish(app_id):
    admin_required()
    app = db.get_or_404(LoanApplication, app_id)
    if app.status in ['На проверке', 'Одобрено']:
        app.status = 'На витрине'
        app.admin_comment = request.form.get('admin_comment', '').strip()
        notify(app.user_id, 'Заявка опубликована на витрине', f'Заявка #{app.id} доступна займодавцам для финансирования.', 'success')
        db.session.add(AuditLog(actor_id=current_user.id, action='application_published_to_marketplace', entity='LoanApplication', entity_id=app.id, ip_address=request.remote_addr))
        db.session.commit()
        flash('Заявка опубликована на P2P-витрине.', 'success')
    else:
        flash('Текущий статус не позволяет опубликовать заявку.', 'warning')
    return redirect(url_for('admin.application_detail', app_id=app.id))


@admin_bp.route('/demo/seed', methods=['POST'])
@login_required
def seed_demo():
    admin_required()
    if not current_app.config.get('ALLOW_DEMO_RESET', True):
        abort(403)
    demo = create_demo_data(reset_existing=True)
    seed_legal_templates()
    created_docs = 0
    for loan in Loan.query.all():
        created_docs += len(ensure_loan_documents(loan))
    db.session.commit()
    compliance_created = run_bulk_compliance(current_user)
    operations_created = seed_demo_operations(current_user)
    message_templates = seed_message_templates()
    antifraud_created = seed_demo_antifraud(current_user)
    collections_created = seed_demo_collections(current_user)
    db.session.add(AuditLog(
        actor_id=current_user.id,
        action='full_demo_environment_reseeded',
        entity='DemoData',
        entity_id=None,
        ip_address=request.remote_addr,
    ))
    db.session.commit()
    flash(
        f"DEMO-контур пересоздан: {demo['borrowers']} заёмщиков, {demo['investors']} займодавцев, "
        f"{demo['applications']} заявок, {created_docs} документов, {compliance_created} compliance-кейсов, "
        f"{operations_created} обращений, {antifraud_created} anti-fraud событий и "
        f"{collections_created} collection-дел. Пароль: {demo['password']}",
        'success',
    )
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/legal')
@login_required
def legal_dashboard():
    admin_required()
    seed_legal_templates()
    summary = legal_summary()
    templates = LegalDocumentTemplate.query.order_by(LegalDocumentTemplate.document_type, LegalDocumentTemplate.title).all()
    documents = DealDocument.query.order_by(DealDocument.created_at.desc()).limit(120).all()
    signatures = SignatureEvent.query.order_by(SignatureEvent.created_at.desc()).limit(80).all()
    loans_without_docs = []
    for loan in Loan.query.order_by(Loan.issued_at.desc()).limit(200).all():
        if not DealDocument.query.filter_by(loan_id=loan.id).first():
            loans_without_docs.append(loan)
    return render_template('admin/legal.html', summary=summary, templates=templates, documents=documents, signatures=signatures, loans_without_docs=loans_without_docs[:30])


@admin_bp.route('/legal/rebuild', methods=['POST'])
@login_required
def rebuild_legal_documents():
    admin_required()
    seed_legal_templates()
    created = 0
    for loan in Loan.query.all():
        docs = ensure_loan_documents(loan)
        created += len(docs)
    db.session.add(AuditLog(actor_id=current_user.id, action='legal_documents_rebuilt', entity='DealDocument', entity_id=None, ip_address=request.remote_addr))
    db.session.commit()
    flash(f'Юридический пакет пересобран. Новых документов создано: {created}.', 'success')
    return redirect(url_for('admin.legal_dashboard'))


@admin_bp.route('/legal/document/<int:doc_id>/platform-sign', methods=['POST'])
@login_required
def platform_sign_document(doc_id):
    admin_required()
    doc = db.get_or_404(DealDocument, doc_id)
    signature_event = sign_document_test(doc, current_user, request.remote_addr, request.headers.get('User-Agent'))
    if signature_event is None:
        flash('Платформа уже подписала этот документ.', 'info')
        return redirect(url_for('admin.legal_dashboard'))
    db.session.add(AuditLog(actor_id=current_user.id, action='platform_legal_document_signed_test', entity='DealDocument', entity_id=doc.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Платформа поставила TEST-подпись. В реальности здесь должен быть официальный контур подписания/ЭЦП.', 'success')
    return redirect(url_for('admin.legal_dashboard'))


@admin_bp.route('/finance')
@login_required
def finance_dashboard():
    admin_required()
    summary = finance_summary()
    reconciliation = reconciliation_report()
    ledger_entries = LedgerEntry.query.order_by(LedgerEntry.created_at.desc()).limit(120).all()
    gateway_ops = PaymentGatewayOperation.query.order_by(PaymentGatewayOperation.created_at.desc()).limit(80).all()
    wallet_txs = WalletTransaction.query.order_by(WalletTransaction.created_at.desc()).limit(80).all()
    platform_fees = PlatformLedger.query.order_by(PlatformLedger.created_at.desc()).limit(80).all()
    return render_template(
        'admin/finance.html',
        summary=summary,
        reconciliation=reconciliation,
        ledger_entries=ledger_entries,
        gateway_ops=gateway_ops,
        wallet_txs=wallet_txs,
        platform_fees=platform_fees,
    )


@admin_bp.route('/finance/rebuild-ledger', methods=['POST'])
@login_required
def rebuild_ledger():
    admin_required()
    rebuild_finance_ledger_from_existing_data()
    db.session.add(AuditLog(actor_id=current_user.id, action='finance_ledger_rebuilt_from_demo_data', entity='LedgerEntry', entity_id=None, ip_address=request.remote_addr))
    db.session.commit()
    flash('Finance Core ledger пересобран по текущим demo-данным.', 'success')
    return redirect(url_for('admin.finance_dashboard'))



@admin_bp.route('/compliance')
@login_required
def compliance_dashboard():
    admin_required()
    summary = compliance_summary()
    cases = ComplianceCase.query.order_by(ComplianceCase.created_at.desc()).limit(120).all()
    flags = ComplianceFlag.query.order_by(ComplianceFlag.created_at.desc()).limit(80).all()
    decisions = ComplianceDecision.query.order_by(ComplianceDecision.created_at.desc()).limit(80).all()
    users_without_checks = User.query.filter(User.role.in_(['client', 'investor'])).outerjoin(ComplianceCase, ComplianceCase.user_id == User.id).filter(ComplianceCase.id == None).limit(30).all()
    return render_template('admin/compliance.html', summary=summary, cases=cases, flags=flags, decisions=decisions, users_without_checks=users_without_checks)


@admin_bp.route('/compliance/run-bulk', methods=['POST'])
@login_required
def compliance_run_bulk():
    admin_required()
    created = run_bulk_compliance(current_user)
    flash(f'Compliance TEST-проверки выполнены. Создано кейсов: {created}.', 'success')
    return redirect(url_for('admin.compliance_dashboard'))


@admin_bp.route('/compliance/user/<int:user_id>/run', methods=['POST'])
@login_required
def compliance_run_user(user_id):
    admin_required()
    user = db.get_or_404(User, user_id)
    application = LoanApplication.query.filter_by(user_id=user.id).order_by(LoanApplication.created_at.desc()).first() if user.is_borrower else None
    case = run_compliance_check(user, application, current_user)
    db.session.commit()
    flash(f'Создан Compliance TEST-кейс #{case.id}: риск {case.risk_level}, score {case.risk_score}/100.', 'success')
    return redirect(url_for('admin.compliance_dashboard'))


@admin_bp.route('/compliance/case/<int:case_id>/<decision>', methods=['POST'])
@login_required
def compliance_decide(case_id, decision):
    admin_required()
    if decision not in ['approve', 'manual_review', 'reject', 'freeze']:
        abort(404)
    case = db.get_or_404(ComplianceCase, case_id)
    comment = request.form.get('comment', '').strip()
    decide_case(case, current_user, decision, comment)
    db.session.commit()
    flash('Compliance-решение сохранено в audit trail.', 'success')
    return redirect(url_for('admin.compliance_dashboard'))


@admin_bp.route('/operations')
@login_required
def operations_dashboard():
    admin_required()
    seed_response_templates()
    summary = operations_summary()
    tickets = SupportTicket.query.order_by(SupportTicket.updated_at.desc()).limit(100).all()
    manual_items = ManualReviewItem.query.order_by(ManualReviewItem.created_at.desc()).limit(80).all()
    notes = OperatorNote.query.order_by(OperatorNote.created_at.desc()).limit(80).all()
    templates = ResponseTemplate.query.filter_by(is_active=True).order_by(ResponseTemplate.title).all()
    operators = User.query.filter_by(role='admin').order_by(User.full_name).all()
    return render_template('admin/operations.html', summary=summary, tickets=tickets, manual_items=manual_items, notes=notes, templates=templates, operators=operators)


@admin_bp.route('/operations/seed', methods=['POST'])
@login_required
def operations_seed():
    admin_required()
    created = seed_demo_operations(current_user)
    db.session.commit()
    flash(f'Operations Core demo-данные созданы. Новых обращений: {created}.', 'success')
    return redirect(url_for('admin.operations_dashboard'))


@admin_bp.route('/operations/ticket/<int:ticket_id>', methods=['GET', 'POST'])
@login_required
def operations_ticket(ticket_id):
    admin_required()
    ticket = db.get_or_404(SupportTicket, ticket_id)
    templates = ResponseTemplate.query.filter_by(is_active=True).order_by(ResponseTemplate.title).all()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'reply':
            message = request.form.get('message', '').strip()
            if message:
                db.session.add(SupportMessage(ticket_id=ticket.id, author_id=current_user.id, message=message, is_internal=False))
                requested_status = request.form.get('status') or 'В работе'
                if not set_ticket_status(ticket, requested_status):
                    flash('Передан недопустимый статус обращения.', 'danger')
                    return redirect(url_for('admin.operations_ticket', ticket_id=ticket.id))
                ticket.assigned_to_id = current_user.id
                notify(ticket.user_id, 'Ответ поддержки', f'По обращению #{ticket.id} появился ответ оператора.', 'info')
        elif action == 'internal_note':
            note = request.form.get('note', '').strip()
            if note:
                db.session.add(SupportMessage(ticket_id=ticket.id, author_id=current_user.id, message=note, is_internal=True))
                db.session.add(OperatorNote(actor_id=current_user.id, user_id=ticket.user_id, ticket_id=ticket.id, note=note))
        elif action == 'status':
            requested_status = request.form.get('status') or ticket.status
            requested_priority = request.form.get('priority') or ticket.priority
            if not set_ticket_status(ticket, requested_status) or requested_priority not in ALLOWED_TICKET_PRIORITIES:
                flash('Переданы недопустимые статус или приоритет.', 'danger')
                return redirect(url_for('admin.operations_ticket', ticket_id=ticket.id))
            ticket.priority = requested_priority
            ticket.assigned_to_id = current_user.id
        db.session.add(AuditLog(actor_id=current_user.id, action='operations_ticket_updated', entity='SupportTicket', entity_id=ticket.id, ip_address=request.remote_addr))
        db.session.commit()
        flash('Обращение обновлено.', 'success')
        return redirect(url_for('admin.operations_ticket', ticket_id=ticket.id))
    return render_template('admin/operation_ticket.html', ticket=ticket, templates=templates)


@admin_bp.route('/operations/manual/<int:item_id>/<action>', methods=['POST'])
@login_required
def operations_manual_action(item_id, action):
    admin_required()
    item = db.get_or_404(ManualReviewItem, item_id)
    if action == 'take':
        item.status = 'В работе'
        item.assigned_to_id = current_user.id
    elif action == 'close':
        item.status = 'Закрыта'
        item.closed_at = utc_now()
        item.resolution = request.form.get('resolution', '').strip() or 'Ручная проверка завершена.'
        db.session.add(OperatorNote(actor_id=current_user.id, user_id=item.borrower_id, application_id=item.application_id, note=item.resolution))
    else:
        abort(404)
    db.session.add(AuditLog(actor_id=current_user.id, action=f'operations_manual_{action}', entity='ManualReviewItem', entity_id=item.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Очередь ручной проверки обновлена.', 'success')
    return redirect(url_for('admin.operations_dashboard'))


@admin_bp.route('/operations/application/<int:app_id>/manual', methods=['POST'])
@login_required
def operations_create_manual(app_id):
    admin_required()
    app = db.get_or_404(LoanApplication, app_id)
    reason = request.form.get('reason', '').strip() or 'Ручная проверка заявки оператором.'
    item = create_manual_review(app, reason=reason, priority=request.form.get('priority') or 'medium')
    db.session.add(AuditLog(actor_id=current_user.id, action='operations_manual_created', entity='ManualReviewItem', entity_id=item.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Заявка добавлена в очередь ручной проверки.', 'success')
    return redirect_back('admin.operations_dashboard')


@admin_bp.route('/communications')
@login_required
def communications_dashboard():
    admin_required()
    seed_message_templates()
    db.session.commit()
    summary = communications_summary()
    logs = CommunicationLog.query.order_by(CommunicationLog.created_at.desc()).limit(120).all()
    notifications = Notification.query.order_by(Notification.created_at.desc()).limit(80).all()
    templates = MessageTemplate.query.order_by(MessageTemplate.category, MessageTemplate.code).all()
    preferences_count = NotificationPreference.query.count()
    return render_template('admin/communications.html', summary=summary, logs=logs, notifications=notifications, templates=templates, preferences_count=preferences_count)


@admin_bp.route('/communications/test', methods=['POST'])
@login_required
def communications_test():
    admin_required()
    user = User.query.filter_by(email=request.form.get('email', '').strip()).first()
    if not user:
        user = current_user
    notify_user(user.id, 'TEST-уведомление Kopilka', 'Проверка Notification / Email / SMS / Push TEST-каналов. Реальные SMS/email не отправлялись.', notification_type='info', category='system', channels=('email', 'sms', 'push'))
    db.session.add(AuditLog(actor_id=current_user.id, action='communications_test_sent', entity='User', entity_id=user.id, ip_address=request.remote_addr))
    db.session.commit()
    flash(f'TEST-уведомление создано для {user.email}. Записи каналов добавлены в logs/*.log.', 'success')
    return redirect(url_for('admin.communications_dashboard'))



@admin_bp.route('/antifraud')
@login_required
def antifraud_dashboard():
    admin_required()
    summary = antifraud_summary()
    events = AntifraudEvent.query.order_by(AntifraudEvent.created_at.desc()).limit(140).all()
    decisions = AntifraudDecision.query.order_by(AntifraudDecision.created_at.desc()).limit(80).all()
    users = User.query.filter(User.role.in_(['client', 'investor'])).order_by(User.created_at.desc()).limit(40).all()
    grouped = {}
    for event in events:
        grouped[event.event_type] = grouped.get(event.event_type, 0) + 1
    return render_template('admin/antifraud.html', summary=summary, events=events, decisions=decisions, users=users, grouped=grouped)


@admin_bp.route('/antifraud/run-bulk', methods=['POST'])
@login_required
def antifraud_run_bulk():
    admin_required()
    created = run_bulk_antifraud(current_user)
    db.session.commit()
    flash(f'Anti-Fraud TEST-сканирование выполнено. Создано событий: {created}.', 'success')
    return redirect(url_for('admin.antifraud_dashboard'))


@admin_bp.route('/antifraud/seed', methods=['POST'])
@login_required
def antifraud_seed():
    admin_required()
    created = seed_demo_antifraud(current_user)
    db.session.commit()
    flash(f'Anti-Fraud demo-события созданы. Новых событий: {created}.', 'success')
    return redirect(url_for('admin.antifraud_dashboard'))


@admin_bp.route('/antifraud/user/<int:user_id>/run', methods=['POST'])
@login_required
def antifraud_run_user(user_id):
    admin_required()
    user = db.get_or_404(User, user_id)
    created = run_user_antifraud(user, current_user)
    db.session.commit()
    flash(f'Anti-Fraud TEST-проверка пользователя {user.email}: создано событий {len(created)}.', 'success')
    return redirect(url_for('admin.antifraud_dashboard'))


@admin_bp.route('/antifraud/user/<int:user_id>/unblock', methods=['POST'])
@login_required
def antifraud_unblock_user(user_id):
    admin_required()
    user = db.get_or_404(User, user_id)
    if not user.is_blocked:
        flash('Аккаунт уже разблокирован.', 'info')
        return redirect(url_for('admin.antifraud_dashboard'))
    user.is_blocked = False
    notify(user.id, 'Аккаунт разблокирован', 'Администратор снял TEST-блокировку безопасности.', 'success')
    db.session.add(AuditLog(
        actor_id=current_user.id,
        action='antifraud_user_unblocked',
        entity='User',
        entity_id=user.id,
        ip_address=request.remote_addr,
    ))
    db.session.commit()
    flash(f'Аккаунт {user.email} разблокирован.', 'success')
    return redirect(url_for('admin.antifraud_dashboard'))


@admin_bp.route('/antifraud/event/<int:event_id>/<decision>', methods=['POST'])
@login_required
def antifraud_decide(event_id, decision):
    admin_required()
    if decision not in ['approve', 'monitor', 'restrict', 'block']:
        abort(404)
    event = db.get_or_404(AntifraudEvent, event_id)
    comment = request.form.get('comment', '').strip()
    decide_antifraud_event(event, current_user, decision, comment)
    db.session.commit()
    flash('Anti-Fraud решение сохранено в audit trail.', 'success')
    return redirect(url_for('admin.antifraud_dashboard'))



@admin_bp.route('/collections')
@login_required
def collections_dashboard():
    admin_required()
    summary = collections_summary()
    cases = CollectionCase.query.order_by(CollectionCase.overdue_days.desc(), CollectionCase.updated_at.desc()).limit(160).all()
    actions = CollectionAction.query.order_by(CollectionAction.created_at.desc()).limit(120).all()
    return render_template('admin/collections.html', summary=summary, cases=cases, actions=actions)


@admin_bp.route('/collections/run-cycle', methods=['POST'])
@login_required
def collections_run_cycle():
    admin_required()
    processed = run_collections_cycle(current_user)
    db.session.commit()
    flash(f'Collections TEST-цикл выполнен. Обработано дел: {processed}.', 'success')
    return redirect(url_for('admin.collections_dashboard'))


@admin_bp.route('/collections/<int:case_id>/action', methods=['POST'])
@login_required
def collections_add_action(case_id):
    admin_required()
    case = db.get_or_404(CollectionCase, case_id)
    title = request.form.get('title', '').strip() or 'Комментарий оператора'
    message = request.form.get('message', '').strip()
    channel = request.form.get('channel', '').strip() or 'operator'
    add_collection_action(case, current_user, 'manual_operator_action', channel, title, message, status='Создано')
    case.updated_at = utc_now()
    db.session.commit()
    flash('Действие по collection-делу добавлено.', 'success')
    return redirect(url_for('admin.collections_dashboard'))


@admin_bp.route('/collections/<int:case_id>/close', methods=['POST'])
@login_required
def collections_close(case_id):
    admin_required()
    case = db.get_or_404(CollectionCase, case_id)
    close_collection_case(case, current_user, request.form.get('comment', '').strip() or 'Закрыто после урегулирования TEST')
    db.session.commit()
    flash('Collection-дело закрыто.', 'success')
    return redirect(url_for('admin.collections_dashboard'))
