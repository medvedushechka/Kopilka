from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import uuid

from app.time_utils import utc_now
from app import db
from app.models import (
    LedgerEntry, PaymentGatewayOperation, WalletTransaction,
    PlatformLedger, Wallet, Investment, Loan, Transaction
)

PLATFORM_REVENUE = 'platform:revenue'
ESCROW_INVESTMENTS = 'escrow:investments'
COLLECTION_ACCOUNT = 'settlement:collections'
BORROWER_PAYOUT = 'settlement:borrower_payout'
EXTERNAL_BANK = 'external:bank_or_payment_provider'
INVESTOR_WALLET = 'wallet:investor'
BORROWER_OBLIGATION = 'borrower:loan_obligation'


def q(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def new_external_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:10].upper()}'


def add_ledger(operation_type, debit_account, credit_account, amount, *, user_id=None, loan_id=None,
               investment_id=None, external_id=None, comment='', status='posted', key=None):
    amount = q(amount)
    if amount <= 0:
        return None
    entry = LedgerEntry(
        operation_type=operation_type,
        debit_account=debit_account,
        credit_account=credit_account,
        amount=amount,
        status=status,
        user_id=user_id,
        loan_id=loan_id,
        investment_id=investment_id,
        external_id=external_id,
        idempotency_key=key or new_external_id('LEDGER'),
        comment=comment,
    )
    db.session.add(entry)
    return entry


def add_gateway_operation(operation_type, user_id, amount, *, loan_id=None, provider='BANK/PAYMENT TEST',
                          status='Создан', external_id=None, comment='', request_payload=None,
                          response_payload=None):
    op = PaymentGatewayOperation(
        user_id=user_id,
        loan_id=loan_id,
        operation_type=operation_type,
        provider=provider,
        amount=q(amount),
        status=status,
        external_id=external_id or new_external_id('PAY'),
        comment=comment,
        request_payload=request_payload,
        response_payload=response_payload,
        processed_at=utc_now() if status in ['Успешно', 'Ошибка', 'Возврат'] else None,
    )
    db.session.add(op)
    return op


def add_wallet_tx(wallet, direction, operation_type, amount, *, status='Успешно', external_id=None, comment=''):
    if not wallet:
        return None
    tx = WalletTransaction(
        wallet_id=wallet.id,
        user_id=wallet.user_id,
        direction=direction,
        operation_type=operation_type,
        amount=q(amount),
        balance_after=q(wallet.balance),
        status=status,
        external_id=external_id,
        comment=comment,
    )
    db.session.add(tx)
    return tx


def add_platform_fee(source_type, source_id, amount, comment):
    amount = q(amount)
    if amount <= 0:
        return None
    fee = PlatformLedger(source_type=source_type, source_id=source_id, amount=amount, comment=comment)
    db.session.add(fee)
    add_ledger('platform_fee', 'client/investor:settlement', PLATFORM_REVENUE, amount,
               external_id=new_external_id('FEE'), comment=comment)
    return fee


def finance_summary():
    platform_income = db.session.query(db.func.coalesce(db.func.sum(PlatformLedger.amount), 0)).scalar() or 0
    total_wallet_balances = db.session.query(db.func.coalesce(db.func.sum(Wallet.balance), 0)).scalar() or 0
    active_investments = db.session.query(db.func.coalesce(db.func.sum(Investment.amount), 0)).filter(Investment.status == 'Активна').scalar() or 0
    total_expected_return = db.session.query(db.func.coalesce(db.func.sum(Investment.expected_return), 0)).scalar() or 0
    loan_principal = db.session.query(db.func.coalesce(db.func.sum(Loan.principal), 0)).filter(Loan.status.in_(['Активный', 'Просрочен'])).scalar() or 0
    borrower_paid = db.session.query(db.func.coalesce(db.func.sum(Loan.repaid_amount), 0)).scalar() or 0
    gateway_success = db.session.query(db.func.coalesce(db.func.sum(PaymentGatewayOperation.amount), 0)).filter(PaymentGatewayOperation.status == 'Успешно').scalar() or 0
    gateway_processing = PaymentGatewayOperation.query.filter(PaymentGatewayOperation.status.in_(['Создан', 'В обработке'])).count()
    ledger_posted = db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.amount), 0)).filter(LedgerEntry.status == 'posted').scalar() or 0
    return {
        'platform_income': q(platform_income),
        'total_wallet_balances': q(total_wallet_balances),
        'active_investments': q(active_investments),
        'total_expected_return': q(total_expected_return),
        'loan_principal': q(loan_principal),
        'borrower_paid': q(borrower_paid),
        'gateway_success': q(gateway_success),
        'gateway_processing': gateway_processing,
        'ledger_posted': q(ledger_posted),
    }


def reconciliation_report():
    wallets = Wallet.query.all()
    rows = []
    warnings = 0
    for wallet in wallets:
        tx_sum_credit = db.session.query(db.func.coalesce(db.func.sum(WalletTransaction.amount), 0)).filter_by(wallet_id=wallet.id, direction='credit').scalar() or 0
        tx_sum_debit = db.session.query(db.func.coalesce(db.func.sum(WalletTransaction.amount), 0)).filter_by(wallet_id=wallet.id, direction='debit').scalar() or 0
        calculated_flow = q(Decimal(str(tx_sum_credit)) - Decimal(str(tx_sum_debit)))
        balance = q(wallet.balance)
        # В демо-данных у кошельков есть стартовый баланс, поэтому это не строгая бухгалтерская сверка.
        status = 'OK'
        if balance < 0:
            status = 'NEGATIVE_BALANCE'
            warnings += 1
        rows.append({
            'user': wallet.user,
            'balance': balance,
            'available': wallet.available,
            'earned': q(wallet.earned),
            'total_invested': q(wallet.total_invested),
            'calculated_flow': calculated_flow,
            'status': status,
        })
    return {'rows': rows, 'warnings': warnings}


