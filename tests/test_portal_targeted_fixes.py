import unittest
from datetime import date, datetime
from unittest.mock import patch

from app import create_app
from routes import admin, doctor, patient, pharmacy
from utils.auth_helpers import verify_password


class FakeDatabase:
    def __init__(self, cursor):
        self.cursor = cursor
        self.committed = False

    def __enter__(self):
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False


class StatefulApprovalCursor:
    def __init__(self):
        self.status = 'pending'
        self.rowcount = 0
        self._one = None
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if 'SELECT status FROM appointments' in sql:
            self._one = {'status': self.status}
        elif "UPDATE appointments SET status = 'approved'" in sql:
            if self.status == 'pending':
                self.status = 'approved'
                self.rowcount = 1
            else:
                self.rowcount = 0

    def fetchone(self):
        return self._one


class StaffCursor:
    def __init__(self, staff_member=None):
        self.staff_member = staff_member
        self.rowcount = 1
        self.lastrowid = 201
        self.calls = []
        self._one = None

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if 'SELECT s.id, s.user_id, u.status, u.role' in sql:
            self._one = self.staff_member

    def fetchone(self):
        return self._one


class LeaveCancellationCursor:
    def __init__(
        self,
        affected_rows=1,
        leave_owner=8,
        leave_status='pending',
        fail_update=False,
    ):
        self.rowcount = 0
        self.affected_rows = affected_rows
        self.leave_owner = leave_owner
        self.leave_status = leave_status
        self.fail_update = fail_update
        self.saved_status = leave_status
        self._one = None
        self.update_sql = None
        self.update_params = None
        self.committed = False

    def execute(self, sql, params=()):
        if 'SELECT id FROM doctors' in sql:
            self._one = {'id': params[0]}
        elif 'FROM doctor_leave_requests' in sql:
            leave_id, doctor_id = params
            self._one = (
                {'id': leave_id}
                if doctor_id == self.leave_owner and self.leave_status == 'pending'
                else None
            )
        elif "UPDATE doctor_leave_requests" in sql:
            if self.fail_update:
                raise RuntimeError('database update failed')
            self.update_sql = sql
            self.update_params = params
            self.rowcount = self.affected_rows
            if self.rowcount == 1:
                self.saved_status = 'cancelled'

    def fetchone(self):
        return self._one

    def fetchall(self):
        return []


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 12, 8, 0, 0)


