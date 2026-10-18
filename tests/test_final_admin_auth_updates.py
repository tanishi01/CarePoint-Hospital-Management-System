import hashlib
import unittest
from decimal import Decimal
from unittest.mock import patch

from app import create_app
from routes import admin, auth, pharmacy
from routes.auth import _password_reset_mail_configured
from utils.auth_helpers import verify_password
from utils.input_validation import validate_room_number


class DoctorDeleteCursor:
    def __init__(self, dependent_counts=None):
        self.doctor = {'id': 9, 'user_id': 19}
        self.dependent_counts = dependent_counts or {
            'appointments': 0,
            'medical_records': 0,
            'schedules': 0,
            'leave_requests': 0,
            'notifications': 0,
            'reviewed_leaves': 0,
            'reset_tokens': 0,
            'staff_records': 0,
            'patient_records': 0,
        }
        self.rowcount = 0
        self.deleted = []

    def execute(self, sql, params=()):
        sql = ' '.join(sql.split())
        if sql.startswith('SELECT id, user_id FROM doctors'):
            self.result = self.doctor
        elif sql.startswith('SELECT id, role FROM users'):
            self.result = {'id': params[0], 'role': 'doctor'}
        elif sql.startswith('SELECT (SELECT COUNT(*) FROM appointments'):
            self.result = self.dependent_counts
        elif sql.startswith('DELETE FROM doctors'):
            self.deleted.append(('doctor', params[0]))
            self.rowcount = 1
        elif sql.startswith("DELETE FROM users"):
            self.deleted.append(('user', params[0]))
            self.rowcount = 1

    def fetchone(self):
        return getattr(self, 'result', None)


class SimpleDatabase:
    def __init__(self, cursor):
        self.cursor = cursor
        self.committed = False

    def __enter__(self):
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False


class MedicineCursor:
    def __init__(self, medicine):
        self.medicine = medicine
        self.rowcount = 1
        self.statement = ''

    def execute(self, sql, params=()):
        self.statement = sql
        if sql.lstrip().startswith('UPDATE medicines'):
            name, generic_name, unit_price, threshold, medicine_id = params
            self.medicine.update({
                'name': name,
                'generic_name': generic_name,
                'unit_price': unit_price,
                'min_stock_threshold': threshold,
            })


class DoctorUpdateCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return {'id': 9}

    def fetchall(self):
        return [{
            'day_of_week': 'Monday',
            'start_time': '09:00:00',
            'end_time': '13:00:00',
            'break_start': None,
            'break_end': None,
        }]


class PasswordResetState:
    def __init__(self, email='user@gmail.com', role='patient', lookup_email=None):
        self.user = {
            'id': 11,
            'email': email,
            'role': role,
            'password_hash': 'old-hash',
        }
        self.lookup_email = lookup_email if lookup_email is not None else email
        self.requests = []
        self.tokens = {}
        self.user_lookups = 0
        self.user_updates = 0
        self.last_token_hash = None
        self.last_token_id = 0


class PasswordResetCursor:
    def __init__(self, state):
        self.state = state
        self.result = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        normalized = ' '.join(sql.split())
        self.result = None
        self.rowcount = 0
        if normalized.startswith('SELECT COUNT(*) AS request_count'):
            self.result = {'request_count': len(self.state.requests)}
        elif normalized.startswith('SELECT id, email FROM users'):
            self.state.user_lookups += 1
            self.result = (
                {'id': self.state.user['id'], 'email': self.state.user['email']}
                if params[0] == self.state.lookup_email else None
            )
        elif normalized.startswith('INSERT INTO password_reset_tokens'):
            self.state.requests.append(params)
            if '(user_id, email_hash, request_ip_hash, token_hash, expires_at)' in normalized:
                user_id, email_hash, ip_hash, token_hash = params
                self.state.last_token_id += 1
                token = {
                    'id': self.state.last_token_id,
                    'user_id': user_id,
                    'token_hash': token_hash,
                    'attempt_count': 0,
                    'expired': False,
                    'used_at': None,
                }
                self.state.tokens[token_hash] = token
                self.state.last_token_hash = token_hash
        elif normalized.startswith('SELECT id, user_id, attempt_count'):
            token = self.state.tokens.get(params[0])
            if token and not token['expired'] and not token['used_at'] \
                    and token['attempt_count'] < params[1]:
                self.result = {
                    'id': token['id'],
                    'user_id': token['user_id'],
                    'attempt_count': token['attempt_count'],
                }
        elif normalized.startswith('UPDATE password_reset_tokens SET attempt_count'):
            token = next(
                (row for row in self.state.tokens.values() if row['id'] == params[0]),
                None
            )
            if token and not token['expired'] and not token['used_at'] \
                    and token['attempt_count'] < params[1]:
                token['attempt_count'] += 1
                self.rowcount = 1
        elif normalized.startswith('UPDATE users SET password_hash'):
            self.state.user['password_hash'] = params[0]
            self.state.user_updates += 1
            self.rowcount = 1
        elif normalized.startswith('UPDATE password_reset_tokens SET used_at'):
            token_hash = params[0]
            token = self.state.tokens.get(token_hash)
            if token and not token['used_at']:
                token['used_at'] = True
                self.rowcount = 1
            elif len(params) == 1:
                for row in self.state.tokens.values():
                    if row['user_id'] == token_hash and not row['used_at']:
                        row['used_at'] = True
        elif normalized.startswith('UPDATE password_reset_tokens SET used_at = NOW() WHERE user_id'):
            for token in self.state.tokens.values():
                if token['user_id'] == params[0]:
                    token['used_at'] = True

    def fetchone(self):
        return self.result


class FinalAdminAuthUpdateTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='reset-test-secret',
            SMTP_HOST='smtp.gmail.com',
            SMTP_PORT=587,
            SMTP_USERNAME='sender@gmail.com',
            SMTP_PASSWORD='not-a-real-password',
            SMTP_FROM_EMAIL='sender@gmail.com',
            PUBLIC_BASE_URL='https://carepoint.example',
        )

    def client_for(self, role='admin'):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['role'] = role
        return client

    def test_password_reset_mail_allows_local_http_only_in_development(self):
        with self.app.app_context():
            for hostname in ('127.0.0.1:5000', 'localhost:5000'):
                with self.subTest(hostname=hostname):
                    self.app.config.update(
                        APP_ENV='development',
                        PUBLIC_BASE_URL=f'http://{hostname}',
                    )
                    self.assertTrue(_password_reset_mail_configured())

    def test_password_reset_mail_rejects_public_http_and_credentials(self):
        with self.app.app_context():
            self.app.config.update(APP_ENV='development')
            for base_url in (
                'http://carepoint.example',
                'http://user:password@localhost:5000',
                'http://127.0.0.1:secret@localhost:5000',
            ):
                with self.subTest(base_url=base_url):
                    self.app.config['PUBLIC_BASE_URL'] = base_url
                    self.assertFalse(_password_reset_mail_configured())

    def test_password_reset_mail_accepts_https_in_production(self):
        with self.app.app_context():
            self.app.config.update(
                APP_ENV='production',
                PUBLIC_BASE_URL='https://carepoint.example',
            )
            self.assertTrue(_password_reset_mail_configured())

    def test_password_reset_mail_rejects_local_http_in_production(self):
        with self.app.app_context():
            self.app.config.update(
                APP_ENV='production',
                PUBLIC_BASE_URL='http://localhost:5000',
            )
            self.assertFalse(_password_reset_mail_configured())

    def test_doctor_deletion_requires_confirmation_and_preserves_dependent_records(self):
        cursor = DoctorDeleteCursor()
        with patch.object(admin, 'get_db', return_value=SimpleDatabase(cursor)):
            response = self.client_for().post('/admin/doctors/9/delete')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cursor.deleted, [])

        for dependency in (
            'appointments', 'medical_records', 'schedules',
            'leave_requests', 'notifications', 'reviewed_leaves',
            'reset_tokens', 'staff_records', 'patient_records',
        ):
            with self.subTest(dependency=dependency):
                dependencies = {
                    key: int(key == dependency)
                    for key in (
                        'appointments', 'medical_records', 'schedules',
                        'leave_requests', 'notifications', 'reviewed_leaves',
                        'reset_tokens', 'staff_records', 'patient_records',
                    )
                }
                cursor = DoctorDeleteCursor(dependencies)
                client = self.client_for()
                with patch.object(admin, 'get_db', return_value=SimpleDatabase(cursor)):
                    response = client.post(
                        '/admin/doctors/9/delete', data={'confirmed': '1'}
                    )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(cursor.deleted, [])
                with client.session_transaction() as session:
                    flashed = session.get('_flashes', [])
                self.assertIn('cannot be deleted', str(flashed))

    def test_doctor_deletion_allowed_only_when_all_dependencies_are_absent(self):
        cursor = DoctorDeleteCursor()
        database = SimpleDatabase(cursor)
        with patch.object(admin, 'get_db', return_value=database):
            response = self.client_for().post(
                '/admin/doctors/9/delete', data={'confirmed': '1'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cursor.deleted, [('doctor', 9), ('user', 19)])
        self.assertTrue(database.committed)

    def test_doctor_list_shows_confirmed_delete_action_and_status_toggle(self):
        doctor = {
            'id': 9, 'user_id': 19, 'username': 'doc', 'full_name': 'Doctor',
            'email': 'doc@gmail.com', 'phone': '9876543210', 'department': 'General',
            'specialization': 'General', 'qualification': 'MBBS', 'room_number': '1',
            'consulting_fee': 500, 'status': 'active', 'user_status': 'active',
            'can_delete': True,
        }
        with patch.object(admin, 'query_all', return_value=[doctor]):
            page = self.client_for().get('/admin/doctors').get_data(as_text=True)
        self.assertIn('Deactivate doctor', page)
        self.assertIn('Delete doctor', page)
        self.assertIn("Permanently delete this doctor account?", page)
        self.assertEqual(page.count('pattern="(?:[A-Za-z]+[ -]?)?[0-9]+"'), 2)

        doctor['can_delete'] = False
        with patch.object(admin, 'query_all', return_value=[doctor]):
            page = self.client_for().get('/admin/doctors').get_data(as_text=True)
        self.assertNotIn('Delete doctor', page)

    def _doctor_update_form(self, room_number):
        return {
            'full_name': 'Test Doctor',
            'email': 'doctor@gmail.com',
            'phone': '9876543210',
            'department': 'General',
            'specialization': 'General',
            'qualification': 'MBBS',
            'room_number': room_number,
            'consulting_fee': '500',
        }

    def test_room_number_validation_matches_established_project_formats(self):
        for room_number in ('208', 'Room 101', 'Room 4', 'R-1'):
            with self.subTest(room_number=room_number):
                self.assertTrue(validate_room_number(room_number))
        for room_number in ('eded', 'R-', 'Room', '1A', 'Room 101 A'):
            with self.subTest(room_number=room_number):
                self.assertFalse(validate_room_number(room_number))

    def test_invalid_room_number_is_rejected_before_doctor_update(self):
        client = self.client_for()
        with patch.object(admin, 'query_one') as lookup, \
                patch.object(admin, 'get_db') as database:
            response = client.post(
                '/admin/doctors/9/update',
                data=self._doctor_update_form('eded')
            )

        self.assertEqual(response.status_code, 302)
        lookup.assert_not_called()
        database.assert_not_called()
        with client.session_transaction() as session:
            self.assertIn('valid room number', str(session.get('_flashes', [])))

    def test_valid_room_number_is_persisted_by_doctor_edit(self):
        cursor = DoctorUpdateCursor()
        client = self.client_for()
        with patch.object(admin, 'query_one', side_effect=[
            {'user_id': 19}, None
        ]), patch.object(admin, 'get_db', return_value=SimpleDatabase(cursor)), \
                patch.object(admin, '_room_schedule_conflicts', return_value=False):
            response = client.post(
                '/admin/doctors/9/update',
                data=self._doctor_update_form('Room 101')
            )

        self.assertEqual(response.status_code, 302)
        doctor_update = next(
            (params for sql, params in cursor.calls if 'UPDATE doctors SET department' in sql),
            None
        )
        self.assertIsNotNone(doctor_update)
        self.assertEqual(doctor_update[3], 'Room 101')

    def test_valid_room_number_still_rejects_conflicting_room_assignment(self):
        cursor = DoctorUpdateCursor()
        client = self.client_for()
        with patch.object(admin, 'query_one', side_effect=[
            {'user_id': 19}, None
        ]), patch.object(admin, 'get_db', return_value=SimpleDatabase(cursor)), \
                patch.object(admin, '_room_schedule_conflicts', return_value=[{
                    'doctor_name': 'Existing Doctor'
                }]):
            response = client.post(
                '/admin/doctors/9/update',
                data=self._doctor_update_form('R-1')
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(any(
            'UPDATE doctors SET department' in sql for sql, _ in cursor.calls
        ))
        with client.session_transaction() as session:
            self.assertIn('overlapping working hours', str(session.get('_flashes', [])))

    def test_medicine_edit_preserves_stock_and_updates_threshold_for_low_stock_query(self):
        medicine = {
            'id': 7, 'name': 'Medicine', 'generic_name': 'Generic',
            'unit_price': Decimal('12.50'), 'stock_quantity': 8,
            'min_stock_threshold': 10,
        }
        cursor = MedicineCursor(medicine)
        database = SimpleDatabase(cursor)
        client = self.client_for()
        with patch.object(pharmacy, 'query_one', side_effect=lambda *args: medicine), \
                patch.object(pharmacy, 'get_db', return_value=database):
            response = client.post('/pharmacy/medicine/7/edit', data={
                'name': 'Medicine updated',
                'generic_name': 'Generic updated',
                'unit_price': '15.25',
                'min_stock_threshold': '5',
            })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(database.committed)
        self.assertEqual(medicine['stock_quantity'], 8)
        self.assertEqual(medicine['min_stock_threshold'], 5)
        self.assertNotIn('stock_quantity', cursor.statement)

        with patch.object(pharmacy, 'query_all', return_value=[medicine]):
            inventory_page = client.get('/pharmacy/inventory').get_data(as_text=True)
        self.assertIn('href="/pharmacy/medicine/7/edit"', inventory_page)

        queried = []

        def read_low_stock(sql):
            queried.append(sql)
            return [medicine] if medicine['stock_quantity'] <= medicine['min_stock_threshold'] else []

        with patch.object(pharmacy, 'query_all', side_effect=read_low_stock):
            alerts = client.get('/pharmacy/low-stock')
        self.assertEqual(alerts.status_code, 200)
        self.assertIn('stock_quantity <= min_stock_threshold', queried[0])
        self.assertNotIn('Medicine updated', alerts.get_data(as_text=True))
        medicine['min_stock_threshold'] = 8
        with patch.object(pharmacy, 'query_all', side_effect=read_low_stock):
            alerts = client.get('/pharmacy/low-stock')
        self.assertIn('Medicine updated', alerts.get_data(as_text=True))

    def test_medicine_edit_rejects_negative_threshold_and_non_admin_access(self):
        medicine = {
            'id': 7, 'name': 'Medicine', 'generic_name': 'Generic',
            'unit_price': Decimal('12.50'), 'stock_quantity': 8,
            'min_stock_threshold': 10,
        }
        cursor = MedicineCursor(medicine)
        with patch.object(pharmacy, 'query_one', return_value=medicine), \
                patch.object(pharmacy, 'get_db', return_value=SimpleDatabase(cursor)):
            response = self.client_for().post('/pharmacy/medicine/7/edit', data={
                'name': 'Medicine',
                'generic_name': 'Generic',
                'unit_price': '-1',
                'min_stock_threshold': '-2',
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(medicine['min_stock_threshold'], 10)
        unauthorized = self.client_for('doctor').get('/pharmacy/medicine/7/edit')
        self.assertEqual(unauthorized.status_code, 302)

    def test_login_links_to_forgot_password_and_request_is_generic(self):
        client = self.app.test_client()
        login_page = client.get('/auth/login').get_data(as_text=True)
        self.assertIn('Forgot password?', login_page)
        state = PasswordResetState()
        with patch.object(auth, 'get_db', side_effect=lambda *args, **kwargs: SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(auth, 'send_password_reset_email') as send_mail:
            response = client.post('/auth/forgot-password', data={'email': 'unknown@gmail.com'})
        self.assertEqual(response.status_code, 302)
        send_mail.assert_not_called()
        self.assertEqual(state.user_lookups, 1)
        with client.session_transaction() as session:
            self.assertIn('If the account exists', str(session.get('_flashes', [])))

    def _request_reset(self, state, client=None, submitted_email=None):
        client = client or self.app.test_client()
        with patch.object(auth.secrets, 'token_urlsafe', return_value='one-time-token'), \
                patch.object(auth, 'get_db', side_effect=lambda *args, **kwargs: SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(auth, 'send_password_reset_email') as sender:
            response = client.post(
                '/auth/forgot-password',
                data={'email': submitted_email or state.user['email']}
            )
        return response, sender

    def test_password_reset_uses_hashed_single_use_token_and_updates_password(self):
        state = PasswordResetState()
        response, sender = self._request_reset(state)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            state.last_token_hash,
            hashlib.sha256(b'one-time-token').hexdigest()
        )
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], state.user['email'])
        self.assertIn('/auth/reset-password/one-time-token', sender.call_args.args[1])
        self.assertEqual(state.tokens[state.last_token_hash]['user_id'], state.user['id'])

        cursor = PasswordResetCursor(state)
        client = self.app.test_client()
        with patch.object(auth, 'query_one', return_value={'id': 1}):
            reset_page = client.get('/auth/reset-password/one-time-token')
        self.assertIn('Confirm new password', reset_page.get_data(as_text=True))
        with patch.object(auth, 'get_db', return_value=SimpleDatabase(cursor)):
            reset_response = client.post(
                '/auth/reset-password/one-time-token',
                data={'password': 'NewPass1!', 'confirm_password': 'NewPass1!'}
            )
        self.assertEqual(reset_response.status_code, 302)
        self.assertTrue(verify_password(state.user['password_hash'], 'NewPass1!'))
        self.assertTrue(state.tokens[state.last_token_hash]['used_at'])

        with patch.object(auth, 'get_db', return_value=SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(auth, 'query_one', return_value=None):
            reused = client.post(
                '/auth/reset-password/one-time-token',
                data={'password': 'NewPass2!', 'confirm_password': 'NewPass2!'}
            )
        self.assertEqual(reused.status_code, 200)
        self.assertEqual(state.user_updates, 1)
        self.assertIn('already used', reused.get_data(as_text=True))

    def test_invalid_and_expired_reset_tokens_are_rejected(self):
        state = PasswordResetState()
        expired_token_hash = hashlib.sha256(b'expired').hexdigest()
        state.tokens[expired_token_hash] = {
            'id': 1, 'user_id': 11, 'token_hash': 'expiredhash',
            'attempt_count': 0, 'expired': True, 'used_at': None,
        }
        client = self.app.test_client()
        with patch.object(auth, 'get_db', return_value=SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(
                    auth,
                    'query_one',
                    side_effect=lambda sql, params: (
                        {'id': 1} if params[0] in state.tokens
                        and not state.tokens[params[0]]['expired'] else None
                    )
                ):
            invalid = client.post(
                '/auth/reset-password/not-valid',
                data={'password': 'NewPass1!', 'confirm_password': 'NewPass1!'}
            )
            expired = client.post(
                '/auth/reset-password/expired',
                data={'password': 'NewPass1!', 'confirm_password': 'NewPass1!'}
            )
        self.assertIn('invalid, expired, or already used', invalid.get_data(as_text=True))
        self.assertIn('invalid, expired, or already used', expired.get_data(as_text=True))
        self.assertEqual(state.user_updates, 0)

    def test_reset_password_policy_and_five_attempt_limit(self):
        state = PasswordResetState()
        self._request_reset(state)
        cursor = PasswordResetCursor(state)
        client = self.app.test_client()
        with patch.object(auth, 'get_db', return_value=SimpleDatabase(cursor)), \
                patch.object(auth, 'query_one', return_value=None):
            weak = client.post(
                '/auth/reset-password/one-time-token',
                data={'password': 'weak', 'confirm_password': 'weak'}
            )
        self.assertIn('at least 8 characters', weak.get_data(as_text=True))
        self.assertEqual(state.user_updates, 0)
        for _ in range(auth.PASSWORD_RESET_ATTEMPT_LIMIT - 1):
            with patch.object(auth, 'get_db', return_value=SimpleDatabase(PasswordResetCursor(state))), \
                    patch.object(auth, 'query_one', return_value=None):
                client.post(
                    '/auth/reset-password/one-time-token',
                    data={'password': 'weak', 'confirm_password': 'weak'}
                )
        with patch.object(auth, 'get_db', return_value=SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(auth, 'query_one', return_value=None):
            locked = client.get('/auth/reset-password/one-time-token')
        self.assertIn('invalid, expired, or already used', locked.get_data(as_text=True))
        self.assertEqual(state.tokens[state.last_token_hash]['attempt_count'], 5)

    def test_request_rate_limit_and_unknown_email_do_not_reveal_account_existence(self):
        state = PasswordResetState()
        client = self.app.test_client()
        responses = []
        with patch.object(auth.secrets, 'token_urlsafe', return_value='token'), \
                patch.object(auth, 'get_db', side_effect=lambda *args, **kwargs: SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(auth, 'send_password_reset_email'):
            for _ in range(auth.PASSWORD_RESET_RATE_LIMIT + 1):
                responses.append(client.post(
                    '/auth/forgot-password', data={'email': state.user['email']}
                ))
        self.assertEqual(state.user_lookups, auth.PASSWORD_RESET_RATE_LIMIT)
        with client.session_transaction() as session:
            message = str(session.get('_flashes', []))
        self.assertIn('If the account exists', message)
        self.assertEqual(len(responses), auth.PASSWORD_RESET_RATE_LIMIT + 1)

        unknown = PasswordResetState(
            email='registered@gmail.com',
            lookup_email='another-address@gmail.com',
        )
        unknown_client = self.app.test_client()
        with patch.object(auth, 'get_db', side_effect=lambda *args, **kwargs: SimpleDatabase(PasswordResetCursor(unknown))), \
                patch.object(auth, 'send_password_reset_email') as unknown_sender:
            unknown_response = unknown_client.post(
                '/auth/forgot-password', data={'email': 'nobody@gmail.com'}
            )
        self.assertEqual(unknown_response.status_code, 302)
        unknown_sender.assert_not_called()
        with unknown_client.session_transaction() as session:
            self.assertIn('If the account exists', str(session.get('_flashes', [])))

    def test_alternate_submission_cannot_receive_another_accounts_reset_link(self):
        stored_account = 'owner@gmail.com'
        alternate_submission = 'alternate@gmail.com'
        state = PasswordResetState(
            email=stored_account,
            lookup_email=alternate_submission,
        )

        response, sender = self._request_reset(
            state,
            submitted_email=alternate_submission,
        )

        self.assertEqual(response.status_code, 302)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], stored_account)
        self.assertNotEqual(sender.call_args.args[0], alternate_submission)
        self.assertEqual(state.tokens[state.last_token_hash]['user_id'], state.user['id'])

    def test_reset_is_supported_for_all_existing_roles(self):
        for role in ('patient', 'doctor', 'receptionist', 'admin'):
            with self.subTest(role=role):
                state = PasswordResetState(role=role)
                self._request_reset(state)
                with patch.object(
                    auth, 'get_db',
                    return_value=SimpleDatabase(PasswordResetCursor(state))
                ):
                    response = self.app.test_client().post(
                        '/auth/reset-password/one-time-token',
                        data={'password': 'NewPass1!', 'confirm_password': 'NewPass1!'}
                    )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(state.user_updates, 1)

    def test_smtp_unavailable_is_reported_safely_without_claiming_delivery(self):
        self.app.config['SMTP_PASSWORD'] = ''
        client = self.app.test_client()
        state = PasswordResetState()
        with patch.object(auth, 'get_db', side_effect=lambda *args, **kwargs: SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(auth, 'send_password_reset_email') as sender:
            response = client.post(
                '/auth/forgot-password', data={'email': state.user['email']}
            )
        self.assertEqual(response.status_code, 302)
        sender.assert_not_called()
        with client.session_transaction() as session:
            message = str(session.get('_flashes', []))
        self.assertIn('temporarily unavailable', message)
        self.assertNotIn('email sent', message.lower())

    def test_smtp_delivery_failure_is_logged_and_token_is_invalidated(self):
        state = PasswordResetState()
        client = self.app.test_client()
        with patch.object(auth.secrets, 'token_urlsafe', return_value='one-time-token'), \
                patch.object(auth, 'get_db', side_effect=lambda *args, **kwargs: SimpleDatabase(PasswordResetCursor(state))), \
                patch.object(
                    auth, 'send_password_reset_email',
                    side_effect=RuntimeError('simulated SMTP delivery failure')
                ), self.assertLogs(self.app.logger.name, level='ERROR'):
            response = client.post(
                '/auth/forgot-password', data={'email': state.user['email']}
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(state.tokens[state.last_token_hash]['used_at'])
        with client.session_transaction() as session:
            message = str(session.get('_flashes', []))
        self.assertIn('If the account exists', message)
        self.assertNotIn('simulated SMTP delivery failure', message)


if __name__ == '__main__':
    unittest.main()