def rebuild_finance_ledger_from_existing_data():
    """Восстанавливает демонстрационный финансовый журнал по существующим операциям. Не предназначено для промышленной эксплуатации."""
    LedgerEntry.query.delete()
    PaymentGatewayOperation.query.delete()
    WalletTransaction.query.delete()
    db.session.flush()

    for fee in PlatformLedger.query.all():
        add_ledger('platform_fee_existing', 'client/investor:settlement', PLATFORM_REVENUE, fee.amount,
                   external_id=new_external_id('FEE'), comment=fee.comment or fee.source_type)

    topup_transactions = Transaction.query.filter_by(operation_type='Пополнение кошелька займодавца').order_by(Transaction.created_at).all()
    for tx in topup_transactions:
        user_wallet = Wallet.query.filter_by(user_id=tx.user_id).first()
        external_id = tx.external_id or new_external_id('TOPUP')
        add_gateway_operation(
            'Пополнение кошелька',
            tx.user_id,
            tx.amount,
            status=tx.status,
            external_id=external_id,
            comment=tx.comment or 'Восстановлено из журнала транзакций',
        )
        add_ledger(
            'wallet_topup',
            EXTERNAL_BANK,
            INVESTOR_WALLET,
            tx.amount,
            user_id=tx.user_id,
            external_id=external_id,
            status='posted' if tx.status == 'Успешно' else 'pending',
            comment=tx.comment or 'Пополнение кошелька',
        )
        if user_wallet:
            add_wallet_tx(user_wallet, 'credit', 'Пополнение кошелька', tx.amount,
                          status=tx.status, external_id=external_id, comment=tx.comment or '')

    for inv in Investment.query.all():
        wallet = inv.investor.wallet if inv.investor else None
        add_ledger('investment_escrow', INVESTOR_WALLET, ESCROW_INVESTMENTS, inv.amount,
                   user_id=inv.investor_id, investment_id=inv.id, external_id=inv.external_id,
                   comment=f'Инвестиция в заявку #{inv.application_id}')
        if wallet:
            total_charge = q(inv.amount) + q(inv.platform_fee)
            add_wallet_tx(wallet, 'debit', 'Инвестиция в P2P-заявку', total_charge,
                          external_id=inv.external_id, comment=f'Заявка #{inv.application_id}')

    for loan in Loan.query.all():
        issue_tx = Transaction.query.filter(
            Transaction.loan_id == loan.id,
            Transaction.operation_type.in_(['Выдача P2P-займа', 'Ручная тестовая выдача займа']),
        ).order_by(Transaction.created_at.asc()).first()
        payout_status = issue_tx.status if issue_tx else 'Успешно'
        payout_external_id = issue_tx.external_id if issue_tx and issue_tx.external_id else new_external_id('PAYOUT')
        payout_comment = issue_tx.comment if issue_tx and issue_tx.comment else 'DEMO: выплата через банк-партнёр после полного финансирования'
        op = add_gateway_operation('Выдача займа заёмщику', loan.user_id, loan.principal, loan_id=loan.id,
                                   status=payout_status, external_id=payout_external_id,
                                   comment=payout_comment)
        add_ledger('borrower_payout', ESCROW_INVESTMENTS, BORROWER_PAYOUT, loan.principal,
                   user_id=loan.user_id, loan_id=loan.id, external_id=op.external_id,
                   status='posted' if payout_status == 'Успешно' else 'pending',
                   comment='Выплата заёмщику')

        repayment_transactions = Transaction.query.filter_by(
            loan_id=loan.id,
            operation_type='Погашение',
        ).order_by(Transaction.created_at).all()
        for repay_tx in repayment_transactions:
            repay_external_id = repay_tx.external_id or new_external_id('REPAY')
            repay_op = add_gateway_operation('Погашение займа', loan.user_id, repay_tx.amount, loan_id=loan.id,
                                             status=repay_tx.status, external_id=repay_external_id,
                                             comment=repay_tx.comment or 'DEMO: входящий платёж через платёжный шлюз')
            add_ledger('borrower_repayment', EXTERNAL_BANK, COLLECTION_ACCOUNT, repay_tx.amount,
                       user_id=loan.user_id, loan_id=loan.id, external_id=repay_op.external_id,
                       status='posted' if repay_tx.status == 'Успешно' else 'pending',
                       comment='Погашение заёмщиком')

        for inv in loan.application.investments if loan.application else []:
            if inv.status == 'Завершена':
                add_ledger('investor_payout', COLLECTION_ACCOUNT, INVESTOR_WALLET, inv.expected_return,
                           user_id=inv.investor_id, loan_id=loan.id, investment_id=inv.id,
                           external_id=new_external_id('INVRETURN'), comment='Выплата инвестору')
                wallet = inv.investor.wallet if inv.investor else None
                if wallet:
                    add_wallet_tx(wallet, 'credit', 'Возврат инвестиции', inv.expected_return,
                                  external_id='RETURN-' + str(inv.id), comment=f'Займ #{loan.id}')
    db.session.commit()
