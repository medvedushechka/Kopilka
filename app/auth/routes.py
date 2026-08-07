from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user
from email_validator import EmailNotValidError, validate_email
from app import db
from app.models import User, ClientProfile, Wallet


auth_bp = Blueprint('auth', __name__)


def dashboard_endpoint(user):
    return 'admin.dashboard' if user.is_admin else 'loans.dashboard'


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for(dashboard_endpoint(current_user)))
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        role = request.form.get('role', 'client')
        password = request.form.get('password', '')
        if role not in ['client', 'investor']:
            role = 'client'
        if not full_name or not email or len(password) < 8:
            flash('Заполните ФИО, email и пароль от 8 символов.', 'danger')
            return render_template('auth/register.html')
        try:
            email = validate_email(email, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            flash('Введите корректный email.', 'danger')
            return render_template('auth/register.html')
        if User.query.filter_by(email=email).first():
            flash('Пользователь с таким email уже существует.', 'danger')
            return render_template('auth/register.html')
        user = User(full_name=full_name, email=email, phone=phone, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        if role == 'client':
            db.session.add(ClientProfile(user_id=user.id))
            next_url = 'loans.profile'
            message = 'Аккаунт заёмщика создан. Пройдите МСИ TEST и заполните анкету.'
        else:
            db.session.add(Wallet(user_id=user.id))
            next_url = 'loans.dashboard'
            message = 'Аккаунт займодавца создан. Пополните TEST-кошелёк и выберите заявки на витрине.'
        db.session.commit()
        login_user(user)
        flash(message, 'success')
        return redirect(url_for(next_url))
    return render_template('auth/register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(dashboard_endpoint(current_user)))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password) or user.is_blocked:
            flash('Неверный email/пароль или аккаунт заблокирован.', 'danger')
            return render_template('auth/login.html')
        login_user(user)
        return redirect(url_for(dashboard_endpoint(user)))
    return render_template('auth/login.html')


@auth_bp.route('/logout', methods=['POST'])
def logout():
    logout_user()
    return redirect(url_for('main.index'))
