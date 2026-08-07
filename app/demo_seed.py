from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
import random
import uuid

from app.time_utils import utc_now
from app import db
from app.models import (
    User, ClientProfile, LoanApplication, Loan, Transaction, Notification,
    NotificationPreference, CommunicationLog, CreditHistory, Wallet, Investment,
    AutoInvestRule, PlatformLedger, AuditLog, LedgerEntry, PaymentGatewayOperation,
    WalletTransaction, SignatureEvent, DealDocument, ComplianceDecision,
    ComplianceFlag, ComplianceCase, SupportMessage, SupportTicket,
    ManualReviewItem, OperatorNote, AntifraudDecision, AntifraudEvent,
    CollectionAction, CollectionCase,
)

PASSWORD = 'demo12345'

PURPOSES = [
    'Ремонт автомобиля', 'Покупка бытовой техники', 'Медицинские расходы',
    'Оплата обучения', 'Срочный семейный расход', 'Ремонт квартиры',
    'Покупка телефона', 'Оплата коммунальных платежей', 'Рабочий инструмент',
    'Небольшой бизнес-оборот'
]
REGIONS = ['Минск', 'Гомель', 'Брест', 'Витебск', 'Гродно', 'Могилёв', 'Минская область']
EMPLOYMENT = ['Работа по найму', 'ИП / самозанятый', 'Пенсионер']
RISKS = ['Низкий', 'Средний', 'Высокий']
STATUSES = ['На витрине', 'Частично профинансирована', 'Активный займ', 'Закрыт', 'Просрочен', 'На проверке']


