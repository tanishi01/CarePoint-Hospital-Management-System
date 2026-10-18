import unittest
from unittest.mock import patch

from app import create_app
from routes import admin, beds, receptionist


class CheckinCursor:
    def __init__(self, appointment):
        self.appointment = appointment
        self.rowcount = 0
        self.update_attempted = False

    def execute(self, sql, params=()):
        if sql.lstrip().startswith('SELECT a.*'):
            self.appointment_result = (
                dict(self.appointment)
                if self.appointment['status'] in ('pending', 'approved')
                and self.appointment['appointment_date'] == 'today'
                else None
            )
        elif sql.lstrip().startswith('UPDATE appointments'):
            self.update_attempted = True
            if self.appointment['status'] in ('pending', 'approved'):
                self.appointment['status'] = 'checked_in'
                self.rowcount = 1
            else:
                self.rowcount = 0

    def fetchone(self):
        return getattr(self, 'appointment_result', None)


class CheckinDatabase:
    def __init__(self, appointment):
        self.cursor = CheckinCursor(appointment)
        self.committed = False

    def __enter__(self):
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False


class EmptyCursor:
    rowcount = 0

    def execute(self, sql, params=()):
        pass

    def fetchone(self):
        return None


class EmptyDatabase:
    def __enter__(self):
        return EmptyCursor(), self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class ReceptionistAndLogoutRegressionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY='regression-test-key')

    def client_for(self, role='receptionist', user_id=7):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['role'] = role
            session['user_id'] = user_id
        return client

    def test_admin_overview_has_no_bed_management_shortcuts(self):
        client = self.client_for('admin', 1)

        def metric(sql, params=None):
            return {'count': 0, 'sum': 0}

        with patch.object(admin, 'query_one', side_effect=metric), \
                patch.object(admin, 'query_all', return_value=[]):
            response = client.get('/admin/dashboard')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('/beds/availability', page)
        self.assertNotIn('/admin/wards', page)
        self.assertNotIn('Wards and beds', page)

    def test_admin_ward_setup_preserves_creation_without_bed_management_shortcut(self):
        with patch.object(admin, 'query_all', return_value=[]):
            response = self.client_for('admin', 1).get('/admin/wards')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Create ward and beds', page)
        self.assertIn('Receptionists manage bed availability', page)
        self.assertNotIn('href="/beds/availability"', page)

    def test_bed_management_routes_remain_receptionist_only(self):
        with patch.object(beds, 'query_all', return_value=[]), \
                patch.object(beds, 'get_db', return_value=EmptyDatabase()):
            client = self.client_for()
            availability = client.get('/beds/availability')
            allocation = client.post('/beds/allocate', data={})
            discharge = client.post('/beds/discharge/1')

        self.assertEqual(availability.status_code, 200)
        self.assertEqual(allocation.location, '/beds/availability')
        self.assertEqual(discharge.location, '/beds/availability')

        for role in ('admin', 'patient', 'doctor'):
            unauthorized = self.client_for(role).get('/beds/availability')
            self.assertEqual(unauthorized.status_code, 302)
            self.assertIn(f'/{role}/dashboard', unauthorized.location)

    def test_dashboard_pending_count_and_list_exclude_ineligible_statuses(self):
        appointments = [
            {
                'id': 1, 'status': 'pending', 'appointment_time': '09:00',
                'patient_name': 'Waiting Patient', 'doctor_name': 'Doctor',
                'room_number': '1',
            },
            {
                'id': 2, 'status': 'approved', 'appointment_time': '09:30',
                'patient_name': 'Approved Patient', 'doctor_name': 'Doctor',
                'room_number': '1',
            },
            {
                'id': 3, 'status': 'checked_in', 'appointment_time': '10:00',
                'patient_name': 'Arrived Patient', 'doctor_name': 'Doctor',
                'room_number': '1',
            },
            {
                'id': 4, 'status': 'cancelled', 'appointment_time': '10:30',
                'patient_name': 'Cancelled Patient', 'doctor_name': 'Doctor',
                'room_number': '1',
            },
        ]
        with patch.object(receptionist, 'query_all', side_effect=[appointments, []]):
            response = self.client_for().get('/receptionist/dashboard')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<strong>2</strong>', page)
        self.assertIn('Waiting Patient', page)
        self.assertIn('Approved Patient', page)
        self.assertNotIn('Arrived Patient', page.split('Pending check-ins', 1)[1])
        self.assertNotIn('Cancelled Patient', page.split('Pending check-ins', 1)[1])

    def test_appointments_page_only_offers_checkin_for_today_eligible_records(self):
        appointments = [
            {
                'id': 1, 'status': 'pending', 'can_checkin': 1,
                'appointment_date': 'today', 'appointment_time': '09:00',
                'patient_name': 'Today', 'phone': '000', 'doctor_name': 'Doctor',
                'department': 'General', 'reason': 'Review',
            },
            {
                'id': 2, 'status': 'pending', 'can_checkin': 0,
                'appointment_date': 'future', 'appointment_time': '09:00',
                'patient_name': 'Future', 'phone': '000', 'doctor_name': 'Doctor',
                'department': 'General', 'reason': 'Review',
            },
            {
                'id': 3, 'status': 'cancelled', 'can_checkin': 0,
                'appointment_date': 'today', 'appointment_time': '09:00',
                'patient_name': 'Cancelled', 'phone': '000', 'doctor_name': 'Doctor',
                'department': 'General', 'reason': 'Review',
            },
        ]
        with patch.object(receptionist, 'query_all', return_value=appointments):
            response = self.client_for().get('/receptionist/appointments')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(page.count('>Check in</button>'), 1)

    def test_checkin_updates_once_and_refresh_removes_patient_from_pending_list(self):
        appointment = {
            'id': 5, 'status': 'pending', 'appointment_date': 'today',
            'appointment_time': '09:00', 'patient_user_id': 21,
            'patient_name': 'Checked Patient', 'doctor_user_id': 31,
            'doctor_name': 'Doctor', 'room_number': '1',
        }
        database = CheckinDatabase(appointment)
        with patch.object(receptionist, 'get_db', return_value=database), \
                patch.object(receptionist, 'create_notification', return_value=True), \
                patch.object(
                    receptionist,
                    'query_all',
                    side_effect=lambda sql: (
                        [dict(appointment)] if 'FROM appointments' in sql else []
                    )
                ):
            client = self.client_for()
            response = client.post('/receptionist/checkin/5')
            dashboard = client.get('/receptionist/dashboard')

        page = dashboard.get_data(as_text=True)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(database.committed)
        self.assertEqual(appointment['status'], 'checked_in')
        self.assertIn('<strong>0</strong>', page)
        self.assertIn('<strong>1</strong>', page)
        pending_section = page.split('Pending check-ins', 1)[1]
        self.assertNotIn('Checked Patient', pending_section)

    def test_duplicate_checkin_does_not_update_or_report_success(self):
        appointment = {
            'id': 5, 'status': 'checked_in', 'appointment_date': 'today',
            'appointment_time': '09:00', 'patient_user_id': 21,
            'patient_name': 'Checked Patient', 'doctor_user_id': 31,
        }
        database = CheckinDatabase(appointment)
        with patch.object(receptionist, 'get_db', return_value=database):
            response = self.client_for().post('/receptionist/checkin/5')

        self.assertEqual(response.status_code, 302)
        self.assertFalse(database.cursor.update_attempted)
        self.assertEqual(appointment['status'], 'checked_in')

    def test_dashboard_database_failure_is_logged_and_not_reported_as_empty(self):
        client = self.client_for()
        with patch.object(
            receptionist, 'query_all', side_effect=RuntimeError('database unavailable')
        ), self.assertLogs(self.app.logger.name, level='ERROR') as logs:
            response = client.get('/receptionist/dashboard')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('temporarily unavailable', page)
        self.assertNotIn('No patients are currently waiting', page)
        self.assertTrue(any('Unable to load receptionist dashboard data' in row for row in logs.output))

    def test_logout_clears_session_and_all_role_protected_pages_redirect(self):
        protected_pages = {
            'patient': '/patient/dashboard',
            'doctor': '/doctor/dashboard',
            'receptionist': '/receptionist/dashboard',
            'admin': '/admin/dashboard',
        }
        for role, protected_page in protected_pages.items():
            client = self.client_for(role)
            logout = client.get('/auth/logout')
            self.assertEqual(logout.status_code, 302)
            self.assertIn('no-store', logout.headers['Cache-Control'])
            with client.session_transaction() as session:
                self.assertNotIn('user_id', session)
                self.assertNotIn('role', session)

            response = client.get(protected_page)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/auth/login', response.location)
            self.assertIn('no-store', response.headers['Cache-Control'])

    def test_authenticated_html_response_is_not_cached(self):
        with patch.object(receptionist, 'query_all', side_effect=[[], []]):
            response = self.client_for().get('/receptionist/dashboard')

        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Pragma'], 'no-cache')
        self.assertEqual(response.headers['Expires'], '0')


if __name__ == '__main__':
    unittest.main()