class UnavailableRescheduleCursor:
    def __init__(self):
        self.rowcount = 0
        self.update_count = 0
        self._one = None
        self._many = []

    def execute(self, sql, params=()):
        if 'SELECT id FROM patients' in sql or 'SELECT id FROM doctors' in sql:
            self._one = {'id': params[0]}
        elif 'SELECT id, patient_id, status' in sql:
            self._one = {'id': 15, 'patient_id': 44, 'status': 'approved'}
        elif 'FROM doctor_schedules ds' in sql:
            self._many = [{
                'start_time': '09:00:00',
                'end_time': '11:00:00',
                'break_start': None,
                'break_end': None,
                'slot_duration_mins': 30,
            }]
        elif 'FROM doctor_leave_requests' in sql:
            self._many = []
        elif 'SELECT appointment_time' in sql and 'FROM appointments' in sql:
            self._many = [{'appointment_time': '09:00:00'}]
        elif 'FROM appointments a' in sql and 'a.patient_id = %s' in sql:
            self._many = []
        elif 'UPDATE appointments' in sql:
            self.update_count += 1
            self.rowcount = 1

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class TargetedPortalFixTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY='test-only')
        self.client = self.app.test_client()

    def set_session(self, role, user_id=1, **values):
        with self.client.session_transaction() as current:
            current['role'] = role
            current['user_id'] = user_id
            current.update(values)

    def test_appointment_approval_persists_and_appears_after_fresh_page_read(self):
        self.set_session('doctor', doctor_id=8, user_id=80)
        cursor = StatefulApprovalCursor()
        appointment = {
            'id': 15,
            'doctor_id': 8,
            'patient_id': 44,
            'patient_user_id': 90,
            'appointment_date': '2026-10-18',
            'appointment_time': '09:00:00',
            'status': 'pending',
        }
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'query_one', return_value=appointment), \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(doctor, 'create_notification'):
            response = self.client.post(
                '/doctor/appointments/15/action', data={'action': 'approve'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cursor.status, 'approved')

        saved = dict(appointment, status=cursor.status)
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'query_all', return_value=[saved]):
            refreshed = self.client.get('/doctor/appointments')
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn('Approved', refreshed.get_data(as_text=True))

    def test_appointment_approval_rejects_non_pending_status_without_update(self):
        self.set_session('doctor', doctor_id=8, user_id=80)
        cursor = StatefulApprovalCursor()
        cursor.status = 'cancelled'
        appointment = {
            'id': 15, 'doctor_id': 8, 'patient_user_id': 90,
            'appointment_date': '2026-10-18', 'appointment_time': '09:00:00',
            'status': 'cancelled',
        }
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'query_one', return_value=appointment), \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(doctor, 'create_notification') as notification:
            response = self.client.post(
                '/doctor/appointments/15/action', data={'action': 'approve'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cursor.status, 'cancelled')
        self.assertFalse(any('UPDATE appointments' in sql for sql, _ in cursor.statements))
        notification.assert_not_called()

    def test_reschedule_rejects_an_unavailable_slot_without_changing_appointment(self):
        self.set_session('doctor', doctor_id=8, user_id=80)
        cursor = UnavailableRescheduleCursor()
        original = {
            'id': 15, 'doctor_id': 8, 'patient_id': 44, 'patient_user_id': 90,
            'appointment_date': date(2026, 10, 12),
            'appointment_time': '09:00:00', 'status': 'approved',
        }
        with patch.object(doctor, 'datetime', FrozenDateTime), \
                patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'query_one', return_value=original), \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(doctor, 'create_notification') as notification:
            response = self.client.post('/doctor/appointments/15/action', data={
                'action': 'reschedule',
                'new_date': '2026-10-13',
                'new_time': '09:00',
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cursor.update_count, 0)
        self.assertEqual(original['appointment_date'], date(2026, 10, 12))
        self.assertEqual(original['status'], 'approved')
        notification.assert_not_called()

    def test_doctor_can_cancel_only_own_pending_leave_request(self):
        self.set_session('doctor', user_id=80, doctor_id=8)
        cursor = LeaveCancellationCursor()
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/doctor/leave/12/cancel')
        self.assertEqual(response.status_code, 302)
        self.assertIn('doctor_id = %s', cursor.update_sql)
        self.assertIn("status = 'pending'", cursor.update_sql)
        self.assertEqual(cursor.update_params, (12, 8))

        other_cursor = LeaveCancellationCursor(affected_rows=0)
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(other_cursor)):
            response = self.client.post('/doctor/leave/99/cancel')
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as current:
            flashes = current.get('_flashes', [])
        self.assertIn('not found', flashes[-1][1])

    def test_leave_request_cancellation_records_status_and_history_hides_action(self):
        self.set_session('doctor', user_id=80, doctor_id=8)
        cursor = LeaveCancellationCursor()
        database = FakeDatabase(cursor)
        cancelled_request = {
            'id': 12, 'leave_date': '2026-10-20', 'is_full_day': 1,
            'start_time': None, 'end_time': None, 'reason': 'Emergency',
            'status': None, 'rejection_reason': None,
            'cancellation_reason': None, 'submitted_at': '2026-10-17 10:00:00',
            'reviewed_at': None,
        }
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'get_db', return_value=database), \
                patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(
                    doctor,
                    'query_all',
                    side_effect=lambda *args: [
                        dict(cancelled_request, status=cursor.saved_status)
                    ]
                ):
            response = self.client.post('/doctor/leave/12/cancel', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(database.committed)
        self.assertEqual(cursor.saved_status, 'cancelled')
        self.assertEqual(cursor.update_params, (12, 8))
        page = response.get_data(as_text=True)
        self.assertIn('Cancelled', page)
        self.assertNotIn('Cancel request', page)

    def test_leave_request_cancellation_rejects_another_doctor_and_non_pending_request(self):
        self.set_session('doctor', user_id=80, doctor_id=8)
        for cursor in (
            LeaveCancellationCursor(leave_owner=9),
            LeaveCancellationCursor(leave_status='approved'),
        ):
            with self.subTest(owner=cursor.leave_owner, status=cursor.leave_status), \
                    patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                    patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)):
                response = self.client.post('/doctor/leave/12/cancel')
            self.assertEqual(response.status_code, 302)
            self.assertIsNone(cursor.update_sql)
            self.assertEqual(cursor.saved_status, cursor.leave_status)

    def test_leave_request_cancellation_database_failure_logs_and_does_not_report_success(self):
        self.set_session('doctor', user_id=80, doctor_id=8)
        cursor = LeaveCancellationCursor(fail_update=True)
        database = FakeDatabase(cursor)
        with self.app.app_context():
            with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                    patch.object(doctor, 'get_db', return_value=database), \
                    patch.object(doctor.current_app.logger, 'exception') as log_exception:
                response = self.client.post('/doctor/leave/12/cancel')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(database.committed)
        log_exception.assert_called_once()
        with self.client.session_transaction() as current:
            flashes = current.get('_flashes', [])
        self.assertIn('Leave request could not be cancelled', flashes[-1][1])
        self.assertNotIn('Pending leave request cancelled.', [message for _, message in flashes])

    def test_leave_request_cancel_button_has_readable_red_states(self):
        self.set_session('doctor', user_id=80, doctor_id=8)
        pending_request = {
            'id': 12, 'leave_date': '2026-10-20', 'is_full_day': 1,
            'start_time': None, 'end_time': None, 'reason': 'Emergency',
            'status': 'pending', 'rejection_reason': None,
            'cancellation_reason': None, 'submitted_at': '2026-10-17 10:00:00',
            'reviewed_at': None,
        }
        with patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'query_all', return_value=[pending_request]):
            response = self.client.get('/doctor/leave')
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="button button-small leave-cancel-button"', response.get_data(as_text=True))

        from pathlib import Path
        stylesheet = (
            Path(__file__).resolve().parents[1] / 'static' / 'css' / 'site.css'
        ).read_text(encoding='utf-8')
        self.assertIn('.leave-cancel-button {', stylesheet)
        self.assertIn('color: #7a1f22', stylesheet)
        self.assertIn('border: 1px solid #b53a3d', stylesheet)
        self.assertIn('background: #fff0ef', stylesheet)
        self.assertIn('.leave-cancel-button:hover', stylesheet)
        self.assertIn('.leave-cancel-button:focus-visible', stylesheet)

    def test_staff_creation_hashes_password_and_duplicate_account_is_rejected(self):
        self.set_session('admin')
        staff_form = {
            'username': 'frontdesk',
            'password': 'Validpass1!',
            'role': 'receptionist',
            'full_name': 'Rita Sharma',
            'email': 'rita@gmail.com',
            'phone': '9876543210',
            'role_title': 'Receptionist',
            'department': 'Front desk',
        }
        cursor = StaffCursor()
        with patch.object(admin, 'query_one', return_value=None), \
                patch.object(admin, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/admin/staff', data=staff_form)
        self.assertEqual(response.status_code, 302)
        user_insert = next(params for sql, params in cursor.calls if 'INSERT INTO users' in sql)
        self.assertTrue(verify_password(user_insert[1], 'Validpass1!'))
        self.assertNotEqual(user_insert[1], 'Validpass1!')

        cursor = StaffCursor()
        with patch.object(admin, 'query_one', return_value={'id': 7}), \
                patch.object(admin, 'get_db') as database:
            response = self.client.post('/admin/staff', data=staff_form)
        self.assertEqual(response.status_code, 302)
        database.assert_not_called()

    def test_staff_validation_and_edit_form_do_not_expose_password_fields(self):
        self.set_session('admin')
        valid_staff_form = {
            'username': 'frontdesk',
            'password': 'Validpass1!',
            'role': 'receptionist',
            'full_name': 'Rita Sharma',
            'email': 'rita@gmail.com',
            'phone': '9876543210',
            'role_title': 'Receptionist',
            'department': 'Front desk',
        }
        for field, value in (
            ('role', 'admin'),
            ('email', 'rita@example.org'),
            ('phone', '5876543210'),
            ('full_name', '1234'),
        ):
            invalid_form = dict(valid_staff_form)
            invalid_form[field] = value
            with self.subTest(field=field), patch.object(admin, 'get_db') as database:
                response = self.client.post('/admin/staff', data=invalid_form)
            self.assertEqual(response.status_code, 302)
            database.assert_not_called()

        editing_staff = {
            'staff_id': 7, 'role_title': 'Receptionist', 'department': 'Front desk',
            'username': 'frontdesk', 'full_name': 'Rita Sharma',
            'email': 'rita@gmail.com', 'phone': '9876543210',
            'role': 'receptionist', 'status': 'active',
        }
        with patch.object(admin, 'query_one', return_value=editing_staff), \
                patch.object(admin, 'query_all', return_value=[]):
            response = self.client.get('/admin/staff?edit=7')
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('name="staff_id" value="7"', page)
        self.assertNotIn('name="password"', page)
        self.assertNotIn('password_hash', page)

    def test_staff_edit_does_not_reset_password_and_status_changes_require_confirmation(self):
        self.set_session('admin', user_id=1)
        form = {
            'staff_id': '7',
            'username': 'frontdesk',
            'role': 'receptionist',
            'full_name': 'Rita Sharma',
            'email': 'rita@gmail.com',
            'phone': '9876543210',
            'role_title': 'Receptionist',
            'department': 'Front desk',
        }
        cursor = StaffCursor()
        with patch.object(admin, 'query_one', side_effect=[
            {'id': 7, 'user_id': 44, 'role': 'receptionist'}, None
        ]), patch.object(admin, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/admin/staff', data=form)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('UPDATE users' in sql for sql, _ in cursor.calls))
        self.assertFalse(any('password' in sql.lower() for sql, _ in cursor.calls))

        with patch.object(admin, 'get_db') as database:
            response = self.client.post('/admin/staff/7/toggle-status')
        self.assertEqual(response.status_code, 302)
        database.assert_not_called()

    def test_staff_status_update_and_self_deactivation_protection(self):
        self.set_session('admin', user_id=1)
        member = {'id': 7, 'user_id': 44, 'status': 'active', 'role': 'receptionist'}
        cursor = StaffCursor(member)
        with patch.object(admin, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post(
                '/admin/staff/7/toggle-status', data={'confirmed': '1'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [params for sql, params in cursor.calls if 'UPDATE users' in sql],
            [('inactive', 44)]
        )
        self.assertEqual(
            [params for sql, params in cursor.calls if 'UPDATE staff' in sql],
            [('inactive', 7)]
        )

        self.set_session('admin', user_id=44)
        protected = StaffCursor(member)
        with patch.object(admin, 'get_db', return_value=FakeDatabase(protected)):
            response = self.client.post(
                '/admin/staff/7/toggle-status', data={'confirmed': '1'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any('UPDATE ' in sql for sql, _ in protected.calls))

    def test_patient_booking_overview_and_cancellation_reason_presentation(self):
        self.set_session('patient', user_id=70)
        with patch.object(patient, 'get_current_patient_id', return_value=71), \
                patch.object(patient, 'query_all', side_effect=[[], [], []]):
            response = self.client.get('/patient/dashboard')
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Book an appointment', page)
        nav = page.split('<nav class="main-nav" id="main-nav"', 1)[1].split('</nav>', 1)[0]
        self.assertNotIn('Book appointment', nav)

        cancelled = {
            'appointment_date': '2026-10-18',
            'appointment_time': '09:00:00',
            'doctor_name': 'Dr. Example',
            'specialization': 'General',
            'department': 'General',
            'room_number': 'Room 1',
            'reason': 'Stomach pain',
            'status': 'cancelled',
            'cancellation_reason': None,
        }
        with patch.object(patient, 'get_current_patient_id', return_value=71), \
                patch.object(patient, 'query_all', return_value=[cancelled]):
            history = self.client.get('/patient/appointments')
        history_page = history.get_data(as_text=True)
        self.assertIn('<th>Visit reason</th><th>Cancellation reason</th>', history_page)
        self.assertIn('Stomach pain', history_page)
        self.assertNotIn('Cancellation reason: Stomach pain', history_page)

    def test_admin_inventory_remains_available_for_viewing_and_admin_writes_only(self):
        medicine = {
            'id': 3, 'name': 'Test medicine', 'generic_name': 'Generic',
            'unit_price': 10, 'stock_quantity': 5, 'min_stock_threshold': 2
        }
        self.set_session('admin')
        with patch.object(pharmacy, 'query_all', return_value=[medicine]):
            response = self.client.get('/pharmacy/inventory')
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Add medicine', page)
        self.assertIn('Minimum stock threshold', page)
        self.assertIn('Test medicine', page)

        self.set_session('doctor', user_id=8)
        with patch.object(pharmacy, 'query_all', return_value=[medicine]), \
                patch.object(pharmacy, 'execute_dml') as write:
            response = self.client.get('/pharmacy/inventory')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Test medicine', response.get_data(as_text=True))
        self.assertNotIn('Add medicine', response.get_data(as_text=True))
        write.assert_not_called()

        with patch.object(pharmacy, 'execute_dml') as write:
            response = self.client.post('/pharmacy/inventory', data={
                'name': 'Restricted medicine', 'generic_name': 'Restricted',
                'unit_price': '5', 'stock_quantity': '3',
                'min_stock_threshold': '1'
            })
        self.assertEqual(response.status_code, 302)
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
