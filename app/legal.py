from datetime import datetime
import uuid
from decimal import Decimal, ROUND_HALF_UP
from flask import render_template_string
from app.time_utils import utc_now
from app import db
from app.models import LegalDocumentTemplate, DealDocument, SignatureEvent, Investment, User


DEFAULT_LEGAL_TEMPLATES = [
    {
        'code': 'platform_rules_p2p',
        'title': 'Правила P2P-платформы Kopilka',
        'document_type': 'platform_rules',
        'version': '1.0',
        'requires_signature': False,
        'content': '''
<h2>Правила P2P-платформы Kopilka</h2>
<p>Kopilka TEST является демонстрационной P2P-площадкой, где заёмщики размещают заявки, а займодавцы финансируют их полностью или частично.</p>
<ul>
  <li>Платформа не хранит паспортные данные, сканы документов и полные номера банковских карт.</li>
  <li>Идентификация в реальном проекте выполняется через МСИ, банк или официальный KYC-провайдер.</li>
  <li>Платёжные операции в реальном проекте выполняются через банк-партнёр/платёжного провайдера.</li>
  <li>Договоры займа заключаются между заёмщиком и конкретными займодавцами, профинансировавшими заявку.</li>
</ul>
'''
    },
    {
        'code': 'personal_data_consent',
        'title': 'Согласие на обработку данных',
        'document_type': 'consent',
        'version': '1.0',
        'requires_signature': True,
        'content': '''
<h2>Согласие на обработку данных</h2>
<p>Пользователь разрешает Kopilka TEST обрабатывать технические и анкетные данные, необходимые для демонстрации заявки, скоринга, уведомлений, кошелька и юридического контура.</p>
<p><b>Важно:</b> паспортные данные, сканы документов, селфи с документом и полный номер карты не сохраняются в системе Kopilka.</p>
<p>В реальной эксплуатации персональные сведения должны обрабатываться в рамках договоров с официальными провайдерами, МСИ, банком и платёжной инфраструктурой.</p>
'''
    },
    {
        'code': 'scoring_consent',
        'title': 'Согласие на скоринг и антифрод-проверки',
        'document_type': 'consent',
        'version': '1.0',
        'requires_signature': True,
        'content': '''
<h2>Согласие на скоринг</h2>
<p>Пользователь разрешает платформе рассчитывать тестовый рейтинг по данным анкеты, истории займов, возвратов, просрочек и техническим признакам.</p>
<p>В реальном проекте источники скоринга должны подключаться по договорам с бюро кредитных историй, МСИ, банком, антифрод-провайдерами и другими официальными участниками.</p>
'''
    },
    {
        'code': 'loan_contract_p2p',
        'title': 'Договор займа P2P',
        'document_type': 'loan_contract',
        'version': '1.0',
        'requires_signature': True,
        'content': '''
<h2>Договор займа P2P №{{ loan.id }}</h2>
<p><b>Заёмщик:</b> {{ borrower.full_name }} / User ID {{ borrower.id }}</p>
<p><b>Займодавец:</b> {{ investor.full_name if investor else 'несколько займодавцев по реестру инвестиций' }}</p>
<p><b>Платформа:</b> Kopilka TEST, оператор P2P-площадки.</p>
<table class="legal-table">
  <tr><th>Сумма займа</th><td>{{ loan.principal }} BYN</td></tr>
  <tr><th>Срок</th><td>до {{ loan.due_date }}</td></tr>
  <tr><th>Ставка</th><td>{{ (loan.daily_rate * 100)|round(3) }}% в день</td></tr>
  <tr><th>Сумма к возврату</th><td>{{ loan.total_due }} BYN</td></tr>
  <tr><th>Заявка</th><td>#{{ loan.application_id }}</td></tr>
</table>
<p>Денежные средства предоставляются займодавцами, профинансировавшими заявку заёмщика. Платформа не является стороной, выдающей собственные деньги в этой P2P-модели, а обеспечивает технологический контур, расчёты, документы и учёт комиссий.</p>
<p>В реальном проекте подписание выполняется через SMS/МСИ/ЭЦП/НЦЭУ или иной официальный механизм, согласованный с юристами и провайдерами.</p>
'''
    },
    {
        'code': 'payment_schedule',
        'title': 'График платежей',
        'document_type': 'payment_schedule',
        'version': '1.0',
        'requires_signature': False,
        'content': '''
<h2>График платежей по займу №{{ loan.id }}</h2>
<p>Сумма займа: {{ loan.principal }} BYN. Плановая дата возврата: {{ loan.due_date }}. Плановая сумма к возврату: {{ loan.total_due }} BYN.</p>
<p>В demo-версии график является справочным. В реальном проекте он должен формироваться по утверждённым правилам договора и расчётной модели.</p>
'''
    },
]


