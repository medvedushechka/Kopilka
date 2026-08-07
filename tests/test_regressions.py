import re
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from flask import g
from sqlalchemy.pool import NullPool

from app.time_utils import utc_now
from app import create_app, db
from app.legal import ensure_loan_documents, sign_document_test
from app.communications import notify_user
from app.collections import close_collection_case, run_collections_cycle
from app.finance import rebuild_finance_ledger_from_existing_data
from app.models import (
    AntifraudEvent,
    CollectionAction,
    CollectionCase,
    CommunicationLog,
    ComplianceCase,
    CreditHistory,
    DealDocument,
    Investment,
    LedgerEntry,
    Loan,
    LoanApplication,
    NotificationPreference,
    Notification,
    PaymentGatewayOperation,
    PlatformLedger,
    SignatureEvent,
    SupportMessage,
    SupportTicket,
    Transaction,
    User,
    Wallet,
    WalletTransaction,
)


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)

        class TestConfig:
            TESTING = True
            SECRET_KEY = 'test-secret-key'
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'test.sqlite3'}"
            SQLALCHEMY_TRACK_MODIFICATIONS = False
            # В тестах не держим SQLite-соединения в пуле: Windows блокирует открытый файл базы.
            SQLALCHEMY_ENGINE_OPTIONS = {'poolclass': NullPool}
            WTF_CSRF_ENABLED = True
            WTF_CSRF_TIME_LIMIT = None
            UPLOAD_FOLDER = str(root / 'uploads')
            LOG_FOLDER = str(root / 'logs')
            MAX_CONTENT_LENGTH = 8 * 1024 * 1024
            SESSION_COOKIE_HTTPONLY = True
            SESSION_COOKIE_SAMESITE = 'Lax'
            SESSION_COOKIE_SECURE = False
            REMEMBER_COOKIE_HTTPONLY = True
            REMEMBER_COOKIE_SAMESITE = 'Lax'
            REMEMBER_COOKIE_SECURE = False

        self.app = create_app(TestConfig)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self._create_users()

    def tearDown(self):
        # Сначала закрываем сессию, удаляем схему и освобождаем все соединения.
        # Без dispose() Windows не позволяет удалить временный SQLite-файл.
        db.session.remove()
        db.drop_all()
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()

    def _create_users(self):
        admin = User(email='admin@test.local', full_name='Администратор', role='admin')
        borrower = User(email='borrower@test.local', full_name='Заёмщик', role='client')
        investor = User(email='investor@test.local', full_name='Займодавец', role='investor')
        for user in (admin, borrower, investor):
            user.set_password('test-password')
            db.session.add(user)
        db.session.flush()

        db.session.add_all([
            Wallet(user_id=investor.id, balance=Decimal('0.00')),
            CreditHistory(user_id=borrower.id),
            NotificationPreference(user_id=admin.id, enabled=False),
            NotificationPreference(user_id=borrower.id, enabled=False),
            NotificationPreference(user_id=investor.id, enabled=False),
        ])
        db.session.commit()

    @staticmethod
    def _csrf_token(response):
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if not match:
            raise AssertionError('CSRF-токен не найден в HTML-форме.')
        return match.group(1).decode()

    def _login(self, client, email):
        response = client.get('/auth/login')
        token = self._csrf_token(response)
        response = client.post(
            '/auth/login',
            data={
                'email': email,
                'password': 'test-password',
                'csrf_token': token,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_external_referrer_cannot_control_post_redirect(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        notification = Notification(
            user_id=borrower.id,
            title='Проверка возврата',
            message='Тест безопасной переадресации.',
        )
        db.session.add(notification)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, borrower.email)
            token = self._csrf_token(client.get('/cabinet/notifications'))
            response = client.post(
                f'/cabinet/notifications/{notification.id}/read',
                data={'csrf_token': token},
                headers={'Referer': 'https://example.invalid/phishing'},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers['Location'].endswith('/cabinet/notifications'))
            self.assertNotIn('example.invalid', response.headers['Location'])

    def test_full_demo_reset_removes_stale_data_and_rebuilds_modules(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        stale_ticket = SupportTicket(
            user_id=borrower.id,
            subject='Устаревшее обращение перед пересборкой',
            category='general',
            priority='low',
            status='Новая',
        )
        db.session.add(stale_ticket)
        db.session.commit()
        db.session.expunge_all()

        with self.app.test_client() as client:
            self._login(client, 'admin@test.local')
            token = self._csrf_token(client.get('/admin/'))
            response = client.post(
                '/admin/demo/seed',
                data={'csrf_token': token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        self.assertEqual(
            SupportTicket.query.filter_by(subject='Устаревшее обращение перед пересборкой').count(),
            0,
        )
        self.assertEqual(User.query.filter_by(role='client').count(), 10)
        self.assertEqual(User.query.filter_by(role='investor').count(), 10)
        self.assertGreater(SupportTicket.query.count(), 0)
        self.assertGreater(ComplianceCase.query.count(), 0)
        self.assertGreater(AntifraudEvent.query.count(), 0)
        self.assertGreater(CollectionCase.query.count(), 0)
        self.assertGreater(DealDocument.query.count(), 0)

    def test_csrf_blocks_requests_without_token(self):
        with self.app.test_client() as client:
            response = client.post(
                '/auth/login',
                data={'email': 'admin@test.local', 'password': 'test-password'},
            )
            self.assertEqual(response.status_code, 400)

            self._login(client, 'admin@test.local')
            response = client.post('/auth/logout')
            self.assertEqual(response.status_code, 400)

    def test_admin_login_redirects_to_admin_dashboard(self):
        with self.app.test_client() as client:
            response = client.get('/auth/login')
            token = self._csrf_token(response)
            response = client.post(
                '/auth/login',
                data={
                    'email': 'admin@test.local',
                    'password': 'test-password',
                    'csrf_token': token,
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers['Location'].endswith('/admin/'))

    def test_registration_rejects_invalid_email(self):
        with self.app.test_client() as client:
            token = self._csrf_token(client.get('/auth/register'))
            response = client.post(
                '/auth/register',
                data={
                    'full_name': 'Тестовый пользователь',
                    'email': 'not-an-email',
                    'phone': '',
                    'role': 'client',
                    'password': 'test-password',
                    'csrf_token': token,
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn('Введите корректный email'.encode('utf-8'), response.data)
            self.assertIsNone(User.query.filter_by(full_name='Тестовый пользователь').first())

    def test_admin_cannot_open_borrower_profile_or_application(self):
        with self.app.test_client() as client:
            self._login(client, 'admin@test.local')
            self.assertEqual(client.get('/cabinet/profile').status_code, 403)
            self.assertEqual(client.get('/cabinet/apply').status_code, 403)

    def test_antifraud_block_terminates_session_and_unblock_restores_access(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        event = AntifraudEvent(
            user_id=borrower.id,
            event_type='manual_test',
            severity='danger',
            fraud_score=95,
            title='Тест блокировки',
            description='Проверка реальной блокировки аккаунта.',
            provider_reference='AF-TEST-BLOCK',
        )
        db.session.add(event)
        db.session.commit()

        borrower_client = self.app.test_client()
        admin_client = self.app.test_client()
        self._login(admin_client, 'admin@test.local')
        g.pop('_login_user', None)
        g.pop('csrf_token', None)
        self._login(borrower_client, borrower.email)

        g.pop('_login_user', None)
        g.pop('csrf_token', None)
        token = self._csrf_token(admin_client.get('/admin/antifraud'))
        response = admin_client.post(
            f'/admin/antifraud/event/{event.id}/block',
            data={'csrf_token': token, 'comment': 'Регрессионный тест'},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertTrue(db.session.get(User, borrower.id).is_blocked)

        g.pop('_login_user', None)
        g.pop('csrf_token', None)
        response = borrower_client.get('/cabinet/', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/auth/login'))

        g.pop('_login_user', None)
        g.pop('csrf_token', None)
        token = self._csrf_token(admin_client.get('/admin/antifraud'))
        response = admin_client.post(
            f'/admin/antifraud/user/{borrower.id}/unblock',
            data={'csrf_token': token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertFalse(db.session.get(User, borrower.id).is_blocked)

        g.pop('_login_user', None)
        g.pop('csrf_token', None)
        response = borrower_client.get('/auth/login')
        token = self._csrf_token(response)
        response = borrower_client.post(
            '/auth/login',
            data={
                'email': borrower.email,
                'password': 'test-password',
                'csrf_token': token,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/cabinet/'))

    def test_notification_category_preferences_control_external_delivery(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        pref = NotificationPreference.query.filter_by(user_id=borrower.id).one()
        pref.enabled = True
        pref.email_enabled = True
        pref.sms_enabled = False
        pref.push_enabled = False
        pref.loan_events = False
        pref.payment_events = True
        db.session.commit()

        notify_user(borrower.id, 'Заявка обновлена', 'Событие займа.', category='loan')
        notify_user(borrower.id, 'Платёж принят', 'Событие платежа.', category='payment')
        db.session.commit()

        self.assertEqual(Notification.query.filter_by(user_id=borrower.id).count(), 2)
        self.assertEqual(
            CommunicationLog.query.filter_by(user_id=borrower.id, category='loan').count(),
            0,
        )
        self.assertEqual(
            CommunicationLog.query.filter_by(user_id=borrower.id, category='payment', channel='email').count(),
            1,
        )

    def test_closed_support_ticket_reopens_without_stale_closed_date(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        ticket = SupportTicket(
            user_id=borrower.id,
            subject='Закрытое обращение',
            category='general',
            priority='medium',
            status='Закрыта',
            closed_at=utc_now(),
        )
        db.session.add(ticket)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, borrower.email)
            token = self._csrf_token(client.get(f'/cabinet/support/{ticket.id}'))
            response = client.post(
                f'/cabinet/support/{ticket.id}',
                data={'message': 'Нужно снова открыть обращение.', 'csrf_token': token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        ticket = db.session.get(SupportTicket, ticket.id)
        self.assertEqual(ticket.status, 'Новая')
        self.assertIsNone(ticket.closed_at)
        self.assertEqual(SupportMessage.query.filter_by(ticket_id=ticket.id).count(), 1)

    def test_collections_cycle_does_not_duplicate_actions_or_reopen_closed_case(self):
        admin = User.query.filter_by(email='admin@test.local').one()
        borrower = User.query.filter_by(email='borrower@test.local').one()
        application = self._create_application(borrower, Decimal('200.00'), status='Активный займ')
        loan = Loan(
            application=application,
            user_id=borrower.id,
            principal=Decimal('200.00'),
            daily_rate=Decimal('0.008'),
            issued_at=utc_now() - timedelta(days=11),
            due_date=date.today() - timedelta(days=1),
        )
        db.session.add(loan)
        db.session.commit()

        run_collections_cycle(admin)
        db.session.commit()
        case = CollectionCase.query.filter_by(loan_id=loan.id).one()
        self.assertEqual(
            CollectionAction.query.filter(
                CollectionAction.case_id == case.id,
                CollectionAction.action_type.like('auto_%'),
            ).count(),
            1,
        )

        run_collections_cycle(admin)
        db.session.commit()
        self.assertEqual(
            CollectionAction.query.filter(
                CollectionAction.case_id == case.id,
                CollectionAction.action_type.like('auto_%'),
            ).count(),
            1,
        )

        close_collection_case(case, admin, 'Долг урегулирован.')
        close_collection_case(case, admin, 'Повторное закрытие не должно дублироваться.')
        db.session.commit()
        run_collections_cycle(admin)
        db.session.commit()
        db.session.expire_all()
        case = db.session.get(CollectionCase, case.id)
        self.assertEqual(case.status, 'Закрыта')
        self.assertEqual(case.stage, 'closed')
        self.assertEqual(CollectionAction.query.filter_by(case_id=case.id, action_type='case_closed').count(), 1)
        self.assertEqual(
            CollectionAction.query.filter(
                CollectionAction.case_id == case.id,
                CollectionAction.action_type.like('auto_%'),
            ).count(),
            1,
        )

    def test_finance_rebuild_preserves_statuses_and_does_not_duplicate_fee(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        investor = User.query.filter_by(email='investor@test.local').one()
        application = self._create_application(borrower, Decimal('100.00'), status='Активный займ')
        investment = Investment(
            investor_id=investor.id,
            amount=Decimal('100.00'),
            platform_fee=Decimal('1.00'),
            expected_return=Decimal('108.00'),
            external_id='INV-REBUILD-TEST',
        )
        application.investments.append(investment)
        loan = Loan(
            application=application,
            user_id=borrower.id,
            principal=Decimal('100.00'),
            daily_rate=Decimal('0.008'),
            due_date=date.today() + timedelta(days=10),
        )
        db.session.add(loan)
        db.session.flush()
        db.session.add(Transaction(
            loan_id=loan.id,
            user_id=borrower.id,
            operation_type='Выдача P2P-займа',
            amount=Decimal('100.00'),
            status='В обработке',
            external_id='PAYOUT-REBUILD-TEST',
        ))
        db.session.add(PlatformLedger(
            source_type='investor_fee',
            source_id=application.id,
            amount=Decimal('1.00'),
            comment='Комиссия тестовой инвестиции',
        ))
        db.session.commit()

        rebuild_finance_ledger_from_existing_data()
        db.session.expire_all()

        gateway = PaymentGatewayOperation.query.filter_by(external_id='PAYOUT-REBUILD-TEST').one()
        self.assertEqual(gateway.status, 'В обработке')
        payout_entry = LedgerEntry.query.filter_by(external_id='PAYOUT-REBUILD-TEST').one()
        self.assertEqual(payout_entry.status, 'pending')
        wallet_tx = WalletTransaction.query.filter_by(external_id='INV-REBUILD-TEST').one()
        self.assertEqual(Decimal(wallet_tx.amount), Decimal('101.00'))
        self.assertEqual(LedgerEntry.query.filter_by(operation_type='platform_fee_existing').count(), 1)
        self.assertEqual(LedgerEntry.query.filter_by(operation_type='investor_platform_fee').count(), 0)

    def test_investment_fee_cannot_make_wallet_negative(self):
        investor = User.query.filter_by(email='investor@test.local').one()
        borrower = User.query.filter_by(email='borrower@test.local').one()
        investor.wallet.balance = Decimal('100.00')
        application = self._create_application(borrower, Decimal('100.00'))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, investor.email)
            token = self._csrf_token(client.get('/cabinet/marketplace'))
            response = client.post(
                f'/cabinet/invest/{application.id}',
                data={'amount': '100', 'csrf_token': token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        self.assertEqual(Decimal(investor.wallet.balance), Decimal('100.00'))
        self.assertEqual(Investment.query.filter_by(application_id=application.id).count(), 0)

    def test_full_funding_creates_complete_financial_package(self):
        investor = User.query.filter_by(email='investor@test.local').one()
        borrower = User.query.filter_by(email='borrower@test.local').one()
        investor.wallet.balance = Decimal('101.00')
        application = self._create_application(borrower, Decimal('100.00'))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, investor.email)
            token = self._csrf_token(client.get('/cabinet/marketplace'))
            response = client.post(
                f'/cabinet/invest/{application.id}',
                data={'amount': '100', 'csrf_token': token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        application = db.session.get(LoanApplication, application.id)
        self.assertIsNotNone(application.loan)
        self.assertEqual(application.status, 'Активный займ')
        self.assertGreaterEqual(DealDocument.query.filter_by(loan_id=application.loan.id).count(), 2)
        self.assertEqual(PaymentGatewayOperation.query.filter_by(loan_id=application.loan.id).count(), 1)
        investment = application.investments[0]
        self.assertEqual(
            LedgerEntry.query.filter_by(
                operation_type='investment_escrow',
                investment_id=investment.id,
            ).count(),
            1,
        )
        self.assertEqual(Decimal(investor.wallet.balance), Decimal('0.00'))

    def test_manual_issue_is_recorded_in_gateway_and_ledger(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        application = self._create_application(borrower, Decimal('300.00'), status='На проверке')
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, 'admin@test.local')
            token = self._csrf_token(client.get(f'/admin/application/{application.id}'))
            response = client.post(
                f'/admin/application/{application.id}/approve',
                data={'csrf_token': token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

            db.session.expire_all()
            application = db.session.get(LoanApplication, application.id)
            self.assertIsNotNone(application.loan)
            gateway = PaymentGatewayOperation.query.filter_by(loan_id=application.loan.id).one()
            self.assertEqual(gateway.status, 'В обработке')
            self.assertEqual(
                LedgerEntry.query.filter_by(
                    loan_id=application.loan.id,
                    operation_type='manual_borrower_payout_created',
                ).count(),
                1,
            )
            self.assertEqual(
                PlatformLedger.query.filter_by(
                    source_type='manual_borrower_fee',
                    source_id=application.id,
                ).count(),
                1,
            )

            transaction = Transaction.query.filter_by(loan_id=application.loan.id).one()
            token = self._csrf_token(client.get('/admin/'))
            response = client.post(
                f'/admin/transaction/{transaction.id}/success',
                data={'csrf_token': token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        self.assertEqual(db.session.get(Transaction, transaction.id).status, 'Успешно')
        self.assertEqual(db.session.get(PaymentGatewayOperation, gateway.id).status, 'Успешно')

    def test_funded_application_cannot_be_rejected(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        investor = User.query.filter_by(email='investor@test.local').one()
        application = self._create_application(
            borrower,
            Decimal('100.00'),
            status='Частично профинансирована',
        )
        application.investments.append(Investment(
            investor_id=investor.id,
            amount=Decimal('10.00'),
            platform_fee=Decimal('0.10'),
            expected_return=Decimal('10.80'),
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, 'admin@test.local')
            token = self._csrf_token(client.get(f'/admin/application/{application.id}'))
            response = client.post(
                f'/admin/application/{application.id}/reject',
                data={'csrf_token': token, 'admin_comment': 'Проверка ограничения'},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        self.assertEqual(
            db.session.get(LoanApplication, application.id).status,
            'Частично профинансирована',
        )

    def test_repeated_signature_does_not_create_duplicate_event(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        application = self._create_application(borrower, Decimal('100.00'), status='Активный займ')
        loan = Loan(
            application=application,
            user_id=borrower.id,
            principal=Decimal('100.00'),
            daily_rate=Decimal('0.008'),
            due_date=date.today() + timedelta(days=10),
        )
        db.session.add(loan)
        db.session.flush()
        ensure_loan_documents(loan)
        document = DealDocument.query.filter_by(loan_id=loan.id).first()

        first_event = sign_document_test(document, borrower)
        second_event = sign_document_test(document, borrower)
        db.session.commit()

        self.assertIsNotNone(first_event)
        self.assertIsNone(second_event)
        self.assertEqual(SignatureEvent.query.filter_by(document_id=document.id).count(), 1)

    def test_overpayment_is_rejected_without_side_effects(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        application = self._create_application(borrower, Decimal('100.00'), status='Активный займ')
        loan = Loan(
            application=application,
            user_id=borrower.id,
            principal=Decimal('100.00'),
            daily_rate=Decimal('0.008'),
            due_date=date.today() + timedelta(days=10),
        )
        db.session.add(loan)
        db.session.commit()
        initial_repaid = Decimal(loan.repaid_amount)

        with self.app.test_client() as client:
            self._login(client, borrower.email)
            token = self._csrf_token(client.get(f'/cabinet/loan/{loan.id}'))
            response = client.post(
                f'/cabinet/repay/{loan.id}',
                data={'amount': str(loan.balance + Decimal('1.00')), 'csrf_token': token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        loan = db.session.get(Loan, loan.id)
        self.assertEqual(Decimal(loan.repaid_amount), initial_repaid)
        self.assertEqual(Transaction.query.filter_by(loan_id=loan.id, operation_type='Погашение').count(), 0)

    def test_overdue_history_is_incremented_once(self):
        borrower = User.query.filter_by(email='borrower@test.local').one()
        application = self._create_application(borrower, Decimal('200.00'), status='Активный займ')
        loan = Loan(
            application=application,
            user_id=borrower.id,
            principal=Decimal('200.00'),
            daily_rate=Decimal('0.008'),
            issued_at=utc_now() - timedelta(days=8),
            due_date=date.today() - timedelta(days=1),
        )
        db.session.add(loan)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, 'admin@test.local')
            for _ in range(2):
                token = self._csrf_token(client.get('/admin/'))
                response = client.post(
                    '/admin/overdue/check',
                    data={'csrf_token': token},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        history = CreditHistory.query.filter_by(user_id=borrower.id).one()
        self.assertEqual(history.overdue_count, 1)

    @staticmethod
    def _create_application(borrower, amount, status='На витрине'):
        application = LoanApplication(
            user_id=borrower.id,
            amount=amount,
            term_days=10,
            daily_rate=Decimal('0.008'),
            total_to_return=(amount * Decimal('1.08')).quantize(Decimal('0.01')),
            status=status,
            purpose='Регрессионный тест',
            scoring_score=900,
            scoring_decision='Допущена на витрину',
            risk_level='Низкий',
            max_approved_amount=Decimal('500.00'),
        )
        db.session.add(application)
        db.session.flush()
        return application


if __name__ == '__main__':
    unittest.main()
