from datetime import datetime, timedelta
import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from app.time_utils import utc_now
from app import db
from app.http_utils import redirect_back
from app.models import ClientProfile, LoanApplication, Loan, Transaction, AuditLog, Notification, CreditHistory, Wallet, Investment, PlatformLedger, AutoInvestRule, DealDocument, LegalDocumentTemplate
from app.finance import add_ledger, add_gateway_operation, add_wallet_tx, add_platform_fee, INVESTOR_WALLET, ESCROW_INVESTMENTS, PLATFORM_REVENUE, COLLECTION_ACCOUNT, BORROWER_PAYOUT, EXTERNAL_BANK, new_external_id
from app.legal import ensure_loan_documents, sign_document_test, seed_legal_templates
from app.communications import notify_user, get_or_create_preferences

loans_bp = Blueprint('loans', __name__)


def money(value, default='0'):
    try:
        return Decimal(str(value or default)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def parse_int(value, default=0):
    try:
        return int(value or default)
    except (ValueError, TypeError):
        return default


def calc_total(amount, days, rate=Decimal('0.008')):
    amount = Decimal(str(amount))
    total = amount + amount * rate * Decimal(int(days))
    return total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def get_or_create_wallet(user):
    wallet = user.wallet
    if not wallet:
        wallet = Wallet(user_id=user.id)
        db.session.add(wallet)
        db.session.flush()
    return wallet


def get_or_create_credit_history(user):
    history = user.credit_history
    if not history:
        closed = Loan.query.filter_by(user_id=user.id, status='Закрыт').count()
        total = Loan.query.filter_by(user_id=user.id).count()
        max_principal = db.session.query(db.func.max(Loan.principal)).filter_by(user_id=user.id).scalar() or 0
        history = CreditHistory(user_id=user.id, total_loans=total, closed_loans=closed, max_principal=max_principal)
        db.session.add(history)
        db.session.flush()
    return history


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


def grade_min_score(grade):
    return {'A+': 900, 'A': 800, 'B+': 700, 'B': 600, 'C': 500, 'D': 0}.get(grade or 'D', 0)


def risk_rank(risk):
    return {'Низкий': 1, 'Средний': 2, 'Высокий': 3}.get(risk or 'Высокий', 3)


def portfolio_stats(investor):
    investments = Investment.query.filter_by(investor_id=investor.id).all()
    active = [i for i in investments if i.status == 'Активна']
    closed = [i for i in investments if i.status == 'Завершена']
    overdue = [i for i in active if i.application.loan and i.application.loan.status == 'Просрочен']
    invested_active = sum(Decimal(i.amount or 0) for i in active).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    expected_active = sum(Decimal(i.expected_return or 0) for i in active).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    closed_profit = sum(i.investor_profit for i in closed).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    active_profit = max(Decimal('0.00'), expected_active - invested_active).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    avg_yield = Decimal('0.00')
    if active:
        avg_yield = (sum(Decimal(i.application.annual_yield or 0) for i in active) / Decimal(len(active))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {
        'active_count': len(active),
        'closed_count': len(closed),
        'overdue_count': len(overdue),
        'invested_active': invested_active,
        'expected_active': expected_active,
        'active_profit': active_profit,
        'closed_profit': closed_profit,
        'avg_yield': avg_yield,
    }


def investor_analytics(investor):
    investments = Investment.query.filter_by(investor_id=investor.id).all()
    grade_order = ['A+', 'A', 'B+', 'B', 'C', 'D']
    risk_order = ['Низкий', 'Средний', 'Высокий']
    grade_totals = {g: Decimal('0.00') for g in grade_order}
    risk_totals = {r: Decimal('0.00') for r in risk_order}
    for inv in investments:
        amount = Decimal(inv.amount or 0)
        grade = inv.application.rating_grade if inv.application else 'D'
        risk = inv.application.risk_level if inv.application else 'Высокий'
        grade_totals[grade] = grade_totals.get(grade, Decimal('0.00')) + amount
        risk_totals[risk] = risk_totals.get(risk, Decimal('0.00')) + amount
    active_profit = sum((max(Decimal('0.00'), Decimal(i.expected_return or 0) - Decimal(i.amount or 0) - Decimal(i.platform_fee or 0)) for i in investments if i.status == 'Активна'), Decimal('0.00'))
    closed_profit = sum((i.investor_profit for i in investments if i.status == 'Завершена'), Decimal('0.00'))
    total_profit = active_profit + closed_profit
    invested_total = sum((Decimal(i.amount or 0) for i in investments), Decimal('0.00'))
    monthly = []
    base = max(total_profit, Decimal('120.00'))
    factors = [Decimal('0.18'), Decimal('0.24'), Decimal('0.31'), Decimal('0.45'), Decimal('0.62'), Decimal('0.78'), Decimal('1.00')]
    for label, factor in zip(['Пн','Вт','Ср','Чт','Пт','Сб','Вс'], factors):
        monthly.append({'label': label, 'value': float((base * factor).quantize(Decimal('0.01')))})
    forecast_month = (total_profit + max(invested_total * Decimal('0.018'), Decimal('45.00'))).quantize(Decimal('0.01'))
    forecast_year = (total_profit + max(invested_total * Decimal('0.22'), Decimal('620.00'))).quantize(Decimal('0.01'))
    return {
        'grade_labels': grade_order,
        'grade_values': [float(grade_totals[g]) for g in grade_order],
        'risk_labels': risk_order,
        'risk_values': [float(risk_totals[r]) for r in risk_order],
        'profit_labels': [x['label'] for x in monthly],
        'profit_values': [x['value'] for x in monthly],
        'forecast_month': forecast_month,
        'forecast_year': forecast_year,
        'portfolio_total': invested_total.quantize(Decimal('0.01')),
    }



def portfolio_intelligence(investor, stats, analytics):
    """Демонстрационные рекомендации без новой схемы БД: считаем из текущих инвестиций."""
    total = Decimal(analytics.get('portfolio_total') or 0)
    grade_values = analytics.get('grade_values') or []
    grade_labels = analytics.get('grade_labels') or []
    high_grade = Decimal('0.00')
    mid_grade = Decimal('0.00')
    low_grade = Decimal('0.00')
    for label, value in zip(grade_labels, grade_values):
        amount = Decimal(str(value or 0))
        if label in ['A+', 'A']:
            high_grade += amount
        elif label in ['B+', 'B']:
            mid_grade += amount
        else:
            low_grade += amount
    high_share = int((high_grade / total * 100)) if total else 0
    low_share = int((low_grade / total * 100)) if total else 0
    overdue_count = int(stats.get('overdue_count') or 0)
    active_count = int(stats.get('active_count') or 0)
    avg_yield = Decimal(stats.get('avg_yield') or 0)
    risk_score = 10
    if low_share > 25:
        risk_score -= 2
    if overdue_count:
        risk_score -= min(3, overdue_count)
    if active_count < 5:
        risk_score -= 1
    if avg_yield > Decimal('280'):
        risk_score -= 1
    risk_score = max(1, min(10, risk_score))
    if risk_score >= 8:
        profile = 'Сбалансированный низкорисковый'
        level = 'Низкий риск'
    elif risk_score >= 6:
        profile = 'Сбалансированный'
        level = 'Средний риск'
    else:
        profile = 'Агрессивный / требуется ребалансировка'
        level = 'Высокий риск'
    recommendations = []
    if total == 0:
        recommendations.append('Начните с небольших долей в заявках A+ и A, чтобы собрать базовую диверсификацию.')
    if active_count < 8:
        recommendations.append('Увеличьте количество активных долей: P2P-портфель безопаснее, когда сумма распределена между многими заявками.')
    if low_share > 20:
        recommendations.append('Доля C/D повышена. Для консервативного профиля снизьте лимиты на высокий риск.')
    if high_share < 50:
        recommendations.append('Добавьте больше A+ и A: это снизит волатильность портфеля.')
    if not recommendations:
        recommendations.append('Портфель выглядит устойчиво. Следующий шаг — настроить автоинвест под выбранный профиль.')
    return {
        'risk_score': risk_score,
        'profile': profile,
        'risk_level': level,
        'high_share': high_share,
        'low_share': low_share,
        'recommendations': recommendations[:4],
    }

def get_or_create_autoinvest_rule(user):
    rule = AutoInvestRule.query.filter_by(investor_id=user.id).first()
    if not rule:
        rule = AutoInvestRule(investor_id=user.id)
        db.session.add(rule)
        db.session.flush()
    return rule


def execute_auto_investments(application=None):
    apps_query = LoanApplication.query.filter(LoanApplication.status.in_(['На витрине', 'Частично профинансирована']))
    if application is not None:
        apps_query = apps_query.filter_by(id=application.id)
    apps = apps_query.order_by(LoanApplication.created_at.asc()).all()
    rules = AutoInvestRule.query.filter_by(is_enabled=True).all()
    executed = 0
    for app in apps:
        if app.remaining_to_fund <= Decimal('0.00'):
            continue
        for rule in rules:
            investor = rule.investor
            if not investor or not investor.is_investor:
                continue
            if app.user_id == investor.id:
                continue
            if Investment.query.filter_by(application_id=app.id, investor_id=investor.id).first():
                continue
            if int(app.scoring_score or 0) < rule.min_score:
                continue
            if risk_rank(app.risk_level) > risk_rank(rule.max_risk):
                continue
            if int(app.term_days or 0) > int(rule.max_term_days or 0):
                continue
            if Decimal(app.annual_yield or 0) < Decimal(rule.min_yield or 0):
                continue
            wallet = get_or_create_wallet(investor)
            amount = min(Decimal(rule.max_amount_per_application or 0), app.remaining_to_fund)
            if amount < Decimal('10') or wallet.available < (amount + amount * Decimal('0.01')):
                continue
            investor_fee = (amount * Decimal('0.01')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            expected_share = (Decimal(app.total_to_return) * (amount / Decimal(app.amount))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            inv = Investment(investor_id=investor.id, amount=amount, platform_fee=investor_fee, expected_return=expected_share, external_id='AUTO-INV-' + uuid.uuid4().hex[:8].upper())
            wallet.balance = Decimal(wallet.balance or 0) - amount - investor_fee
            wallet.total_invested = Decimal(wallet.total_invested or 0) + amount
            app.investments.append(inv)
            db.session.flush()
            add_platform_fee('autoinvest_investor_fee', app.id, investor_fee, f'Комиссия автоинвеста по заявке #{app.id}')
            add_ledger(
                'investment_escrow',
                INVESTOR_WALLET,
                ESCROW_INVESTMENTS,
                amount,
                user_id=investor.id,
                investment_id=inv.id,
                external_id=inv.external_id,
                comment=f'Автоинвест в заявку #{app.id}',
            )
            add_wallet_tx(
                wallet,
                'debit',
                'Автоинвестиция в заявку',
                amount + investor_fee,
                external_id=inv.external_id,
                comment=f'Заявка #{app.id}, включая комиссию {investor_fee} BYN',
            )
            notify(investor.id, 'Автоинвестирование выполнено', f'Правило вложило {amount} BYN в заявку #{app.id}. Комиссия: {investor_fee} BYN.', 'success')
            executed += 1
            if app.remaining_to_fund <= Decimal('0.00'):
                activate_funded_application(app)
                break
        if app.remaining_to_fund > Decimal('0.00') and app.funded_amount > Decimal('0.00'):
            app.status = 'Частично профинансирована'
    return executed


def activate_funded_application(app):
    if app.loan:
        return app.loan

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

    history = get_or_create_credit_history(app.user)
    history.total_loans += 1
    history.max_principal = max(Decimal(history.max_principal or 0), Decimal(app.amount))

    borrower_fee = app.platform_borrower_fee
    payout_id = 'PAYOUT-TEST-' + uuid.uuid4().hex[:8].upper()
    add_platform_fee('borrower_fee', app.id, borrower_fee, f'Комиссия заёмщика по заявке #{app.id}')
    db.session.add(Transaction(
        loan_id=loan.id,
        user_id=app.user_id,
        operation_type='Выдача P2P-займа',
        amount=app.amount,
        status='В обработке',
        external_id=payout_id,
        comment='TEST: перевод заёмщику через банк-партнёр после полного финансирования',
    ))
    add_gateway_operation(
        'Выдача займа заёмщику',
        app.user_id,
        app.amount,
        loan_id=loan.id,
        status='В обработке',
        external_id=payout_id,
        comment='TEST: банк-партнёр переводит деньги заёмщику',
    )
    add_ledger(
        'borrower_payout_created',
        ESCROW_INVESTMENTS,
        BORROWER_PAYOUT,
        app.amount,
        user_id=app.user_id,
        loan_id=loan.id,
        external_id=payout_id,
        comment='Создана выплата заёмщику после полного финансирования',
    )

    notify(app.user_id, 'Заявка полностью профинансирована', f'Заявка #{app.id} собрала {app.amount} BYN. TEST-перевод денег отправлен в обработку. Комиссия платформы заёмщика: {borrower_fee} BYN.', 'success')
    for investment in app.investments:
        notify(investment.investor_id, 'Заявка профинансирована полностью', f'По заявке #{app.id} создан активный займ. Ожидаемый возврат вашей доли: {investment.expected_return} BYN.', 'success')

    ensure_loan_documents(loan)
    notify(app.user_id, 'Документы сформированы', f'По займу #{loan.id} создан договорный пакет: договоры с займодавцами и график платежей.', 'info')
    return loan


def run_test_scoring(user, amount, term_days):
    profile = user.profile
    history = get_or_create_credit_history(user)
    score = 420
    reasons = []
    if not profile:
        return 0, 'Автоотказ', 'Высокий', Decimal('0'), 'Анкета не заполнена.'
    if profile.age_group == '25-55':
        score += 120; reasons.append('возрастная группа в оптимальном диапазоне')
    elif profile.age_group in ['18-24', '56-65']:
        score += 55; reasons.append('возрастная группа допустимая')
    elif profile.age_group == '65+':
        score -= 80; reasons.append('возрастная группа повышает риск')
    else:
        score -= 120; reasons.append('возрастная группа не подтверждена')
    if profile.msi_verified:
        score += 120; reasons.append('личность подтверждена через МСИ TEST')
    else:
        score -= 220; reasons.append('нет подтверждения МСИ')
    if profile.card_verified:
        score += 70; reasons.append('карта тестово токенизирована')
    income = Decimal(profile.monthly_income or 0)
    expenses = Decimal(profile.monthly_expenses or 0)
    if income >= 1800:
        score += 150; reasons.append('высокий доход')
    elif income >= 1000:
        score += 95; reasons.append('доход достаточный')
    elif income >= 600:
        score += 40; reasons.append('доход средний')
    else:
        score -= 170; reasons.append('низкий или неуказанный доход')
    free_income = max(income - expenses, Decimal('0'))
    requested_load = Decimal(amount) / max(income, Decimal('1'))
    if free_income >= Decimal(amount) * Decimal('0.75'):
        score += 90; reasons.append('достаточный свободный доход')
    if requested_load <= Decimal('0.35'):
        score += 80; reasons.append('умеренная долговая нагрузка')
    elif requested_load > Decimal('0.9'):
        score -= 160; reasons.append('сумма близка к месячному доходу')
    if (profile.work_experience_months or 0) >= 24:
        score += 110; reasons.append('стаж более двух лет')
    elif (profile.work_experience_months or 0) >= 6:
        score += 45; reasons.append('стаж более шести месяцев')
    else:
        score -= 60; reasons.append('малый стаж или стаж не указан')
    if profile.employment_type in ['Работа по найму', 'ИП / самозанятый', 'Пенсионер']:
        score += 45; reasons.append('подтверждён тип занятости')
    elif profile.employment_type == 'Безработный':
        score -= 160; reasons.append('нет постоянной занятости')
    if (profile.children_count or 0) >= 3:
        score -= 35; reasons.append('повышенная семейная нагрузка')
    active = Loan.query.filter(
        Loan.user_id == user.id,
        Loan.status.in_(['Активный', 'Просрочен']),
    ).count()
    if active:
        score -= 280; reasons.append('есть активный займ')
    if history.closed_loans:
        score += min(170, history.closed_loans * 55); reasons.append('есть закрытые займы')
    if history.overdue_count:
        score -= min(350, history.overdue_count * 160); reasons.append('были просрочки')
    else:
        score += 80; reasons.append('нет просрочек')
    if term_days <= 30:
        score += 30; reasons.append('короткий срок займа')
    score = max(0, min(1000, score))
    if score >= 700:
        decision, risk = 'Допущена на витрину', 'Низкий'
    elif score >= 500:
        decision, risk = 'Ручная проверка', 'Средний'
    else:
        decision, risk = 'Автоотказ', 'Высокий'
    if score >= 820:
        multiplier = Decimal('2.0')
    elif score >= 700:
        multiplier = Decimal('1.25')
    elif score >= 500:
        multiplier = Decimal('0.75')
    else:
        multiplier = Decimal('0')
    max_amount = min(Decimal('5000'), max(Decimal('0'), income * multiplier), history.repeat_limit)
    return score, decision, risk, max_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '; '.join(reasons)


@loans_bp.route('/')
@login_required
def dashboard():
    if current_user.is_admin:
        return redirect(url_for('admin.dashboard'))
    if current_user.is_investor:
        wallet = get_or_create_wallet(current_user)
        marketplace = LoanApplication.query.filter(LoanApplication.status.in_(['На витрине', 'Частично профинансирована'])).order_by(LoanApplication.created_at.desc()).limit(12).all()
        investments = Investment.query.filter_by(investor_id=current_user.id).order_by(Investment.created_at.desc()).limit(30).all()
        notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(8).all()
        rule = get_or_create_autoinvest_rule(current_user)
        stats = portfolio_stats(current_user)
        db.session.commit()
        analytics = investor_analytics(current_user)
        intelligence = portfolio_intelligence(current_user, stats, analytics)
        return render_template('loans/investor_dashboard.html', wallet=wallet, marketplace=marketplace, investments=investments, notifications=notifications, rule=rule, stats=stats, analytics=analytics, intelligence=intelligence)
    applications = LoanApplication.query.filter_by(user_id=current_user.id).order_by(LoanApplication.created_at.desc()).all()
    active_loan = Loan.query.filter(
        Loan.user_id == current_user.id,
        Loan.status.in_(['Активный', 'Просрочен']),
    ).order_by(Loan.due_date.asc()).first()
    transactions = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.created_at.desc()).limit(10).all()
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(8).all()
    history = get_or_create_credit_history(current_user)
    db.session.commit()
    return render_template('loans/dashboard.html', applications=applications, active_loan=active_loan, transactions=transactions, notifications=notifications, history=history)


@loans_bp.route('/notifications/read', methods=['POST'])
@login_required
def read_notifications():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({
        'is_read': True,
        'read_at': utc_now(),
    })
    db.session.commit()
    flash('Уведомления отмечены как прочитанные.', 'success')
    return redirect(url_for('loans.dashboard'))


@loans_bp.route('/notifications')
@login_required
def notifications_center():
    category = request.args.get('category') or ''
    status = request.args.get('status') or ''
    query = Notification.query.filter_by(user_id=current_user.id)
    if category:
        query = query.filter_by(category=category)
    if status == 'unread':
        query = query.filter_by(is_read=False)
    notifications = query.order_by(Notification.created_at.desc()).limit(200).all()
    counts = {
        'all': Notification.query.filter_by(user_id=current_user.id).count(),
        'unread': Notification.query.filter_by(user_id=current_user.id, is_read=False).count(),
        'loan': Notification.query.filter_by(user_id=current_user.id, category='loan').count(),
        'investment': Notification.query.filter_by(user_id=current_user.id, category='investment').count(),
        'payment': Notification.query.filter_by(user_id=current_user.id, category='payment').count(),
        'support': Notification.query.filter_by(user_id=current_user.id, category='support').count(),
        'system': Notification.query.filter_by(user_id=current_user.id, category='system').count(),
    }
    return render_template('loans/notifications.html', notifications=notifications, counts=counts, category=category, status=status)


@loans_bp.route('/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def read_notification(notification_id):
    note = Notification.query.filter_by(id=notification_id, user_id=current_user.id).first_or_404()
    note.is_read = True
    note.read_at = utc_now()
    db.session.commit()
    return redirect_back('loans.notifications_center')


@loans_bp.route('/notification-settings', methods=['GET', 'POST'])
@login_required
def notification_settings():
    pref = get_or_create_preferences(current_user.id)
    if request.method == 'POST':
        pref.enabled = bool(request.form.get('enabled'))
        pref.email_enabled = bool(request.form.get('email_enabled'))
        pref.sms_enabled = bool(request.form.get('sms_enabled'))
        pref.push_enabled = bool(request.form.get('push_enabled'))
        pref.loan_events = bool(request.form.get('loan_events'))
        pref.investment_events = bool(request.form.get('investment_events'))
        pref.payment_events = bool(request.form.get('payment_events'))
        pref.support_events = bool(request.form.get('support_events'))
        pref.marketing_enabled = bool(request.form.get('marketing_enabled'))
        db.session.commit()
        flash('Настройки уведомлений сохранены.', 'success')
        return redirect(url_for('loans.notification_settings'))
    return render_template('loans/notification_settings.html', pref=pref)


@loans_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if not current_user.is_borrower:
        abort(403)
    profile = current_user.profile or ClientProfile(user_id=current_user.id)
    if request.method == 'POST':
        action = request.form.get('action')
        profile.consent_personal_data = bool(request.form.get('consent_personal_data'))
        profile.consent_scoring = bool(request.form.get('consent_scoring'))
        profile.consent_msi = bool(request.form.get('consent_msi'))
        if action == 'msi_verify':
            if not profile.consent_msi:
                flash('Для тестовой МСИ-проверки нужно дать согласие на идентификацию через МСИ.', 'danger')
                return redirect(url_for('loans.profile'))
            profile.msi_verified = True
            profile.msi_verified_at = utc_now()
            profile.msi_reference = 'MSI-TEST-' + uuid.uuid4().hex[:10].upper()
            profile.msi_provider = 'МСИ TEST'
            db.session.add(profile)
            notify(current_user.id, 'МСИ-проверка выполнена', f'Личность подтверждена через тестовый шлюз МСИ. Reference: {profile.msi_reference}. Паспортные данные на сайте не сохранялись.', 'success')
            db.session.add(AuditLog(actor_id=current_user.id, action='msi_test_verified', entity='ClientProfile', entity_id=profile.id, ip_address=request.remote_addr))
            db.session.commit()
            flash('МСИ TEST: идентификация пройдена. Паспортные данные и сканы документов не сохранялись.', 'success')
            return redirect(url_for('loans.profile'))
        profile.age_group = request.form.get('age_group', '').strip()
        profile.region = request.form.get('region', '').strip()
        profile.marital_status = request.form.get('marital_status', '').strip()
        profile.children_count = parse_int(request.form.get('children_count'))
        profile.employment_type = request.form.get('employment_type', '').strip()
        profile.work_experience_months = parse_int(request.form.get('work_experience_months'))
        profile.monthly_income = money(request.form.get('monthly_income')) if request.form.get('monthly_income') else None
        profile.monthly_expenses = money(request.form.get('monthly_expenses')) if request.form.get('monthly_expenses') else None
        card_number = request.form.get('card_number', '').replace(' ', '').replace('-', '').strip()
        if card_number:
            if not card_number.isdigit() or not 12 <= len(card_number) <= 19:
                flash('Введите корректный тестовый номер карты: от 12 до 19 цифр.', 'danger')
                return render_template('loans/profile.html', profile=profile)
            profile.card_mask = '**** **** **** ' + card_number[-4:]
            profile.card_token = 'CARD-TEST-' + uuid.uuid4().hex[:10].upper()
            profile.card_verified = True
        db.session.add(profile)
        notify(current_user.id, 'Анкета обновлена', 'Данные сохранены. Паспортные данные и файлы документов на сайте не хранятся.', 'success')
        db.session.add(AuditLog(actor_id=current_user.id, action='client_profile_updated_no_documents', entity='ClientProfile', entity_id=profile.id, ip_address=request.remote_addr))
        db.session.commit()
        flash('Анкета сохранена. Идентификация должна проходить через МСИ/официальный шлюз, здесь хранится только статус проверки.', 'success')
        return redirect(url_for('loans.dashboard'))
    return render_template('loans/profile.html', profile=profile)


@loans_bp.route('/apply', methods=['GET', 'POST'])
@login_required
def apply():
    if not current_user.is_borrower:
        abort(403)
    profile = current_user.profile
    if request.method == 'POST':
        if not profile or not profile.is_ready_for_application:
            flash('Перед заявкой пройдите МСИ TEST, подтвердите тестовую карту и отметьте согласия.', 'danger')
            return redirect(url_for('loans.profile'))
        amount = parse_int(request.form.get('amount'))
        term_days = parse_int(request.form.get('term_days'))
        purpose = request.form.get('purpose', '').strip()
        if amount < 50 or amount > 5000 or term_days < 3 or term_days > 60:
            flash('Сумма должна быть 50–5000, срок 3–60 дней.', 'danger')
            return render_template('loans/apply.html', profile=profile)
        total = calc_total(amount, term_days)
        score, decision, risk, max_amount, details = run_test_scoring(current_user, amount, term_days)
        status = 'На витрине' if decision == 'Допущена на витрину' and Decimal(amount) <= max_amount else ('Отказано' if decision == 'Автоотказ' else 'На проверке')
        if decision == 'Допущена на витрину' and Decimal(amount) > max_amount:
            status = 'На проверке'
            details += '; сумма выше тестового лимита — нужна ручная проверка'
        app = LoanApplication(user_id=current_user.id, amount=amount, term_days=term_days, daily_rate=Decimal('0.008'), total_to_return=total, purpose=purpose, scoring_score=score, scoring_decision=decision, scoring_details=details, status=status, risk_level=risk, max_approved_amount=max_amount)
        db.session.add(app)
        db.session.flush()
        notify(current_user.id, 'Заявка создана', f'Решение: {decision}. Балл: {score}/1000. Статус: {status}.', 'success' if status != 'Отказано' else 'warning')
        db.session.add(AuditLog(actor_id=current_user.id, action='p2p_loan_request_created', entity='LoanApplication', entity_id=app.id, ip_address=request.remote_addr))
        if status == 'На витрине':
            executed = execute_auto_investments(app)
            if executed:
                notify(current_user.id, 'Автоинвесторы подключились', f'По заявке #{app.id} автоматически создано инвестиций: {executed}.', 'info')
        db.session.commit()
        flash(f'Заявка создана. Статус: {status}. Теперь её могут профинансировать займодавцы.', 'success' if status != 'Отказано' else 'warning')
        return redirect(url_for('loans.dashboard'))
    return render_template('loans/apply.html', profile=profile)


@loans_bp.route('/marketplace')
@login_required
def marketplace():
    query = LoanApplication.query.filter(LoanApplication.status.in_(['На витрине', 'Частично профинансирована']))
    risk = request.args.get('risk', '').strip()
    grade = request.args.get('grade', '').strip()
    amount_min = request.args.get('amount_min', '').strip()
    amount_max = request.args.get('amount_max', '').strip()
    term_max = request.args.get('term_max', '').strip()
    funded_max = request.args.get('funded_max', '').strip()
    sort = request.args.get('sort', 'new')
    if risk:
        query = query.filter_by(risk_level=risk)
    if grade:
        query = query.filter(LoanApplication.scoring_score >= grade_min_score(grade))
    if amount_min:
        query = query.filter(LoanApplication.amount >= money(amount_min))
    if amount_max:
        query = query.filter(LoanApplication.amount <= money(amount_max))
    if term_max:
        query = query.filter(LoanApplication.term_days <= parse_int(term_max, 60))
    apps = query.all()
    if funded_max:
        apps = [a for a in apps if a.funding_progress <= parse_int(funded_max, 100)]
    if sort == 'yield':
        apps.sort(key=lambda a: Decimal(a.annual_yield or 0), reverse=True)
    elif sort == 'risk':
        apps.sort(key=lambda a: (risk_rank(a.risk_level), -(a.scoring_score or 0)))
    elif sort == 'remaining':
        apps.sort(key=lambda a: a.remaining_to_fund)
    else:
        apps.sort(key=lambda a: a.created_at, reverse=True)
    wallet = get_or_create_wallet(current_user) if current_user.is_investor else None
    return render_template('loans/marketplace.html', applications=apps, wallet=wallet, filters=request.args)


@loans_bp.route('/investor/topup', methods=['POST'])
@login_required
def investor_topup():
    if not current_user.is_investor:
        abort(403)
    wallet = get_or_create_wallet(current_user)
    amount = money(request.form.get('amount'))
    if amount < Decimal('10'):
        flash('Минимальное TEST-пополнение — 10 BYN.', 'danger')
        return redirect(url_for('loans.dashboard'))
    if amount > Decimal('100000'):
        flash('Максимальное TEST-пополнение за одну операцию — 100 000 BYN.', 'danger')
        return redirect(url_for('loans.dashboard'))
    external_id = 'BANK-TEST-' + uuid.uuid4().hex[:8].upper()
    wallet.balance = Decimal(wallet.balance or 0) + amount
    tx = Transaction(user_id=current_user.id, operation_type='Пополнение кошелька займодавца', amount=amount, status='Успешно', external_id=external_id, comment='TEST: деньги должны приходить через банк-партнёр/платёжного провайдера')
    db.session.add(tx)
    add_gateway_operation('Пополнение кошелька', current_user.id, amount, status='Успешно', external_id=external_id, comment='TEST provider: входящее пополнение кошелька займодавца')
    add_ledger('wallet_topup', EXTERNAL_BANK, INVESTOR_WALLET, amount, user_id=current_user.id, external_id=external_id, comment='Пополнение TEST-кошелька займодавца')
    add_wallet_tx(wallet, 'credit', 'Пополнение кошелька', amount, external_id=external_id, comment='TEST-платёжный провайдер')
    notify(current_user.id, 'Кошелёк пополнен', f'TEST-баланс пополнен на {amount} BYN. В реальном проекте деньги хранятся/проводятся через банк-партнёр.', 'success')
    db.session.commit()
    flash('TEST-кошелёк пополнен.', 'success')
    return redirect(url_for('loans.dashboard'))


@loans_bp.route('/investor/autoinvest', methods=['POST'])
@login_required
def save_autoinvest():
    if not current_user.is_investor:
        abort(403)
    rule = get_or_create_autoinvest_rule(current_user)
    allowed_ratings = {'A+', 'A', 'B+', 'B', 'C', 'D'}
    allowed_risks = {'Низкий', 'Средний', 'Высокий'}
    requested_rating = request.form.get('min_rating', 'B')
    requested_risk = request.form.get('max_risk', 'Средний')
    rule.is_enabled = bool(request.form.get('is_enabled'))
    rule.min_rating = requested_rating if requested_rating in allowed_ratings else 'B'
    rule.max_risk = requested_risk if requested_risk in allowed_risks else 'Средний'
    rule.max_amount_per_application = min(
        Decimal('5000.00'),
        max(Decimal('10.00'), money(request.form.get('max_amount_per_application'), '100')),
    )
    rule.max_term_days = max(3, min(60, parse_int(request.form.get('max_term_days'), 30)))
    rule.min_yield = min(
        Decimal('500.00'),
        max(Decimal('0.00'), money(request.form.get('min_yield'), '20')),
    )
    db.session.add(AuditLog(actor_id=current_user.id, action='autoinvest_rule_updated', entity='AutoInvestRule', entity_id=rule.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Правило автоинвестирования сохранено.', 'success')
    return redirect(url_for('loans.dashboard'))




@loans_bp.route('/investor/autoinvest/profile', methods=['POST'])
@login_required
def autoinvest_profile():
    if not current_user.is_investor:
        abort(403)
    profile = request.form.get('profile', 'balanced')
    rule = get_or_create_autoinvest_rule(current_user)
    presets = {
        'conservative': {'min_rating': 'A', 'max_risk': 'Низкий', 'max_amount_per_application': Decimal('75'), 'max_term_days': 25, 'min_yield': Decimal('18')},
        'balanced': {'min_rating': 'B+', 'max_risk': 'Средний', 'max_amount_per_application': Decimal('150'), 'max_term_days': 40, 'min_yield': Decimal('22')},
        'aggressive': {'min_rating': 'B', 'max_risk': 'Высокий', 'max_amount_per_application': Decimal('250'), 'max_term_days': 60, 'min_yield': Decimal('28')},
    }
    data = presets.get(profile, presets['balanced'])
    rule.is_enabled = True
    rule.min_rating = data['min_rating']
    rule.max_risk = data['max_risk']
    rule.max_amount_per_application = data['max_amount_per_application']
    rule.max_term_days = data['max_term_days']
    rule.min_yield = data['min_yield']
    db.session.add(AuditLog(actor_id=current_user.id, action=f'autoinvest_profile_{profile}', entity='AutoInvestRule', entity_id=rule.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Профиль умного автоинвеста применён.', 'success')
    return redirect(url_for('loans.dashboard'))

@loans_bp.route('/investor/autoinvest/run', methods=['POST'])
@login_required
def run_autoinvest_now():
    if not current_user.is_investor:
        abort(403)
    executed = execute_auto_investments()
    db.session.commit()
    flash(f'Автоинвестирование выполнено. Создано инвестиций: {executed}.', 'success' if executed else 'info')
    return redirect(url_for('loans.dashboard'))


@loans_bp.route('/invest/<int:app_id>', methods=['POST'])
@login_required
def invest(app_id):
    if not current_user.is_investor:
        abort(403)
    app = db.get_or_404(LoanApplication, app_id)
    if app.status not in ['На витрине', 'Частично профинансирована']:
        flash('Эта заявка уже недоступна для финансирования.', 'warning')
        return redirect(url_for('loans.marketplace'))
    wallet = get_or_create_wallet(current_user)
    amount = min(money(request.form.get('amount')), app.remaining_to_fund)
    if amount < Decimal('10'):
        flash('Минимальная инвестиция — 10 BYN.', 'danger')
        return redirect(url_for('loans.marketplace'))
    investor_fee = (amount * Decimal('0.01')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_charge = amount + investor_fee
    if wallet.available < total_charge:
        flash(f'Недостаточно средств: с комиссией требуется {total_charge} BYN.', 'danger')
        return redirect(url_for('loans.marketplace'))
    expected_share = (Decimal(app.total_to_return) * (amount / Decimal(app.amount))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    inv = Investment(investor_id=current_user.id, amount=amount, platform_fee=investor_fee, expected_return=expected_share, external_id='INV-TEST-' + uuid.uuid4().hex[:8].upper())
    wallet.balance = Decimal(wallet.balance or 0) - amount - investor_fee
    wallet.total_invested = Decimal(wallet.total_invested or 0) + amount
    app.investments.append(inv)
    db.session.flush()
    add_platform_fee('investor_fee', app.id, investor_fee, f'Комиссия займодавца по заявке #{app.id}')
    add_ledger('investment_escrow', INVESTOR_WALLET, ESCROW_INVESTMENTS, amount, user_id=current_user.id, investment_id=inv.id, external_id=inv.external_id, comment=f'Средства займодавца зарезервированы под заявку #{app.id}')
    add_wallet_tx(wallet, 'debit', 'Инвестиция в заявку', amount + investor_fee, external_id=inv.external_id, comment=f'Заявка #{app.id}, включая комиссию {investor_fee} BYN')
    if app.remaining_to_fund <= Decimal('0.00'):
        activate_funded_application(app)
    else:
        app.status = 'Частично профинансирована'
        notify(app.user_id, 'Заявка частично профинансирована', f'Собрано {app.funded_amount} из {app.amount} BYN.', 'info')
    notify(current_user.id, 'Инвестиция принята', f'Вы профинансировали заявку #{app.id} на {amount} BYN. Комиссия платформы: {investor_fee} BYN.', 'success')
    db.session.add(AuditLog(actor_id=current_user.id, action='p2p_investment_created', entity='LoanApplication', entity_id=app.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Инвестиция TEST-режима принята.', 'success')
    return redirect(url_for('loans.dashboard'))


@loans_bp.route('/loan/<int:loan_id>')
@login_required
def loan_detail(loan_id):
    loan = Loan.query.filter_by(id=loan_id, user_id=current_user.id).first_or_404()
    transactions = Transaction.query.filter_by(loan_id=loan.id).order_by(Transaction.created_at.desc()).all()
    investments = Investment.query.filter_by(application_id=loan.application_id).all()
    schedule = []
    start = loan.issued_at.date()
    total_days = max((loan.due_date - start).days, 1)
    for i in range(1, min(total_days, 12) + 1):
        day = start + timedelta(days=round(total_days * i / min(total_days, 12)))
        accrued = (Decimal(loan.principal) + Decimal(loan.principal) * Decimal(loan.daily_rate) * Decimal((day - start).days)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        schedule.append({'date': day, 'amount': accrued})
    return render_template('loans/loan_detail.html', loan=loan, transactions=transactions, schedule=schedule, investments=investments)


@loans_bp.route('/repay/<int:loan_id>', methods=['POST'])
@login_required
def repay(loan_id):
    loan = Loan.query.filter_by(id=loan_id, user_id=current_user.id).first_or_404()
    if loan.status not in ['Активный', 'Просрочен']:
        flash('Этот займ уже нельзя погашать.', 'warning')
        return redirect(url_for('loans.loan_detail', loan_id=loan.id))
    amount = money(request.form.get('amount'))
    if amount <= 0:
        flash('Введите сумму платежа.', 'danger')
        return redirect(url_for('loans.loan_detail', loan_id=loan.id))
    if amount > loan.balance:
        flash(f'Сумма платежа не может превышать остаток {loan.balance} BYN.', 'danger')
        return redirect(url_for('loans.loan_detail', loan_id=loan.id))
    external_id = 'REPAY-TEST-' + uuid.uuid4().hex[:8].upper()
    tx = Transaction(loan_id=loan.id, user_id=current_user.id, operation_type='Погашение', amount=amount, status='Успешно', external_id=external_id, comment='Тестовое погашение через TEST-шлюз')
    add_gateway_operation('Погашение займа', current_user.id, amount, loan_id=loan.id, status='Успешно', external_id=external_id, comment='TEST: входящий платёж от заёмщика')
    add_ledger('borrower_repayment', EXTERNAL_BANK, COLLECTION_ACCOUNT, amount, user_id=current_user.id, loan_id=loan.id, external_id=external_id, comment='Платёж заёмщика принят шлюзом')
    loan.repaid_amount = Decimal(loan.repaid_amount) + amount
    notify(current_user.id, 'Платёж получен', f'Тестовый платёж на сумму {amount} BYN принят.', 'success')
    if loan.repaid_amount >= loan.total_due:
        loan.status = 'Закрыт'
        loan.application.status = 'Закрыт'
        history = get_or_create_credit_history(current_user)
        history.closed_loans += 1
        history.total_loans = max(history.total_loans, history.closed_loans)
        history.max_principal = max(Decimal(history.max_principal or 0), Decimal(loan.principal))
        history.client_level = 'VIP' if history.closed_loans >= 5 else ('Уровень 3' if history.closed_loans >= 3 else 'Уровень 2')
        for inv in loan.application.investments:
            if inv.status == 'Активна':
                investor_wallet = get_or_create_wallet(inv.investor)
                investor_wallet.balance = Decimal(investor_wallet.balance or 0) + Decimal(inv.expected_return or 0)
                investor_wallet.earned = Decimal(investor_wallet.earned or 0) + inv.investor_profit
                add_ledger('investor_return', COLLECTION_ACCOUNT, INVESTOR_WALLET, inv.expected_return, user_id=inv.investor_id, loan_id=loan.id, investment_id=inv.id, external_id='RETURN-' + str(inv.id), comment='Распределение погашения займодавцу')
                add_wallet_tx(investor_wallet, 'credit', 'Возврат инвестиции', inv.expected_return, external_id='RETURN-' + str(inv.id), comment=f'Займ #{loan.id}')
                inv.status = 'Завершена'
                notify(inv.investor_id, 'Получен возврат по займу', f'По заявке #{loan.application_id} начислен TEST-возврат {inv.expected_return} BYN. Прибыль после комиссии: {inv.investor_profit} BYN.', 'success')
        notify(current_user.id, 'Займ закрыт', f'Займ #{loan.id} закрыт. Новый тестовый лимит повторного займа: {history.repeat_limit} BYN.', 'success')
    db.session.add(tx)
    db.session.add(AuditLog(actor_id=current_user.id, action='loan_repaid_test', entity='Loan', entity_id=loan.id, ip_address=request.remote_addr))
    db.session.commit()
    flash('Тестовый платёж принят.', 'success')
    return redirect(url_for('loans.loan_detail', loan_id=loan.id))


@loans_bp.route('/loan/<int:loan_id>/extend', methods=['POST'])
@login_required
def extend_loan(loan_id):
    loan = Loan.query.filter_by(id=loan_id, user_id=current_user.id).first_or_404()
    if loan.status not in ['Активный', 'Просрочен']:
        flash('Продлить можно только активный или просроченный займ.', 'warning')
        return redirect(url_for('loans.loan_detail', loan_id=loan.id))
    days = parse_int(request.form.get('days'), 7)
    if days not in [7, 14, 30]:
        flash('Можно выбрать продление на 7, 14 или 30 дней.', 'danger')
        return redirect(url_for('loans.loan_detail', loan_id=loan.id))
    fee = loan.extension_fee
    loan.due_date = loan.due_date + timedelta(days=days)
    loan.extension_count += 1
    loan.status = 'Активный'
    tx = Transaction(loan_id=loan.id, user_id=current_user.id, operation_type='Продление займа', amount=fee, status='Успешно', comment=f'Тестовая комиссия за продление на {days} дней')
    db.session.add(tx)
    add_platform_fee('extension_fee', loan.id, fee, 'Комиссия/платёж за продление TEST')
    add_ledger('extension_fee_payment', EXTERNAL_BANK, PLATFORM_REVENUE, fee, user_id=current_user.id, loan_id=loan.id, external_id='EXT-' + str(loan.id) + '-' + uuid.uuid4().hex[:6].upper(), comment='Комиссия за продление займа')
    notify(current_user.id, 'Займ продлён', f'Срок займа #{loan.id} продлён на {days} дней. Тестовая комиссия: {fee} BYN.', 'success')
    db.session.commit()
    flash('Займ тестово продлён.', 'success')
    return redirect(url_for('loans.loan_detail', loan_id=loan.id))


@loans_bp.route('/documents')
@login_required
def documents_center():
    seed_legal_templates()
    if current_user.is_investor:
        docs = DealDocument.query.filter_by(investor_id=current_user.id).order_by(DealDocument.created_at.desc()).all()
        loans = []
    else:
        loans = Loan.query.filter_by(user_id=current_user.id).order_by(Loan.issued_at.desc()).all()
        docs = DealDocument.query.filter_by(borrower_id=current_user.id).order_by(DealDocument.created_at.desc()).all()
    templates = LegalDocumentTemplate.query.filter_by(is_active=True).order_by(LegalDocumentTemplate.document_type, LegalDocumentTemplate.title).all()
    return render_template('loans/documents_center.html', loans=loans, docs=docs, templates=templates)


@loans_bp.route('/document/<int:doc_id>')
@login_required
def deal_document(doc_id):
    doc = db.get_or_404(DealDocument, doc_id)
    if not (current_user.is_admin or doc.borrower_id == current_user.id or doc.investor_id == current_user.id):
        abort(403)
    return render_template('loans/deal_document.html', doc=doc)


@loans_bp.route('/document/<int:doc_id>/sign', methods=['POST'])
@login_required
def sign_document(doc_id):
    doc = db.get_or_404(DealDocument, doc_id)
    if not (current_user.is_admin or doc.borrower_id == current_user.id or doc.investor_id == current_user.id):
        abort(403)
    try:
        signature_event = sign_document_test(doc, current_user, request.remote_addr, request.headers.get('User-Agent'))
    except PermissionError:
        abort(403)
    if signature_event is None:
        flash('Эта сторона уже подписала документ.', 'info')
        return redirect(url_for('loans.deal_document', doc_id=doc.id))
    db.session.add(AuditLog(actor_id=current_user.id, action='legal_document_signed_test', entity='DealDocument', entity_id=doc.id, ip_address=request.remote_addr))
    notify(doc.borrower_id, 'Документ подписан', f'Документ #{doc.id} получил TEST-подпись через SMS/МСИ/ЭЦП контур.', 'success')
    if doc.investor_id:
        notify(doc.investor_id, 'Документ подписан', f'Документ #{doc.id} получил TEST-подпись через SMS/МСИ/ЭЦП контур.', 'success')
    db.session.commit()
    flash('TEST-подпись зафиксирована. В реальном проекте здесь должен быть официальный провайдер SMS/МСИ/ЭЦП/НЦЭУ.', 'success')
    return redirect(url_for('loans.deal_document', doc_id=doc.id))


@loans_bp.route('/loan/<int:loan_id>/contract')
@login_required
def loan_contract(loan_id):
    loan = db.get_or_404(Loan, loan_id)
    if not (current_user.is_admin or loan.user_id == current_user.id or any(i.investor_id == current_user.id for i in loan.application.investments)):
        abort(403)
    docs = DealDocument.query.filter_by(loan_id=loan.id).order_by(DealDocument.document_type, DealDocument.created_at).all()
    if not docs:
        ensure_loan_documents(loan)
        db.session.commit()
        docs = DealDocument.query.filter_by(loan_id=loan.id).order_by(DealDocument.document_type, DealDocument.created_at).all()
    return render_template('loans/contract.html', loan=loan, docs=docs)


@loans_bp.route('/security')
@login_required
def security():
    logs = AuditLog.query.filter_by(actor_id=current_user.id).order_by(AuditLog.created_at.desc()).limit(30).all()
    sessions = [{'device': 'Windows / Chrome', 'ip': request.remote_addr or '127.0.0.1', 'status': 'Текущая сессия'}, {'device': 'Android / Mobile browser', 'ip': '192.168.0.24', 'status': 'TEST-история'}]
    return render_template('loans/security.html', logs=logs, sessions=sessions)


@loans_bp.route('/support', methods=['GET', 'POST'])
@login_required
def support_center():
    from app.models import SupportTicket, SupportMessage
    from app.operations import create_ticket
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        category = request.form.get('category') or 'general'
        priority = request.form.get('priority') or 'medium'
        message = request.form.get('message', '').strip()
        if not subject or not message:
            flash('Укажите тему и сообщение.', 'warning')
            return redirect(url_for('loans.support_center'))
        ticket = create_ticket(current_user, subject=subject, category=category, priority=priority, message=message)
        db.session.add(AuditLog(actor_id=current_user.id, action='support_ticket_created', entity='SupportTicket', entity_id=ticket.id, ip_address=request.remote_addr))
        db.session.commit()
        flash('Обращение создано. Оператор увидит его в Operations Core.', 'success')
        return redirect(url_for('loans.support_ticket', ticket_id=ticket.id))
    tickets = SupportTicket.query.filter_by(user_id=current_user.id).order_by(SupportTicket.updated_at.desc()).all()
    return render_template('loans/support.html', tickets=tickets)


@loans_bp.route('/support/<int:ticket_id>', methods=['GET', 'POST'])
@login_required
def support_ticket(ticket_id):
    from app.models import SupportTicket, SupportMessage
    ticket = db.get_or_404(SupportTicket, ticket_id)
    if ticket.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    if request.method == 'POST':
        message = request.form.get('message', '').strip()
        if message:
            db.session.add(SupportMessage(ticket_id=ticket.id, author_id=current_user.id, message=message, is_internal=False))
            if ticket.status == 'Закрыта':
                ticket.status = 'Новая'
                ticket.closed_at = None
            else:
                ticket.status = 'В работе'
            db.session.add(AuditLog(actor_id=current_user.id, action='support_ticket_user_reply', entity='SupportTicket', entity_id=ticket.id, ip_address=request.remote_addr))
            db.session.commit()
            flash('Сообщение отправлено.', 'success')
        return redirect(url_for('loans.support_ticket', ticket_id=ticket.id))
    return render_template('loans/support_detail.html', ticket=ticket)
