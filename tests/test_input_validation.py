import unittest
from unittest.mock import patch

from app import create_app
from routes import admin, auth, doctor, patient, receptionist
from utils.auth_helpers import verify_password
from utils.input_validation import (
    normalize_email,
    validate_email,
    validate_indian_mobile,
    validate_password,
    validate_person_name,
)


class FakeCursor:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.lastrowid = 100
        self.calls = []
        self._one = None

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if 'GET_LOCK' in sql:
            self._one = {'acquired': 1}
        elif 'RELEASE_LOCK' in sql:
            self._one = {'released': 1}
        else:
            self._one = {'id': 1} if 'SELECT id FROM users WHERE username' in sql and self.duplicate else None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return []


class FakeDatabase:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor, None

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY='test-only')
        self.client = self.app.test_client()
        self.registration = {
            'username': 'newpatient',
            'password': 'Validpass1!',
            'full_name': '  Aditi Sharma  ',
            'email': '  Aditi.Sharma@Gmail.com ',
            'phone': ' 9876543210 ',
            'dob': '1999-02-28',
            'gender': 'Female',
            'blood_group': 'O+',
            'address': '12 Sample Road',
            'emergency_contact': '9123456789',
        }

    def test_names_accept_unicode_and_reject_empty_or_numeric_values(self):
        self.assertTrue(validate_person_name('Aditi Sharma'))
        self.assertTrue(validate_person_name('  सीमा जोशी '))
        self.assertTrue(validate_person_name("O’Connor D'Souza"))
        self.assertFalse(validate_person_name(''))
        self.assertFalse(validate_person_name('   '))
        self.assertFalse(validate_person_name('123456'))
        self.assertFalse(validate_person_name('Aditi2'))

    def test_indian_mobile_format_and_outer_whitespace(self):
        self.assertTrue(validate_indian_mobile(' 9876543210 '))
        self.assertTrue(validate_indian_mobile('6789012345'))
        for phone in ('5876543210', '0123456789', '987654321', '98765432100',
                      '+919876543210', '98765 43210', 'abcdefghij'):
            with self.subTest(phone=phone):
                self.assertFalse(validate_indian_mobile(phone))

    def test_email_syntax_normalization_and_domain_structure(self):
        self.assertEqual(normalize_email(' Aditi.Sharma@Gmail.com '), 'aditi.sharma@gmail.com')
        for email in (
            'aditi@gmail.com', 'name@yahoo.com', 'name@yahoo.co.in',
            'name@outlook.com', 'name@nmims.in', 'name+tag@yahoo.co.in',
        ):
            with self.subTest(email=email):
                self.assertTrue(validate_email(email))
        for email in (
            '', 'gmail.com', 'hdfghd.com', 'name@', '@gmail.com',
            'name@example.org', 'name@gmail.co', 'name@Yahoo.co',
            'name@@gmail.com', 'name..tag@gmail.com', 'name+@gmail.com',
            '+++@gmail.com', 'name @gmail.com',
        ):
            with self.subTest(email=email):
                self.assertFalse(validate_email(email))

    def test_password_strength_policy(self):
        self.assertTrue(validate_password('Validpass1!'))
        for password in ('weak', 'lowercase1!', 'UPPERCASE1!', 'NoDigits!!',
                         'NoSpecial1', 'Ab1!'):
            with self.subTest(password=password):
                self.assertFalse(validate_password(password))

    def test_registration_rejects_invalid_fields_before_database_access(self):
        cases = (
            ('full_name', '123456', 'Full name must contain letters'),
            ('phone', '5876543210', 'Phone number must be a 10-digit'),
            ('email', 'user@example.org', 'Enter a valid email address using'),
            ('password', 'weak', 'Password must be at least 8 characters'),
            ('emergency_contact', '5551234567', 'Emergency contact must be a 10-digit'),
            ('address', '   ', 'All fields are required'),
        )
        for field, value, message in cases:
            with self.subTest(field=field), patch.object(auth, 'get_db') as database:
                data = dict(self.registration)
                data[field] = value
                response = self.client.post('/auth/register', data=data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, response.get_data(as_text=True))
                database.assert_not_called()

    def test_registration_rejects_unapproved_or_malformed_emails_server_side(self):
        invalid_emails = ('user@example.org', 'name@yahoo.co', 'gibberish', '', '@gmail.com', '+++@gmail.com')
        for email in invalid_emails:
            with self.subTest(email=email), patch.object(auth, 'get_db') as database:
                data = dict(self.registration)
                data['email'] = email
                response = self.client.post('/auth/register', data=data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    'Enter a valid email address using gmail.com, yahoo.com, yahoo.co.in, outlook.com, or nmims.in.',
                    response.get_data(as_text=True)
                )
                database.assert_not_called()

    def test_valid_registration_normalizes_email_and_persists_hashed_password_and_patient(self):
        cursor = FakeCursor()
        with patch.object(auth, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/auth/register', data=self.registration)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(cursor.calls), 3)
        user_insert = cursor.calls[1][1]
        patient_insert = cursor.calls[2][1]
        self.assertEqual(user_insert[2:], ('patient', 'Aditi Sharma', 'aditi.sharma@gmail.com', '9876543210'))
        self.assertTrue(verify_password(user_insert[1], 'Validpass1!'))
        self.assertEqual(patient_insert, (100, '1999-02-28', 'Female', 'O+', '12 Sample Road', '9123456789'))

    def test_duplicate_account_is_rejected_after_normalization(self):
        cursor = FakeCursor(duplicate=True)
        with patch.object(auth, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/auth/register', data=self.registration)

        self.assertEqual(response.status_code, 200)
        self.assertIn('already registered', response.get_data(as_text=True))
        self.assertEqual(len(cursor.calls), 1)

    def test_registration_database_failure_returns_clear_error_not_exception(self):
        with patch.object(auth, 'get_db', side_effect=RuntimeError('internal db detail')):
            response = self.client.post('/auth/register', data=self.registration)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('A database error occurred during registration.', page)
        self.assertNotIn('internal db detail', page)

    def test_admin_account_forms_apply_server_validation(self):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 1
            flask_session['role'] = 'admin'

        doctor_data = {
            'username': 'doctor',
            'password': 'weak',
            'full_name': 'Dr Example',
            'email': 'doctor@example.org',
            'phone': '9876543210',
            'department': 'General',
            'specialization': 'General',
            'qualification': 'MBBS',
            'room_number': '101',
            'consulting_fee': '500',
        }
        with patch.object(admin, 'query_one') as query, patch.object(admin, 'execute_dml') as write:
            response = self.client.post('/admin/doctors', data=doctor_data)
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertIn('doctor email using gmail.com', str(flask_session['_flashes']))
        query.assert_not_called()
        write.assert_not_called()

        doctor_data['email'] = 'doctor@gmail.com'
        with patch.object(admin, 'query_one') as query, patch.object(admin, 'execute_dml') as write:
            response = self.client.post('/admin/doctors', data=doctor_data)
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertIn('Temporary password must be at least 8 characters', str(flask_session['_flashes']))
        query.assert_not_called()
        write.assert_not_called()

        staff_data = {
            'username': 'reception',
            'password': 'Validpass1!',
            'role': 'receptionist',
            'full_name': 'Reception One',
            'email': 'reception@unsupported.org',
            'phone': '5123456789',
            'role_title': 'Front desk',
            'department': 'Reception',
        }
        with patch.object(admin, 'query_one') as query, patch.object(admin, 'execute_dml') as write:
            response = self.client.post('/admin/staff', data=staff_data)
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertIn('staff email using gmail.com', str(flask_session['_flashes']))
        query.assert_not_called()
        write.assert_not_called()

        staff_data['email'] = 'reception@gmail.com'
        with patch.object(admin, 'query_one') as query, patch.object(admin, 'execute_dml') as write:
            response = self.client.post('/admin/staff', data=staff_data)
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertIn('Staff phone must be a 10-digit', str(flask_session['_flashes']))
        query.assert_not_called()
        write.assert_not_called()

        doctor_data['password'] = 'Validpass1!'
        doctor_data['email'] = ' DOCTOR@Gmail.com '
        cursor = FakeCursor()
        with patch.object(admin, 'query_one', return_value=None), \
                patch.object(admin, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/admin/doctors', data=doctor_data)
        self.assertEqual(response.status_code, 302)
        user_insert = next(call for call in cursor.calls if 'INSERT INTO users' in call[0])
        doctor_insert = next(call for call in cursor.calls if 'INSERT INTO doctors' in call[0])
        schedule_inserts = [
            call for call in cursor.calls if 'INSERT INTO doctor_schedules' in call[0]
        ]
        self.assertTrue(verify_password(user_insert[1][1], 'Validpass1!'))
        self.assertEqual(user_insert[1][4], 'doctor@gmail.com')
        self.assertEqual(doctor_insert[1][0], 100)
        self.assertEqual(len(schedule_inserts), 5)
        self.assertTrue(all(call[1][0] == 100 for call in schedule_inserts))

        staff_data['email'] = ' RECEPTION@Yahoo.co.in '
        staff_data['phone'] = ' 9876543210 '
        cursor = FakeCursor()
        with patch.object(admin, 'query_one', return_value=None), \
                patch.object(admin, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/admin/staff', data=staff_data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(cursor.calls), 2)
        self.assertTrue(verify_password(cursor.calls[0][1][1], 'Validpass1!'))
        self.assertEqual(cursor.calls[0][1][4], 'reception@yahoo.co.in')
        self.assertEqual(cursor.calls[1][1], (100, 'Front desk', 'Reception'))

    def test_legacy_weak_password_can_still_log_in_and_maps_patient_id(self):
        from utils.auth_helpers import hash_password

        user = {
            'id': 20,
            'username': 'legacy-patient',
            'password_hash': hash_password('weakold'),
            'role': 'patient',
            'full_name': 'Legacy Patient',
            'status': 'active',
        }
        with patch.object(auth, 'query_one', side_effect=[user, {'id': 77}]):
            response = self.client.post('/auth/login', data={
                'username': 'legacy-patient',
                'password': 'weakold',
            })

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertEqual(flask_session['user_id'], 20)
            self.assertEqual(flask_session['patient_id'], 77)

    def test_patient_profile_update_validates_and_updates_only_own_entities(self):
        user = {
            'id': 20, 'role': 'patient', 'full_name': 'Old Name',
            'email': 'old@gmail.com', 'phone': '9876543210', 'username': 'patient',
        }
        patient_record = {'id': 77, 'address': 'Old address', 'emergency_contact': '9123456789'}
        cursor = FakeCursor()
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 20
            flask_session['role'] = 'patient'
        with patch.object(auth, 'query_one', side_effect=[user, patient_record, None]), \
                patch.object(auth, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/auth/profile', data={
                'full_name': '  Kavya  Rao ',
                'email': ' KAVYA@Yahoo.com ',
                'phone': ' 9876543210 ',
                'address': 'New address',
                'emergency_contact': ' 9123456789 ',
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(cursor.calls[0][1], ('Kavya Rao', 'kavya@yahoo.com', '9876543210', 20))
        self.assertEqual(cursor.calls[1][1], ('New address', '9123456789', 77, 20))
        self.assertIn('AND user_id = %s', cursor.calls[1][0])

    def test_patient_profile_rejects_invalid_email_and_mobile_without_writing(self):
        user = {
            'id': 20, 'role': 'patient', 'full_name': 'Old Name',
            'email': 'old@gmail.com', 'phone': '9876543210', 'username': 'patient',
        }
        patient_record = {'id': 77, 'address': 'Old address', 'emergency_contact': '9123456789'}
        invalid_submissions = (
            ({'email': 'hdfghd.com'}, 'Enter a valid email address using'),
            ({'email': 'person@unsupported.org'}, 'Enter a valid email address using'),
            ({'email': ''}, 'Enter a valid email address using'),
            ({'phone': '5123456789'}, 'Phone number must be a 10-digit'),
            ({'emergency_contact': '5123456789'}, 'Emergency contact must be a 10-digit'),
        )
        for changes, expected_message in invalid_submissions:
            with self.subTest(changes=changes):
                with self.client.session_transaction() as flask_session:
                    flask_session['user_id'] = 20
                    flask_session['role'] = 'patient'
                form = {
                    'full_name': 'Kavya Rao',
                    'email': 'kavya@yahoo.com',
                    'phone': '9876543210',
                    'address': 'New address',
                    'emergency_contact': '9123456789',
                }
                form.update(changes)
                with patch.object(auth, 'query_one', side_effect=[user, patient_record]), \
                        patch.object(auth, 'get_db') as database:
                    response = self.client.post('/auth/profile', data=form)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected_message, response.get_data(as_text=True))
                database.assert_not_called()

    def test_profile_lookup_database_error_is_handled_as_a_failed_request(self):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 20
            flask_session['role'] = 'patient'
        with patch.object(auth, 'query_one', side_effect=RuntimeError('internal db detail')):
            response = self.client.get('/auth/profile')

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertIn('Profile information could not be loaded.', str(flask_session['_flashes']))

    def test_patient_record_query_uses_database_mapping_not_cached_session_id(self):
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 20
            flask_session['role'] = 'patient'
            flask_session['patient_id'] = 999
        executed = []

        def resolve_patient(sql, params=None):
            self.assertIn('WHERE user_id = %s', sql)
            self.assertEqual(params, (20,))
            return {'id': 77}

        def read_records(sql, params=None):
            executed.append((sql, params))
            return []

        with patch.object(patient, 'query_one', side_effect=resolve_patient), \
                patch.object(patient, 'query_all', side_effect=read_records):
            response = self.client.get('/patient/records')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(executed[0][1], (77,))
        self.assertIn('WHERE mr.patient_id = %s', executed[0][0])
        with self.client.session_transaction() as flask_session:
            self.assertEqual(flask_session['patient_id'], 77)

    def test_role_routes_deny_other_roles(self):
        dashboard_urls = {
            'patient': '/patient/dashboard',
            'doctor': '/doctor/dashboard',
            'receptionist': '/receptionist/dashboard',
            'admin': '/admin/dashboard',
        }
        for current_role, own_dashboard in dashboard_urls.items():
            for target_role, protected_url in dashboard_urls.items():
                if current_role == target_role:
                    continue
                with self.subTest(current_role=current_role, target_role=target_role):
                    with self.client.session_transaction() as flask_session:
                        flask_session['user_id'] = 20
                        flask_session['role'] = current_role
                    response = self.client.get(protected_url)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.headers['Location'].endswith(own_dashboard))

    def test_each_role_can_access_its_own_dashboard(self):
        role_routes = {
            'patient': ('/patient/dashboard', patient),
            'doctor': ('/doctor/dashboard', doctor),
            'receptionist': ('/receptionist/dashboard', receptionist),
            'admin': ('/admin/dashboard', admin),
        }
        for role, (url, route_module) in role_routes.items():
            with self.subTest(role=role):
                with self.client.session_transaction() as flask_session:
                    flask_session['user_id'] = 20
                    flask_session['role'] = role
                    if role == 'doctor':
                        flask_session['doctor_id'] = 33
                    elif role == 'patient':
                        flask_session['patient_id'] = 77

                with patch.object(route_module, 'query_all', return_value=[]):
                    if role == 'patient':
                        with patch.object(patient, 'query_one', return_value={'id': 77}):
                            response = self.client.get(url)
                    elif role == 'admin':
                        with patch.object(admin, 'query_one', return_value={'count': 0, 'sum': 0}):
                            response = self.client.get(url)
                    else:
                        response = self.client.get(url)

                self.assertEqual(response.status_code, 200)

    def test_registration_form_has_client_constraints(self):
        response = self.client.get('/auth/register')
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('minlength="8"', page)
        self.assertIn('pattern="\\s*[6-9][0-9]{9}\\s*"', page)
        self.assertIn('gmail\\.com|yahoo\\.com|yahoo\\.co\\.in|outlook\\.com|nmims\\.in', page)


if __name__ == '__main__':
    unittest.main()
