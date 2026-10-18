from datetime import date, datetime
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session
from database import query_one, get_db
from utils.auth_helpers import hash_password, verify_password, login_required
from utils.email_helper import send_password_reset_email
from utils.input_validation import (
    normalize_email,
    validate_email,
    validate_indian_mobile,
    validate_password,
    validate_person_name,
)

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
PASSWORD_RESET_RATE_LIMIT = 5
PASSWORD_RESET_RATE_WINDOW_MINUTES = 15
PASSWORD_RESET_ATTEMPT_LIMIT = 5


def _reset_digest(value):
    key = str(current_app.config['SECRET_KEY']).encode('utf-8')
    return hmac.new(key, value.encode('utf-8'), hashlib.sha256).hexdigest()


def _password_reset_mail_configured():
    configured = all(current_app.config.get(key) for key in (
        'SMTP_HOST',
        'SMTP_PORT',
        'SMTP_USERNAME',
        'SMTP_PASSWORD',
        'SMTP_FROM_EMAIL',
        'PUBLIC_BASE_URL',
    ))
    if not configured:
        return False
    try:
        base_url = urlsplit(current_app.config['PUBLIC_BASE_URL'])
    except ValueError:
        return False
    if (
        not base_url.netloc
        or not base_url.hostname
        or base_url.username is not None
        or base_url.password is not None
    ):
        return False

    if base_url.scheme == 'https':
        return True

    return (
        current_app.config.get('APP_ENV') == 'development'
        and base_url.scheme == 'http'
        and base_url.hostname.lower() in {'127.0.0.1', 'localhost'}
    )


def _render_reset_page(valid, error=None, token=''):
    return render_template(
        'auth/reset_password.html',
        valid=valid,
        error=error,
        token=token,
    )


