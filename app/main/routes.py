from decimal import Decimal
from flask import Blueprint, render_template
from app.models import User, LoanApplication, Investment, Loan, PlatformLedger

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    funded_apps = LoanApplication.query.filter(LoanApplication.status.in_(['Профинансирована', 'Активный займ', 'Закрыта'])).all()
    active_apps = LoanApplication.query.filter(LoanApplication.status.in_(['На витрине', 'Частично профинансирована', 'Одобрено'])).order_by(LoanApplication.created_at.desc()).limit(6).all()
    total_funded = sum((Decimal(a.funded_amount or 0) for a in funded_apps), Decimal('0.00'))
    investors = User.query.filter_by(role='investor').count()
    borrowers = User.query.filter_by(role='client').count()
    active_loans = Loan.query.filter(Loan.status.in_(['Активный', 'Просрочен'])).count()
    avg_yield = Decimal('0.00')
    apps_with_yield = LoanApplication.query.filter(LoanApplication.scoring_score.isnot(None)).all()
    if apps_with_yield:
        avg_yield = sum((Decimal(a.annual_yield or 0) for a in apps_with_yield), Decimal('0.00')) / Decimal(len(apps_with_yield))
    platform_income = sum((Decimal(x.amount or 0) for x in PlatformLedger.query.all()), Decimal('0.00'))
    stats = {
        'total_funded': total_funded.quantize(Decimal('0.01')),
        'investors': investors,
        'borrowers': borrowers,
        'active_loans': active_loans,
        'avg_yield': avg_yield.quantize(Decimal('0.01')),
        'platform_income': platform_income.quantize(Decimal('0.01')),
    }
    return render_template('main/index.html', stats=stats, featured_applications=active_apps)


@main_bp.route('/documents')
def documents():
    return render_template('main/documents.html')


@main_bp.route('/platform-analytics')
def platform_analytics():
    applications = LoanApplication.query.all()
    investments = Investment.query.all()
    loans = Loan.query.all()
    platform_income = sum((Decimal(x.amount or 0) for x in PlatformLedger.query.all()), Decimal('0.00'))
    total_invested = sum((Decimal(i.amount or 0) for i in investments), Decimal('0.00'))
    expected_return = sum((Decimal(i.expected_return or 0) for i in investments), Decimal('0.00'))
    active_loans = len([l for l in loans if l.status in ['Активный', 'Просрочен']])
    overdue_loans = len([l for l in loans if l.status == 'Просрочен'])
    closed_loans = len([l for l in loans if l.status == 'Закрыт'])
    grades = ['A+', 'A', 'B+', 'B', 'C', 'D']
    grade_values = []
    for grade in grades:
        grade_values.append(sum(1 for a in applications if a.rating_grade == grade))
    marketplace = LoanApplication.query.filter(LoanApplication.status.in_(['На витрине', 'Частично профинансирована'])).count()
    returned_rate = Decimal('96.80')
    if active_loans + closed_loans:
        returned_rate = (Decimal(closed_loans) / Decimal(max(1, active_loans + closed_loans)) * Decimal('100')).quantize(Decimal('0.01'))
    stats = {
        'total_invested': total_invested.quantize(Decimal('0.01')),
        'expected_return': expected_return.quantize(Decimal('0.01')),
        'platform_income': platform_income.quantize(Decimal('0.01')),
        'investors': User.query.filter_by(role='investor').count(),
        'borrowers': User.query.filter_by(role='client').count(),
        'applications': len(applications),
        'marketplace': marketplace,
        'active_loans': active_loans,
        'overdue_loans': overdue_loans,
        'closed_loans': closed_loans,
        'returned_rate': returned_rate,
    }
    return render_template('main/platform_analytics.html', stats=stats, grade_labels=grades, grade_values=grade_values)