def seed_legal_templates():
    for item in DEFAULT_LEGAL_TEMPLATES:
        existing = LegalDocumentTemplate.query.filter_by(code=item['code']).first()
        if existing:
            existing.title = item['title']
            existing.version = item['version']
            existing.document_type = item['document_type']
            existing.content = item['content']
            existing.requires_signature = item['requires_signature']
            existing.is_active = True
        else:
            db.session.add(LegalDocumentTemplate(**item))
    db.session.flush()


def render_legal_content(template, loan=None, investor=None):
    borrower = loan.user if loan else None
    return render_template_string(template.content, loan=loan, borrower=borrower, investor=investor)


def ensure_loan_documents(loan):
    seed_legal_templates()
    created = []
    templates = LegalDocumentTemplate.query.filter_by(is_active=True).all()
    loan_templates = [t for t in templates if t.document_type in ['loan_contract', 'payment_schedule']]
    investments = Investment.query.filter_by(application_id=loan.application_id).all()
    investors = [inv.investor for inv in investments if inv.investor] or [None]

    for template in loan_templates:
        if template.document_type == 'loan_contract':
            for investor in investors:
                existing = DealDocument.query.filter_by(loan_id=loan.id, template_id=template.id, investor_id=(investor.id if investor else None)).first()
                if existing:
                    continue
                doc = DealDocument(
                    loan_id=loan.id,
                    application_id=loan.application_id,
                    borrower_id=loan.user_id,
                    investor_id=investor.id if investor else None,
                    template_id=template.id,
                    title=f'{template.title} №{loan.id}' + (f' / {investor.full_name}' if investor else ''),
                    document_type=template.document_type,
                    version=template.version,
                    status='Ожидает подписи',
                    html_snapshot=render_legal_content(template, loan=loan, investor=investor),
                    external_signature_ref='SIGN-TEST-' + uuid.uuid4().hex[:10].upper(),
                )
                db.session.add(doc)
                created.append(doc)
        else:
            existing = DealDocument.query.filter_by(loan_id=loan.id, template_id=template.id, investor_id=None).first()
            if existing:
                continue
            doc = DealDocument(
                loan_id=loan.id,
                application_id=loan.application_id,
                borrower_id=loan.user_id,
                template_id=template.id,
                title=f'{template.title} №{loan.id}',
                document_type=template.document_type,
                version=template.version,
                status='Сформирован',
                html_snapshot=render_legal_content(template, loan=loan),
            )
            db.session.add(doc)
            created.append(doc)
    db.session.flush()
    return created


def sign_document_test(document, signer, ip_address=None, user_agent=None):
    now = utc_now()
    if signer.role == 'client' and signer.id == document.borrower_id:
        if document.signed_by_borrower_at:
            return None
        document.signed_by_borrower_at = now
        role = 'borrower'
    elif signer.role == 'investor' and signer.id == document.investor_id:
        if document.signed_by_investor_at:
            return None
        document.signed_by_investor_at = now
        role = 'investor'
    elif signer.role == 'admin':
        if document.signed_by_platform_at:
            return None
        document.signed_by_platform_at = now
        role = 'platform'
    else:
        raise PermissionError('Нет прав подписывать этот документ.')

    event = SignatureEvent(
        document_id=document.id,
        signer_id=signer.id,
        signer_role=role,
        provider='SMS/МСИ/ЭЦП TEST',
        external_ref='SIGN-EVENT-' + uuid.uuid4().hex[:10].upper(),
        status='Успешно',
        ip_address=ip_address,
        user_agent=(user_agent or '')[:255],
    )
    db.session.add(event)

    if document.document_type == 'loan_contract':
        if document.signed_by_borrower_at and document.signed_by_investor_at and document.signed_by_platform_at:
            document.status = 'Подписан'
    else:
        document.status = 'Подписан'
    return event


def legal_summary():
    return {
        'templates': LegalDocumentTemplate.query.count(),
        'active_templates': LegalDocumentTemplate.query.filter_by(is_active=True).count(),
        'documents': DealDocument.query.count(),
        'waiting': DealDocument.query.filter_by(status='Ожидает подписи').count(),
        'signed': DealDocument.query.filter_by(status='Подписан').count(),
        'signature_events': SignatureEvent.query.count(),
    }