def _parse_date_of_birth(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None
    return parsed if parsed.isoformat() == value else None


def _render_patient_registration():
    return render_template(
        'auth/register_patient.html',
        today=date.today().isoformat()
    )


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        role = session.get('role')
        if role == 'patient':
            return redirect(url_for('patient.dashboard'))
        elif role == 'doctor':
            return redirect(url_for('doctor.dashboard'))
        elif role == 'receptionist':
            return redirect(url_for('receptionist.dashboard'))
        elif role == 'admin':
            return redirect(url_for('admin.dashboard'))

    next_param = request.args.get('next', '').strip()

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        next_url = request.form.get('next', '').strip() or next_param

        if not username or not password:
            flash('Please provide both username and password.', 'danger')
            return render_template('auth/login.html', next=next_url)

        try:
            user = query_one("SELECT * FROM users WHERE username = %s", (username,))
            if user and user['status'] == 'active' and verify_password(user['password_hash'], password):
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']
                session['full_name'] = user['full_name']

                # Attach role-specific entity IDs to session
                if user['role'] == 'patient':
                    patient = query_one("SELECT id FROM patients WHERE user_id = %s", (user['id'],))
                    session['patient_id'] = patient['id'] if patient else None
                    flash(f"Welcome back, {user['full_name']}!", 'success')
                elif user['role'] == 'doctor':
                    doctor = query_one("SELECT id FROM doctors WHERE user_id = %s", (user['id'],))
                    session['doctor_id'] = doctor['id'] if doctor else None
                    flash(f"Welcome, Dr. {user['full_name']}!", 'success')
                elif user['role'] == 'receptionist':
                    staff = query_one("SELECT id FROM staff WHERE user_id = %s", (user['id'],))
                    session['staff_id'] = staff['id'] if staff else None
                    flash(f"Welcome, {user['full_name']}!", 'success')
                elif user['role'] == 'admin':
                    staff = query_one("SELECT id FROM staff WHERE user_id = %s", (user['id'],))
                    session['staff_id'] = staff['id'] if staff else None
                    flash("Logged in as Administrator.", 'success')

                # Check if safe next redirection parameter is provided
                if next_url and next_url.startswith('/') and not next_url.startswith('//'):
                    return redirect(next_url)

                # Default role-based dashboard redirects
                if user['role'] == 'patient':
                    return redirect(url_for('patient.dashboard'))
                elif user['role'] == 'doctor':
                    return redirect(url_for('doctor.dashboard'))
                elif user['role'] == 'receptionist':
                    return redirect(url_for('receptionist.dashboard'))
                elif user['role'] == 'admin':
                    return redirect(url_for('admin.dashboard'))
            else:
                flash('Invalid credentials or account deactivated.', 'danger')
        except Exception as e:
            flash('A database error occurred during login. Please ensure MySQL is running.', 'danger')
            print(f"[Auth Error] {e}")

    return render_template('auth/login.html', next=next_param)


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    generic_message = (
        'If the account exists and email delivery is available, password reset '
        'instructions will be sent. If you do not receive an email, contact the '
        'hospital administrator.'
    )
    if request.method == 'GET':
        return render_template('auth/forgot_password.html')

    email = normalize_email(request.form.get('email', ''))
    email_hash = _reset_digest(email[:320])
    ip_hash = _reset_digest(request.remote_addr or 'unknown')
    token = None
    reset_user = None
    configured = _password_reset_mail_configured()
    try:
        with get_db(lock_name=f'carepoint_reset_{email_hash[:30]}') as (cursor, _):
            cursor.execute("""
                SELECT COUNT(*) AS request_count
                FROM password_reset_tokens
                WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
                  AND (email_hash = %s OR request_ip_hash = %s)
            """, (PASSWORD_RESET_RATE_WINDOW_MINUTES, email_hash, ip_hash))
            count_result = cursor.fetchone()
            rate_limited = (
                not count_result
                or count_result['request_count'] >= PASSWORD_RESET_RATE_LIMIT
            )

            if not rate_limited and configured and validate_email(email):
                cursor.execute(
                    'SELECT id, email FROM users WHERE email = %s',
                    (email,)
                )
                reset_user = cursor.fetchone()
                if reset_user:
                    token = secrets.token_urlsafe(32)
                    cursor.execute("""
                        INSERT INTO password_reset_tokens
                            (user_id, email_hash, request_ip_hash, token_hash, expires_at)
                        VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL 30 MINUTE))
                    """, (
                        reset_user['id'],
                        email_hash,
                        ip_hash,
                        hashlib.sha256(token.encode('utf-8')).hexdigest(),
                    ))
                else:
                    cursor.execute("""
                        INSERT INTO password_reset_tokens
                            (user_id, email_hash, request_ip_hash)
                        VALUES (NULL, %s, %s)
                    """, (email_hash, ip_hash))
            else:
                cursor.execute("""
                    INSERT INTO password_reset_tokens
                        (user_id, email_hash, request_ip_hash)
                    VALUES (NULL, %s, %s)
                """, (email_hash, ip_hash))
    except Exception:
        current_app.logger.exception('Could not process a password reset request.')
        flash('Password reset is temporarily unavailable. Please try again later.', 'warning')
        return redirect(url_for('auth.forgot_password'))

    if not configured:
        current_app.logger.error(
            'Password reset is unavailable because SMTP settings are incomplete.'
        )
        flash('Password reset email is temporarily unavailable. Contact the hospital administrator.', 'warning')
    else:
        if token and reset_user:
            reset_url = (
                current_app.config['PUBLIC_BASE_URL'].rstrip('/')
                + url_for('auth.reset_password', token=token)
            )
            try:
                send_password_reset_email(reset_user['email'], reset_url)
            except Exception:
                current_app.logger.exception('Password reset email delivery failed.')
                token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
                try:
                    with get_db() as (cursor, _):
                        cursor.execute("""
                            UPDATE password_reset_tokens
                            SET used_at = NOW()
                            WHERE token_hash = %s AND used_at IS NULL
                        """, (token_hash,))
                except Exception:
                    current_app.logger.exception(
                        'Could not invalidate an undelivered password reset token.'
                    )
        flash(generic_message, 'info')

    return redirect(url_for('auth.forgot_password'))


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if len(token) > 256:
        return _render_reset_page(False)
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()

    if request.method == 'GET':
        try:
            reset_row = query_one("""
                SELECT id
                FROM password_reset_tokens
                WHERE token_hash = %s AND used_at IS NULL
                  AND expires_at > NOW() AND attempt_count < %s
            """, (token_hash, PASSWORD_RESET_ATTEMPT_LIMIT))
        except Exception:
            current_app.logger.exception('Could not validate a password reset link.')
            flash('Password reset is temporarily unavailable. Please try again later.', 'warning')
            return redirect(url_for('auth.login'))
        return _render_reset_page(bool(reset_row), token=token)

    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')
    password_error = None
    if not validate_password(password):
        password_error = (
            'Password must be at least 8 characters and include uppercase, lowercase, '
            'a number, and a special character.'
        )
    elif password != confirm_password:
        password_error = 'The passwords do not match.'

    try:
        with get_db() as (cursor, _):
            cursor.execute("""
                SELECT id, user_id, attempt_count
                FROM password_reset_tokens
                WHERE token_hash = %s AND used_at IS NULL
                  AND expires_at > NOW() AND attempt_count < %s
                FOR UPDATE
            """, (token_hash, PASSWORD_RESET_ATTEMPT_LIMIT))
            reset_row = cursor.fetchone()
            if not reset_row:
                return _render_reset_page(False)

            cursor.execute("""
                UPDATE password_reset_tokens
                SET attempt_count = attempt_count + 1
                WHERE id = %s AND used_at IS NULL
                  AND expires_at > NOW() AND attempt_count < %s
            """, (reset_row['id'], PASSWORD_RESET_ATTEMPT_LIMIT))
            if cursor.rowcount != 1:
                return _render_reset_page(False)

            if password_error:
                return _render_reset_page(True, error=password_error, token=token)

            cursor.execute(
                'UPDATE users SET password_hash = %s WHERE id = %s',
                (hash_password(password), reset_row['user_id'])
            )
            if cursor.rowcount != 1:
                raise RuntimeError('Account password could not be updated.')
            cursor.execute("""
                UPDATE password_reset_tokens
                SET used_at = NOW()
                WHERE user_id = %s AND used_at IS NULL
            """, (reset_row['user_id'],))
    except Exception:
        current_app.logger.exception('Could not complete a password reset.')
        flash('Password reset could not be completed. Please request a new link.', 'danger')
        return redirect(url_for('auth.login'))

    flash('Your password has been reset. Please sign in with the new password.', 'success')
    return redirect(url_for('auth.login'))

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        full_name = ' '.join(request.form.get('full_name', '').split())
        email = normalize_email(request.form.get('email', ''))
        phone = request.form.get('phone', '').strip()
        dob = request.form.get('dob', '').strip()
        gender = request.form.get('gender', '').strip()
        blood_group = request.form.get('blood_group', '').strip()
        address = request.form.get('address', '').strip()
        emergency_contact = request.form.get('emergency_contact', '').strip()

        if not email or not validate_email(email):
            flash('Enter a valid email address using gmail.com, yahoo.com, yahoo.co.in, outlook.com, or nmims.in.', 'danger')
            return _render_patient_registration()

        # Input presence validation
        if not all([username, password, full_name, email, phone, dob, gender, blood_group, address, emergency_contact]):
            flash('All fields are required for patient registration.', 'danger')
            return _render_patient_registration()

        if not validate_person_name(full_name):
            flash('Full name must contain letters and may include spaces, hyphens, or apostrophes.', 'danger')
            return _render_patient_registration()
        if not validate_indian_mobile(phone):
            flash('Phone number must be a 10-digit Indian mobile number starting with 6, 7, 8, or 9.', 'danger')
            return _render_patient_registration()
        if not validate_indian_mobile(emergency_contact):
            flash('Emergency contact must be a 10-digit Indian mobile number starting with 6, 7, 8, or 9.', 'danger')
            return _render_patient_registration()
        if not validate_password(password):
            flash('Password must be at least 8 characters and include uppercase, lowercase, a number, and a special character.', 'danger')
            return _render_patient_registration()
        if gender not in ('Male', 'Female', 'Other'):
            flash('Select a valid gender.', 'danger')
            return _render_patient_registration()
        if blood_group not in ('A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'):
            flash('Select a valid blood group.', 'danger')
            return _render_patient_registration()

        parsed_dob = _parse_date_of_birth(dob)
        if not parsed_dob:
            flash('Enter a valid date of birth in YYYY-MM-DD format.', 'danger')
            return _render_patient_registration()
        if parsed_dob < date(1000, 1, 1):
            flash('Enter a date of birth within the supported date range.', 'danger')
            return _render_patient_registration()
        if parsed_dob > date.today():
            flash('Date of birth cannot be in the future.', 'danger')
            return _render_patient_registration()

        try:
            hashed_pw = hash_password(password)
            with get_db() as (cursor, _):
                cursor.execute("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
                if cursor.fetchone():
                    flash('Username or Email already registered. Please login or choose another.', 'warning')
                    return _render_patient_registration()

                cursor.execute(
                    "INSERT INTO users (username, password_hash, role, full_name, email, phone) VALUES (%s, %s, %s, %s, %s, %s)",
                    (username, hashed_pw, 'patient', full_name, email, phone)
                )
                user_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO patients (user_id, dob, gender, blood_group, address, emergency_contact) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id, dob, gender, blood_group, address, emergency_contact)
                )

            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('auth.login'))
        except Exception as e:
            flash('A database error occurred during registration. Please try again.', 'danger')
            print(f"[Registration Error] {e}")

    return _render_patient_registration()