def q(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def total_to_return(amount, term_days, daily_rate):
    amount = q(amount)
    return (amount + amount * Decimal(str(daily_rate)) * Decimal(int(term_days))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def make_user(email, name, role, phone):
    user = User.query.filter_by(email=email).first()
    if user:
        return user
    user = User(email=email, full_name=name, phone=phone, role=role)
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.flush()
    return user


def create_demo_data(reset_existing=True):
    """Создаёт живую P2P-демо-базу: 10 заёмщиков, 10 займодавцев, заявки, инвестиции и транзакции."""
    random.seed(42)

    if reset_existing:
        # Полностью очищаем пользовательский демонстрационный контур. Администраторы и справочные
        # шаблоны сохраняются, чтобы после пересборки можно было сразу войти в систему.
        for model in [
            CollectionAction, CollectionCase,
            AntifraudDecision, AntifraudEvent,
            OperatorNote, SupportMessage, SupportTicket, ManualReviewItem,
            ComplianceDecision, ComplianceFlag, ComplianceCase,
            SignatureEvent, DealDocument,
            AuditLog, CommunicationLog,
            LedgerEntry, PaymentGatewayOperation, WalletTransaction, PlatformLedger,
            AutoInvestRule, Investment, Wallet, CreditHistory,
            NotificationPreference, Notification,
            Transaction, Loan, LoanApplication, ClientProfile,
        ]:
            db.session.query(model).delete()
        db.session.query(User).filter(User.role != 'admin').delete(synchronize_session=False)
        db.session.commit()

    investors = []
    for i in range(1, 11):
        investor = make_user(
            f'investor{i}@kopilka.test',
            f'Займодавец Demo {i}',
            'investor',
            f'+37529110{str(i).zfill(4)}'
        )
        balance = q(random.randint(8000, 65000))
        wallet = Wallet(user_id=investor.id, balance=balance, reserved=0, earned=q(random.randint(250, 6500)), total_invested=q(random.randint(2000, 30000)), provider_reference='BANK-TEST-WALLET')
        db.session.add(wallet)
        rule = AutoInvestRule(
            investor_id=investor.id,
            is_enabled=(i % 3 != 0),
            min_rating=random.choice(['A+', 'A', 'B+', 'B']),
            max_risk=random.choice(['Низкий', 'Средний']),
            max_amount_per_application=q(random.choice([50, 100, 150, 250, 500])),
            max_term_days=random.choice([14, 30, 45, 60]),
            min_yield=q(random.choice([20, 24, 28, 32]))
        )
        db.session.add(rule)
        db.session.add(Notification(user_id=investor.id, title='Демо-аккаунт займодавца готов', message='Это тестовый кабинет займодавца с кошельком, автоинвестом и портфелем.', notification_type='success'))
        investors.append(investor)

    borrowers = []
    for i in range(1, 11):
        borrower = make_user(
            f'borrower{i}@kopilka.test',
            f'Заёмщик Demo {i}',
            'client',
            f'+37529220{str(i).zfill(4)}'
        )
        profile = ClientProfile(
            user_id=borrower.id,
            msi_verified=True,
            msi_reference='MSI-TEST-' + uuid.uuid4().hex[:10].upper(),
            msi_verified_at=utc_now() - timedelta(days=random.randint(1, 90)),
            msi_provider='МСИ TEST',
            age_group=random.choice(['18-24', '25-55', '25-55', '25-55', '56-65']),
            region=random.choice(REGIONS),
            employment_type=random.choice(EMPLOYMENT),
            work_experience_months=random.randint(8, 180),
            monthly_income=q(random.randint(900, 3800)),
            monthly_expenses=q(random.randint(300, 1800)),
            marital_status=random.choice(['Не женат/не замужем', 'Женат/замужем', 'Разведён/разведена']),
            children_count=random.randint(0, 3),
            card_mask='**** **** **** ' + str(random.randint(1000, 9999)),
            card_token='CARD-TEST-' + uuid.uuid4().hex[:10].upper(),
            card_verified=True,
            consent_personal_data=True,
            consent_scoring=True,
            consent_msi=True,
        )
        db.session.add(profile)
        hist = CreditHistory(
            user_id=borrower.id,
            total_loans=random.randint(0, 8),
            closed_loans=random.randint(0, 6),
            overdue_count=random.choice([0, 0, 0, 1, 2]),
            max_principal=q(random.choice([300, 500, 800, 1200, 2000, 3000])),
            client_level=random.choice(['Новичок', 'Уровень 2', 'Уровень 3', 'VIP']),
            rating=random.randint(520, 940)
        )
        db.session.add(hist)
        db.session.add(Notification(user_id=borrower.id, title='Демо-аккаунт заёмщика готов', message='Это тестовый кабинет заёмщика с МСИ TEST, заявками и историей.', notification_type='success'))
        borrowers.append(borrower)

    db.session.flush()

    applications = []
    # Создаём 50 заявок с разными состояниями финансирования и погашения.
    for idx in range(1, 51):
        borrower = random.choice(borrowers)
        amount = q(random.choice([100, 150, 200, 300, 500, 700, 1000, 1500, 2000, 3000]))
        term = random.choice([7, 14, 21, 30, 45, 60])
        rate = Decimal(str(random.choice([0.006, 0.007, 0.008, 0.009, 0.01, 0.011])))
        score = random.randint(480, 960)
        if score >= 820:
            risk = 'Низкий'
        elif score >= 620:
            risk = 'Средний'
        else:
            risk = 'Высокий'
        if idx <= 16:
            status = 'На витрине'
        elif idx <= 30:
            status = 'Частично профинансирована'
        elif idx <= 40:
            status = 'Активный займ'
        elif idx <= 46:
            status = 'Закрыт'
        elif idx <= 48:
            status = 'Просрочен'
        else:
            status = 'На проверке'

        app = LoanApplication(
            user_id=borrower.id,
            amount=amount,
            term_days=term,
            daily_rate=rate,
            total_to_return=total_to_return(amount, term, rate),
            status=status,
            purpose=random.choice(PURPOSES),
            scoring_score=score,
            scoring_decision='Допущена на витрину' if score >= 700 else ('Ручная проверка' if score >= 500 else 'Автоотказ'),
            scoring_details='DEMO: тестовый скоринг, МСИ TEST, карта токенизирована, паспортные данные не хранятся.',
            max_approved_amount=q(min(5000, int(amount) + random.randint(200, 1500))),
            risk_level=risk,
            created_at=utc_now() - timedelta(days=random.randint(1, 50)),
        )
        db.session.add(app)
        applications.append(app)

    db.session.flush()

    # Распределяем вложения по части заявок и оставляем разные проценты финансирования.
    active_statuses = ['Частично профинансирована', 'Активный займ', 'Закрыт', 'Просрочен']
    for app in applications:
        if app.status not in active_statuses:
            continue
        target_ratio = {
            'Частично профинансирована': random.uniform(0.15, 0.92),
            'Активный займ': 1.0,
            'Закрыт': 1.0,
            'Просрочен': 1.0,
        }[app.status]
        target = (Decimal(app.amount) * Decimal(str(target_ratio))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        left = target
        picked = random.sample(investors, random.randint(2, min(8, len(investors))))
        for investor in picked:
            if left <= 0:
                break
            raw = q(random.choice([25, 50, 75, 100, 150, 200, 300, 500, 700]))
            amount = min(raw, left, Decimal(app.amount))
            if amount < Decimal('10'):
                continue
            fee = (amount * Decimal('0.01')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            expected = (Decimal(app.total_to_return) * (amount / Decimal(app.amount))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            inv_status = 'Завершена' if app.status == 'Закрыт' else 'Активна'
            inv = Investment(
                application_id=app.id,
                investor_id=investor.id,
                amount=amount,
                platform_fee=fee,
                expected_return=expected,
                status=inv_status,
                external_id='DEMO-INV-' + uuid.uuid4().hex[:8].upper(),
                created_at=app.created_at + timedelta(hours=random.randint(1, 72))
            )
            db.session.add(inv)
            wallet = investor.wallet
            if wallet:
                wallet.balance = q(Decimal(wallet.balance or 0) - amount - fee)
                wallet.total_invested = q(Decimal(wallet.total_invested or 0) + amount)
                if inv_status == 'Завершена':
                    wallet.balance = q(Decimal(wallet.balance or 0) + expected)
                    wallet.earned = q(Decimal(wallet.earned or 0) + (expected - amount - fee))
            db.session.add(PlatformLedger(source_type='demo_investor_fee', source_id=app.id, amount=fee, comment=f'DEMO комиссия займодавца по заявке #{app.id}'))
            db.session.add(Transaction(user_id=investor.id, operation_type='DEMO инвестиция в заявку', amount=amount, status='Успешно', external_id=inv.external_id, comment=f'Заявка #{app.id}'))
            left -= amount

    db.session.flush()

    # Создаём займы для полностью профинансированных, закрытых и просроченных заявок.
    for app in applications:
        if app.status not in ['Активный займ', 'Закрыт', 'Просрочен']:
            continue
        issued = utc_now() - timedelta(days=random.randint(5, 75))
        due = (issued + timedelta(days=app.term_days)).date()
        if app.status == 'Просрочен':
            due = date.today() - timedelta(days=random.randint(2, 16))
        loan = Loan(
            application_id=app.id,
            user_id=app.user_id,
            principal=app.amount,
            daily_rate=app.daily_rate,
            issued_at=issued,
            due_date=due,
            status='Активный' if app.status == 'Активный займ' else app.status,
            repaid_amount=0,
            penalty_amount=q(random.randint(20, 180)) if app.status == 'Просрочен' else 0,
            extension_count=random.choice([0, 0, 1, 2]),
            last_penalty_date=due if app.status == 'Просрочен' else None,
        )
        if app.status == 'Закрыт':
            loan.repaid_amount = total_to_return(app.amount, app.term_days, app.daily_rate)
        db.session.add(loan)
        db.session.flush()
        db.session.add(Transaction(loan_id=loan.id, user_id=app.user_id, operation_type='DEMO выдача P2P-займа', amount=app.amount, status='Успешно', external_id='DEMO-PAYOUT-' + uuid.uuid4().hex[:8].upper(), comment='TEST банк-партнёр'))
        if app.status == 'Закрыт':
            db.session.add(Transaction(loan_id=loan.id, user_id=app.user_id, operation_type='DEMO погашение', amount=loan.repaid_amount, status='Успешно', external_id='DEMO-REPAY-' + uuid.uuid4().hex[:8].upper(), comment='TEST платёжный шлюз'))
        if app.status == 'Просрочен':
            db.session.add(Transaction(loan_id=loan.id, user_id=app.user_id, operation_type='DEMO начисление просрочки', amount=loan.penalty_amount, status='Успешно', external_id='DEMO-PENALTY-' + uuid.uuid4().hex[:8].upper(), comment='TEST просрочка'))

    db.session.add(PlatformLedger(source_type='demo_system', source_id=None, amount=q(0), comment='DEMO-данные созданы: 10 заёмщиков, 10 займодавцев, 50 заявок'))
    db.session.commit()

    from app.finance import rebuild_finance_ledger_from_existing_data
    rebuild_finance_ledger_from_existing_data()

    return {
        'borrowers': 10,
        'investors': 10,
        'applications': 50,
        'password': PASSWORD,
    }
