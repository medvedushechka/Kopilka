from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app.time_utils import utc_now
from app import db, login_manager


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(180), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(180), nullable=False)
    phone = db.Column(db.String(40))
    role = db.Column(db.String(20), default='client', nullable=False)
    is_blocked = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    profile = db.relationship('ClientProfile', backref='user', uselist=False, cascade='all, delete-orphan')
    applications = db.relationship('LoanApplication', backref='user', lazy=True)
    loans = db.relationship('Loan', backref='user', lazy=True)
    notifications = db.relationship('Notification', backref='user', lazy=True, cascade='all, delete-orphan')
    credit_history = db.relationship('CreditHistory', backref='user', uselist=False, cascade='all, delete-orphan')
    wallet = db.relationship('Wallet', backref='user', uselist=False, cascade='all, delete-orphan')
    investments = db.relationship('Investment', backref='investor', lazy=True, cascade='all, delete-orphan')
    autoinvest_rules = db.relationship('AutoInvestRule', backref='investor', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_investor(self):
        return self.role == 'investor'

    @property
    def is_borrower(self):
        return self.role == 'client'


class ClientProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Паспортные данные и сканы документов здесь не храним.
    # В реальном проекте идентификация проходит через МСИ/банк/официального провайдера.
    # На сайте остаются только признаки успешной проверки и внешний идентификатор операции.
    msi_verified = db.Column(db.Boolean, default=False, nullable=False)
    msi_reference = db.Column(db.String(120))
    msi_verified_at = db.Column(db.DateTime)
    msi_provider = db.Column(db.String(80), default='МСИ TEST')

    age_group = db.Column(db.String(40))
    region = db.Column(db.String(120))
    employment_type = db.Column(db.String(80))
    work_experience_months = db.Column(db.Integer, default=0)
    monthly_income = db.Column(db.Numeric(12, 2))
    monthly_expenses = db.Column(db.Numeric(12, 2))
    marital_status = db.Column(db.String(60))
    children_count = db.Column(db.Integer, default=0)

    card_mask = db.Column(db.String(32))
    card_token = db.Column(db.String(120))
    card_verified = db.Column(db.Boolean, default=False, nullable=False)

    consent_personal_data = db.Column(db.Boolean, default=False, nullable=False)
    consent_scoring = db.Column(db.Boolean, default=False, nullable=False)
    consent_msi = db.Column(db.Boolean, default=False, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    @property
    def completion_percent(self):
        fields = [
            self.msi_verified, self.age_group, self.region, self.employment_type,
            self.work_experience_months, self.monthly_income, self.monthly_expenses,
            self.card_verified, self.consent_personal_data, self.consent_scoring, self.consent_msi,
        ]
        filled = sum(1 for value in fields if value not in (None, '', False))
        return int(filled / len(fields) * 100)

    @property
    def is_ready_for_application(self):
        return self.msi_verified and self.card_verified and self.consent_personal_data and self.consent_scoring and self.consent_msi


class LoanApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    term_days = db.Column(db.Integer, nullable=False)
    daily_rate = db.Column(db.Numeric(8, 4), nullable=False, default=0.008)
    total_to_return = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(40), default='На проверке', nullable=False, index=True)
    purpose = db.Column(db.String(255))
    scoring_score = db.Column(db.Integer)
    scoring_decision = db.Column(db.String(40))
    scoring_details = db.Column(db.Text)
    max_approved_amount = db.Column(db.Numeric(12, 2))
    risk_level = db.Column(db.String(40))
    admin_comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    loan = db.relationship('Loan', backref='application', uselist=False, cascade='all, delete-orphan')
    investments = db.relationship('Investment', backref='application', lazy=True, cascade='all, delete-orphan')

    @property
    def funded_amount(self):
        total = sum((Decimal(i.amount or 0) for i in self.investments if i.status in ['Активна', 'Завершена']), Decimal('0.00'))
        return total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def funding_progress(self):
        if not self.amount:
            return 0
        return min(100, int(self.funded_amount / Decimal(self.amount) * 100))

    @property
    def remaining_to_fund(self):
        return max(Decimal('0.00'), Decimal(self.amount) - self.funded_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def platform_borrower_fee(self):
        return (Decimal(self.amount) * Decimal('0.025')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def investor_count(self):
        return len([i for i in self.investments if i.status in ['Активна', 'Завершена']])

    @property
    def annual_yield(self):
        daily = Decimal(self.daily_rate or 0)
        # Доходность витрины показываем грубо для инвестора, до комиссий и налогов.
        return (daily * Decimal('365') * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def rating_grade(self):
        score = int(self.scoring_score or 0)
        if score >= 900:
            return 'A+'
        if score >= 800:
            return 'A'
        if score >= 700:
            return 'B+'
        if score >= 600:
            return 'B'
        if score >= 500:
            return 'C'
        return 'D'


class Loan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    principal = db.Column(db.Numeric(12, 2), nullable=False)
    daily_rate = db.Column(db.Numeric(8, 4), nullable=False)
    issued_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(40), default='Активный', nullable=False)
    repaid_amount = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    penalty_amount = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    extension_count = db.Column(db.Integer, default=0, nullable=False)
    last_penalty_date = db.Column(db.Date)

    transactions = db.relationship('Transaction', backref='loan', lazy=True)

    @property
    def base_interest(self):
        days = max((self.due_date - self.issued_at.date()).days, 1)
        value = Decimal(self.principal) * Decimal(self.daily_rate) * Decimal(days)
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def total_due(self):
        value = Decimal(self.principal) + self.base_interest + Decimal(self.penalty_amount or 0)
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def balance(self):
        value = self.total_due - Decimal(self.repaid_amount or 0)
        return max(value, Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def days_left(self):
        return (self.due_date - date.today()).days

    @property
    def overdue_days(self):
        return max(0, (date.today() - self.due_date).days)

    @property
    def extension_fee(self):
        value = Decimal(self.principal) * Decimal('0.08')
        return max(value, Decimal('10.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    operation_type = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(40), default='Успешно', nullable=False)
    external_id = db.Column(db.String(120))
    comment = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    notification_type = db.Column(db.String(50), default='info', nullable=False)
    category = db.Column(db.String(60), default='system', nullable=False, index=True)
    source_type = db.Column(db.String(60))
    source_id = db.Column(db.Integer)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    @property
    def category_label(self):
        return {
            'loan': 'Займы',
            'investment': 'Инвестиции',
            'payment': 'Платежи',
            'support': 'Поддержка',
            'rating': 'Рейтинг',
            'system': 'Система',
        }.get(self.category, self.category or 'Система')


class NotificationPreference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True, index=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    email_enabled = db.Column(db.Boolean, default=True, nullable=False)
    sms_enabled = db.Column(db.Boolean, default=False, nullable=False)
    push_enabled = db.Column(db.Boolean, default=True, nullable=False)
    loan_events = db.Column(db.Boolean, default=True, nullable=False)
    investment_events = db.Column(db.Boolean, default=True, nullable=False)
    payment_events = db.Column(db.Boolean, default=True, nullable=False)
    support_events = db.Column(db.Boolean, default=True, nullable=False)
    marketing_enabled = db.Column(db.Boolean, default=False, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    user = db.relationship('User', backref=db.backref('notification_preferences', uselist=False, cascade='all, delete-orphan'))


class MessageTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(80), unique=True, nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(60), default='system', nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)


class CommunicationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    channel = db.Column(db.String(30), nullable=False, index=True)
    recipient = db.Column(db.String(180), nullable=False)
    subject = db.Column(db.String(220), nullable=False)
    body = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(60), default='system', nullable=False, index=True)
    status = db.Column(db.String(40), default='created', nullable=False, index=True)
    provider = db.Column(db.String(80), default='TEST LOG PROVIDER', nullable=False)
    external_id = db.Column(db.String(120), index=True)
    source_type = db.Column(db.String(60))
    source_id = db.Column(db.Integer)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    sent_at = db.Column(db.DateTime)

    user = db.relationship('User', backref='communication_logs')


class CreditHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True, index=True)
    total_loans = db.Column(db.Integer, default=0, nullable=False)
    closed_loans = db.Column(db.Integer, default=0, nullable=False)
    overdue_count = db.Column(db.Integer, default=0, nullable=False)
    max_principal = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    client_level = db.Column(db.String(40), default='Новичок', nullable=False)
    rating = db.Column(db.Integer, default=500, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    @property
    def repeat_limit(self):
        if self.closed_loans >= 5 and self.overdue_count == 0:
            return Decimal('5000')
        if self.closed_loans >= 3:
            return Decimal('3000')
        if self.closed_loans >= 1:
            return Decimal('1000')
        return Decimal('500')


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    action = db.Column(db.String(120), nullable=False)
    entity = db.Column(db.String(80))
    entity_id = db.Column(db.Integer)
    ip_address = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class Wallet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True, index=True)
    balance = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    reserved = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    earned = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    total_invested = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    provider_reference = db.Column(db.String(120), default='BANK-TEST-WALLET')
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    @property
    def available(self):
        return max(Decimal('0.00'), Decimal(self.balance or 0) - Decimal(self.reserved or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class Investment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'), nullable=False, index=True)
    investor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    platform_fee = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    expected_return = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    status = db.Column(db.String(40), default='Активна', nullable=False)
    external_id = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    @property
    def investor_profit(self):
        return max(Decimal('0.00'), Decimal(self.expected_return or 0) - Decimal(self.amount or 0) - Decimal(self.platform_fee or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class AutoInvestRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    investor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    is_enabled = db.Column(db.Boolean, default=True, nullable=False)
    min_rating = db.Column(db.String(4), default='B', nullable=False)
    max_risk = db.Column(db.String(40), default='Средний', nullable=False)
    max_amount_per_application = db.Column(db.Numeric(12, 2), default=100, nullable=False)
    max_term_days = db.Column(db.Integer, default=30, nullable=False)
    min_yield = db.Column(db.Numeric(8, 2), default=20, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    @property
    def min_score(self):
        mapping = {'A+': 900, 'A': 800, 'B+': 700, 'B': 600, 'C': 500, 'D': 0}
        return mapping.get(self.min_rating, 600)


class PlatformLedger(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_type = db.Column(db.String(60), nullable=False)
    source_id = db.Column(db.Integer)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    comment = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

class WalletTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey('wallet.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    direction = db.Column(db.String(20), nullable=False)  # Списание, зачисление, резервирование или освобождение резерва.
    operation_type = db.Column(db.String(80), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    balance_after = db.Column(db.Numeric(12, 2))
    status = db.Column(db.String(40), default='Успешно', nullable=False)
    external_id = db.Column(db.String(120))
    comment = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class LedgerEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    operation_type = db.Column(db.String(80), nullable=False, index=True)
    debit_account = db.Column(db.String(80), nullable=False)
    credit_account = db.Column(db.String(80), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(8), default='BYN', nullable=False)
    status = db.Column(db.String(40), default='posted', nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'))
    investment_id = db.Column(db.Integer, db.ForeignKey('investment.id'))
    external_id = db.Column(db.String(120))
    idempotency_key = db.Column(db.String(120), unique=True, index=True)
    comment = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class PaymentGatewayOperation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'))
    operation_type = db.Column(db.String(80), nullable=False, index=True)
    provider = db.Column(db.String(80), default='BANK/PAYMENT TEST', nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(8), default='BYN', nullable=False)
    status = db.Column(db.String(40), default='Создан', nullable=False, index=True)
    external_id = db.Column(db.String(120), unique=True, index=True)
    request_payload = db.Column(db.Text)
    response_payload = db.Column(db.Text)
    comment = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    processed_at = db.Column(db.DateTime)



class LegalDocumentTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(80), unique=True, nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    version = db.Column(db.String(30), default='1.0', nullable=False)
    document_type = db.Column(db.String(80), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    requires_signature = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)


class DealDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'))
    application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'))
    borrower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    investor_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    template_id = db.Column(db.Integer, db.ForeignKey('legal_document_template.id'))
    title = db.Column(db.String(180), nullable=False)
    document_type = db.Column(db.String(80), nullable=False)
    version = db.Column(db.String(30), default='1.0', nullable=False)
    status = db.Column(db.String(40), default='Черновик', nullable=False, index=True)
    html_snapshot = db.Column(db.Text, nullable=False)
    external_signature_ref = db.Column(db.String(120))
    signed_by_borrower_at = db.Column(db.DateTime)
    signed_by_investor_at = db.Column(db.DateTime)
    signed_by_platform_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    borrower = db.relationship('User', foreign_keys=[borrower_id])
    investor = db.relationship('User', foreign_keys=[investor_id])
    template = db.relationship('LegalDocumentTemplate')

    @property
    def is_signed(self):
        if self.document_type == 'loan_contract':
            return bool(self.signed_by_borrower_at and self.signed_by_investor_at and self.signed_by_platform_at)
        return self.status == 'Подписан'


class SignatureEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('deal_document.id'), nullable=False, index=True)
    signer_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    signer_role = db.Column(db.String(40), nullable=False)
    provider = db.Column(db.String(80), default='SMS/МСИ TEST', nullable=False)
    external_ref = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(40), default='Успешно', nullable=False)
    ip_address = db.Column(db.String(80))
    user_agent = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    document = db.relationship('DealDocument', backref='signature_events')
    signer = db.relationship('User')

class ComplianceCase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'), index=True)
    subject_role = db.Column(db.String(30), nullable=False)  # Заёмщик или займодавец.
    provider = db.Column(db.String(120), default='AML/KYC TEST', nullable=False)
    status = db.Column(db.String(40), default='Новая', nullable=False, index=True)
    risk_level = db.Column(db.String(40), default='Низкий', nullable=False, index=True)
    risk_score = db.Column(db.Integer, default=0, nullable=False)
    checklist_json = db.Column(db.Text, nullable=False)
    provider_reference = db.Column(db.String(120), nullable=False)
    summary = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    user = db.relationship('User', foreign_keys=[user_id])
    application = db.relationship('LoanApplication')
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id])

    @property
    def risk_badge(self):
        if self.risk_level == 'Высокий':
            return 'danger'
        if self.risk_level == 'Средний':
            return 'warning'
        return 'success'


class ComplianceFlag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey('compliance_case.id'), nullable=False, index=True)
    code = db.Column(db.String(80), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    severity = db.Column(db.String(30), default='info', nullable=False)  # Информационный, предупреждающий или критический уровень.
    description = db.Column(db.Text)
    source = db.Column(db.String(120), default='TEST rule engine', nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    case = db.relationship('ComplianceCase', backref='flags')


class ComplianceDecision(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey('compliance_case.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    decision = db.Column(db.String(60), nullable=False)  # Одобрение, ручная проверка, отказ или заморозка.
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    case = db.relationship('ComplianceCase', backref='decisions')
    actor = db.relationship('User')


class SupportTicket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    related_application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'), index=True)
    subject = db.Column(db.String(220), nullable=False)
    category = db.Column(db.String(60), default='general', nullable=False, index=True)
    priority = db.Column(db.String(30), default='medium', nullable=False, index=True)
    status = db.Column(db.String(40), default='Новая', nullable=False, index=True)
    sla_due_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    user = db.relationship('User', foreign_keys=[user_id], backref='support_tickets')
    assigned_to = db.relationship('User', foreign_keys=[assigned_to_id])
    related_application = db.relationship('LoanApplication')

    @property
    def priority_label(self):
        return {'low': 'Низкий', 'medium': 'Средний', 'high': 'Высокий', 'critical': 'Критический'}.get(self.priority, self.priority)

    @property
    def is_sla_overdue(self):
        return bool(self.sla_due_at and self.status != 'Закрыта' and self.sla_due_at < utc_now())


class SupportMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('support_ticket.id'), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    message = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    ticket = db.relationship('SupportTicket', backref='messages')
    author = db.relationship('User')


class ManualReviewItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    reason = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(30), default='medium', nullable=False, index=True)
    status = db.Column(db.String(40), default='Новая', nullable=False, index=True)
    resolution = db.Column(db.Text)
    sla_due_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    closed_at = db.Column(db.DateTime)

    application = db.relationship('LoanApplication')
    borrower = db.relationship('User', foreign_keys=[borrower_id])
    assigned_to = db.relationship('User', foreign_keys=[assigned_to_id])

    @property
    def is_sla_overdue(self):
        return bool(self.sla_due_at and self.status != 'Закрыта' and self.sla_due_at < utc_now())


class OperatorNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'), index=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('support_ticket.id'), index=True)
    note = db.Column(db.Text, nullable=False)
    visibility = db.Column(db.String(30), default='internal', nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    actor = db.relationship('User', foreign_keys=[actor_id])
    user = db.relationship('User', foreign_keys=[user_id])
    application = db.relationship('LoanApplication')
    ticket = db.relationship('SupportTicket')


class ResponseTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(60), default='support', nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)


class AntifraudEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    application_id = db.Column(db.Integer, db.ForeignKey('loan_application.id'), index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)
    severity = db.Column(db.String(30), default='info', nullable=False, index=True)
    fraud_score = db.Column(db.Integer, default=0, nullable=False)
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text)
    ip_address = db.Column(db.String(80))
    device_fingerprint = db.Column(db.String(120))
    status = db.Column(db.String(40), default='Новое', nullable=False, index=True)
    provider_reference = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    user = db.relationship('User', foreign_keys=[user_id])
    application = db.relationship('LoanApplication')
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id])

    @property
    def severity_label(self):
        return {'info': 'Инфо', 'warning': 'Средний риск', 'danger': 'Высокий риск'}.get(self.severity, self.severity)

    @property
    def severity_badge(self):
        return {'info': 'success', 'warning': 'warning', 'danger': 'danger'}.get(self.severity, 'info')


class AntifraudDecision(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('antifraud_event.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    decision = db.Column(db.String(60), nullable=False)  # Одобрение, наблюдение, ограничение или блокировка.
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    event = db.relationship('AntifraudEvent', backref='decisions')
    actor = db.relationship('User')


class CollectionCase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False, unique=True, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    stage = db.Column(db.String(60), default='reminder', nullable=False, index=True)
    status = db.Column(db.String(40), default='Открыта', nullable=False, index=True)
    overdue_days = db.Column(db.Integer, default=0, nullable=False)
    outstanding_amount = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    next_action_at = db.Column(db.DateTime)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    external_partner_ref = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    closed_at = db.Column(db.DateTime)

    loan = db.relationship('Loan', backref=db.backref('collection_case', uselist=False, cascade='all, delete-orphan'))
    borrower = db.relationship('User', foreign_keys=[borrower_id])
    assigned_to = db.relationship('User', foreign_keys=[assigned_to_id])

    @property
    def stage_label(self):
        return {
            'reminder': 'Мягкое напоминание',
            'warning': 'Предупреждение',
            'claim': 'Досудебная претензия',
            'partner_transfer': 'Передача партнёру TEST',
            'closed': 'Закрыто',
        }.get(self.stage, self.stage)

    @property
    def severity_badge(self):
        if self.stage in ['claim', 'partner_transfer']:
            return 'danger'
        if self.stage == 'warning':
            return 'warning'
        return 'success'


class CollectionAction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey('collection_case.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    action_type = db.Column(db.String(80), nullable=False, index=True)
    channel = db.Column(db.String(40), default='system', nullable=False)
    title = db.Column(db.String(180), nullable=False)
    message = db.Column(db.Text)
    status = db.Column(db.String(40), default='Создано', nullable=False, index=True)
    external_ref = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    case = db.relationship('CollectionCase', backref='actions')
    actor = db.relationship('User')