@auth_bp.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user_id = session.get('user_id')
    try:
        user = query_one("SELECT * FROM users WHERE id = %s", (user_id,))
        if not user:
            session.clear()
            flash('Your account could not be found. Please sign in again.', 'warning')
            return redirect(url_for('auth.login'))
        patient = None
        doctor = None
        if user['role'] == 'patient':
            patient = query_one("SELECT * FROM patients WHERE user_id = %s", (user_id,))
        elif user['role'] == 'doctor':
            doctor = query_one("SELECT * FROM doctors WHERE user_id = %s", (user_id,))
    except Exception:
        current_app.logger.exception('Unable to load the signed-in user profile.')
        flash('Profile information could not be loaded. Please try again.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        full_name = ' '.join(request.form.get('full_name', '').split())
        email = normalize_email(request.form.get('email', ''))
        phone = request.form.get('phone', '').strip()
        address = request.form.get('address', '').strip()
        emergency_contact = request.form.get('emergency_contact', '').strip()

        if not email or not validate_email(email):
            flash('Enter a valid email address using gmail.com, yahoo.com, yahoo.co.in, outlook.com, or nmims.in.', 'danger')
            return render_template('auth/profile.html', user=user, patient=patient, doctor=doctor)

        if not all([full_name, email, phone]) or (user['role'] == 'patient' and patient and not all([address, emergency_contact])):
            flash('Name, email, phone, and patient contact fields cannot be empty.', 'danger')
            return render_template('auth/profile.html', user=user, patient=patient, doctor=doctor)

        if not validate_person_name(full_name):
            flash('Full name must contain letters and may include spaces, hyphens, or apostrophes.', 'danger')
            return render_template('auth/profile.html', user=user, patient=patient, doctor=doctor)
        if not validate_indian_mobile(phone):
            flash('Phone number must be a 10-digit Indian mobile number starting with 6, 7, 8, or 9.', 'danger')
            return render_template('auth/profile.html', user=user, patient=patient, doctor=doctor)
        if user['role'] == 'patient' and patient and not validate_indian_mobile(emergency_contact):
            flash('Emergency contact must be a 10-digit Indian mobile number starting with 6, 7, 8, or 9.', 'danger')
            return render_template('auth/profile.html', user=user, patient=patient, doctor=doctor)

        try:
            # Check for duplicate email across other users
            duplicate_email = query_one("SELECT id FROM users WHERE email = %s AND id != %s", (email, user_id))
            if duplicate_email:
                flash('This email address is already in use by another user.', 'warning')
                return render_template('auth/profile.html', user=user, patient=patient, doctor=doctor)

            with get_db() as (cursor, _):
                cursor.execute(
                    "UPDATE users SET full_name = %s, email = %s, phone = %s WHERE id = %s",
                    (full_name, email, phone, user_id)
                )

                if user['role'] == 'patient' and patient:
                    cursor.execute(
                        "UPDATE patients SET address = %s, emergency_contact = %s WHERE id = %s AND user_id = %s",
                        (address, emergency_contact, patient['id'], user_id)
                    )

            session['full_name'] = full_name
            flash('Profile updated successfully!', 'success')
            return redirect(url_for('auth.profile'))
        except Exception as e:
            flash('A database error occurred during profile update.', 'danger')
            print(f"[Profile Error] {e}")

    return render_template('auth/profile.html', user=user, patient=patient, doctor=doctor)
