import os
from app import create_app, db
from app.models import User, Loan
from app.demo_seed import create_demo_data
from app.legal import seed_legal_templates, ensure_loan_documents
from app.compliance import run_bulk_compliance
from app.operations import seed_response_templates, seed_demo_operations
from app.communications import seed_message_templates
from app.antifraud import seed_demo_antifraud
from app.collections import seed_demo_collections

app = create_app()


def create_admin():
    if not User.query.filter_by(email='admin@kopilka.test').first():
        admin = User(email='admin@kopilka.test', full_name='Администратор Kopilka', role='admin')
        admin.set_password('admin12345')
        db.session.add(admin)
        db.session.commit()
        print('Создан админ: admin@kopilka.test / admin12345')


@app.cli.command('init-db')
def init_db():
    db.create_all()
    create_admin()
    seed_legal_templates()
    seed_response_templates()
    seed_message_templates()
    db.session.commit()
    print('База данных готова.')


@app.cli.command('reset-db')
def reset_db():
    db.drop_all()
    db.create_all()
    print('Старая структура базы удалена.')
    create_admin()
    demo = create_demo_data(reset_existing=True)
    seed_legal_templates()
    created_docs = 0
    for loan in Loan.query.all():
        created_docs += len(ensure_loan_documents(loan))
    db.session.commit()
    compliance_created = run_bulk_compliance(None)
    admin = User.query.filter_by(email='admin@kopilka.test').first()
    operations_created = seed_demo_operations(admin)
    communications_templates = seed_message_templates()
    antifraud_created = seed_demo_antifraud(admin)
    collections_created = seed_demo_collections(admin)
    db.session.commit()
    print('Новая база данных создана.')
    print(f'Юридический контур: создано документов сделок: {created_docs}')
    print(f'Комплаенс-контур: создано тестовых кейсов: {compliance_created}')
    print(f'Операционный контур: создано обращений: {operations_created}')
    print(f'Контур коммуникаций: шаблонов сообщений: {communications_templates}')
    print(f'Антифрод-контур: создано тестовых событий: {antifraud_created}')
    print(f'Контур взыскания: создано или обновлено дел: {collections_created}')
    print(f"DEMO-данные: {demo['borrowers']} заёмщиков, {demo['investors']} займодавцев, {demo['applications']} заявок. Пароль: {demo['password']}")


@app.cli.command('seed-demo')
def seed_demo():
    db.create_all()
    create_admin()
    demo = create_demo_data(reset_existing=True)
    seed_legal_templates()
    created_docs = 0
    for loan in Loan.query.all():
        created_docs += len(ensure_loan_documents(loan))
    db.session.commit()
    compliance_created = run_bulk_compliance(None)
    admin = User.query.filter_by(email='admin@kopilka.test').first()
    operations_created = seed_demo_operations(admin)
    communications_templates = seed_message_templates()
    antifraud_created = seed_demo_antifraud(admin)
    collections_created = seed_demo_collections(admin)
    db.session.commit()
    print(f'Юридический контур: создано документов сделок: {created_docs}')
    print(f'Комплаенс-контур: создано тестовых кейсов: {compliance_created}')
    print(f'Операционный контур: создано обращений: {operations_created}')
    print(f'Контур коммуникаций: шаблонов сообщений: {communications_templates}')
    print(f'Антифрод-контур: создано тестовых событий: {antifraud_created}')
    print(f'Контур взыскания: создано или обновлено дел: {collections_created}')
    print(f"DEMO-данные созданы: {demo['borrowers']} заёмщиков, {demo['investors']} займодавцев, {demo['applications']} заявок.")
    print('Пароль для всех демонстрационных аккаунтов: ' + demo['password'])


if __name__ == '__main__':
    debug_enabled = os.environ.get('FLASK_DEBUG', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug_enabled)
