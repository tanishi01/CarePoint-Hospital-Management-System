import unittest
from copy import deepcopy
from datetime import date, datetime, time
from threading import Barrier, Lock, Thread
from unittest.mock import patch

from pymysql.err import OperationalError

from app import create_app
from routes import admin, appointments, doctor
from utils.appointment_helper import build_slot_availability, is_slot_available


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 12, 8, 0, 0)


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 12)


class LeaveCursor:
    def __init__(self, state, lock=None, fail_notification=False):
        self.state = state
        self.lock = lock
        self.fail_notification = fail_notification
        self.rowcount = 0
        self.lastrowid = 900
        self._one = None
        self._many = []
        self.locked_doctor = False

    def execute(self, sql, params=()):
        self.rowcount = 0
        self._one = None
        self._many = []
        if 'SELECT id FROM doctors' in sql and 'FOR UPDATE' in sql:
            if self.lock:
                self.lock.acquire()
                self.locked_doctor = True
            self._one = {'id': 8}
        elif 'SELECT id FROM doctors WHERE id = %s AND user_id' in sql:
            self._one = {'id': 8}
        elif 'SELECT start_time, end_time, break_start, break_end' in sql:
            self._one = self.state.get('schedule')
        elif 'SELECT is_full_day, start_time, end_time' in sql:
            self._many = list(self.state.get('existing_leaves', []))
        elif 'INSERT INTO doctor_leave_requests' in sql:
            new_request = {
                'id': self.lastrowid,
                'doctor_id': params[0],
                'leave_date': params[1],
                'is_full_day': params[2],
                'start_time': params[3],
                'end_time': params[4],
                'reason': params[5],
                'status': 'pending',
                'rejection_reason': None,
                'cancellation_reason': None
            }
            self.state['leave_requests'].append(new_request)
            self.state['existing_leaves'].append(new_request)
            self.rowcount = 1
        elif 'SELECT doctor_id FROM doctor_leave_requests' in sql:
            leave = self.state['leave']
            self._one = {'doctor_id': leave['doctor_id']} if leave else None
        elif 'SELECT * FROM doctor_leave_requests' in sql:
            self._one = dict(self.state['leave']) if self.state['leave'] else None
        elif 'SELECT start_time, end_time, slot_duration_mins' in sql:
            self._one = self.state.get('schedule')
        elif 'FROM appointments' in sql and 'FOR UPDATE' in sql:
            self._many = list(self.state.get('appointments', []))
        elif 'SELECT p.user_id AS patient_user_id' in sql:
            appointment_id = params[0]
            appointment = next(
                row for row in self.state['appointments']
                if row['id'] == appointment_id
            )
            self._one = {
                'patient_user_id': appointment['patient_user_id'],
                'doctor_name': appointment['doctor_name']
            }
        elif "SET status = 'cancelled', cancellation_reason = %s" in sql:
            reason, appointment_id = params
            appointment = next(
                row for row in self.state['appointments']
                if row['id'] == appointment_id
            )
            appointment['status'] = 'cancelled'
            appointment['cancellation_reason'] = reason
            self.rowcount = 1
        elif 'INSERT INTO notifications' in sql:
            if self.fail_notification:
                raise RuntimeError('simulated notification failure')
            self.state['notifications'].append({
                'user_id': params[0], 'message': params[1]
            })
            self.rowcount = 1
        elif "SET status = 'approved'" in sql:
            self.state['leave']['status'] = 'approved'
            self.state['leave']['cancellation_reason'] = params[0]
            self.rowcount = 1
        elif "SET status = 'rejected'" in sql:
            self.state['leave']['status'] = 'rejected'
            self.state['leave']['rejection_reason'] = params[0]
            self.rowcount = 1

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)


class AtomicLeaveDatabase:
    def __init__(self, state, lock=None, fail_notification=False):
        self.state = state
        self.lock = lock
        self.fail_notification = fail_notification

    def __enter__(self):
        self.snapshot = deepcopy(self.state)
        self.cursor = LeaveCursor(
            self.state, self.lock, self.fail_notification
        )
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            self.state.clear()
            self.state.update(self.snapshot)
        if self.cursor.locked_doctor:
            self.lock.release()
        return False


class DoctorEmergencyLeaveTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY='leave-test-only')
        self.client = self.app.test_client()
        self.schedule = {
            'start_time': '09:00:00',
            'end_time': '11:00:00',
            'break_start': None,
            'break_end': None,
            'slot_duration_mins': 30
        }
        self.leave = {
            'id': 12,
            'doctor_id': 8,
            'leave_date': date(2026, 10, 13),
            'is_full_day': False,
            'start_time': '09:15:00',
            'end_time': '09:45:00',
            'reason': 'Emergency',
            'status': 'pending',
            'rejection_reason': None,
            'cancellation_reason': None
        }
        self.state = {
            'schedule': dict(self.schedule),
            'leave': dict(self.leave),
            'leave_requests': [],
            'existing_leaves': [],
            'appointments': [],
            'notifications': []
        }

    def set_session(self, role='doctor', user_id=20):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = role
            flask_session['user_id'] = user_id
            if role == 'doctor':
                flask_session['doctor_id'] = 8

    def submit_leave(self, **overrides):
        data = {
            'leave_date': '2026-10-13',
            'leave_kind': 'partial',
            'start_time': '09:15',
            'end_time': '09:45',
            'reason': 'Emergency'
        }
        data.update(overrides)
        return self.client.post('/doctor/leave', data=data)

    def decision(self, **overrides):
        data = {'action': 'approve', 'confirmed': '1',
                'cancellation_reason': 'Cancelled due to emergency'}
        data.update(overrides)
        return self.client.post('/admin/doctor-leaves/12/decision', data=data)

    def appointment(self, appointment_id, time_value, status='approved'):
        return {
            'id': appointment_id,
            'patient_id': appointment_id + 100,
            'patient_user_id': appointment_id + 200,
            'patient_name': 'Patient {0}'.format(appointment_id),
            'appointment_time': time_value,
            'status': status,
            'cancellation_reason': None,
            'doctor_name': 'Dr. Example'
        }

    def test_doctor_can_submit_partial_request_for_own_schedule(self):
        self.set_session()
        database = AtomicLeaveDatabase(self.state)
        with patch.object(doctor, 'date', FrozenDate), \
                patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'get_db', return_value=database):
            response = self.submit_leave()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.state['leave_requests']), 1)
        self.assertEqual(self.state['leave_requests'][0]['status'], 'pending')
        self.assertEqual(self.state['leave_requests'][0]['start_time'], '09:15:00')

    def test_doctor_sees_only_own_leave_history(self):
        self.set_session()
        own_request = dict(self.leave, doctor_id=8)
        with patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'query_all', return_value=[own_request]) as read_requests:
            response = self.client.get('/doctor/leave')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Emergency', response.get_data(as_text=True))
        self.assertIn('WHERE doctor_id = %s', read_requests.call_args.args[0])
        self.assertEqual(read_requests.call_args.args[1], (8,))

    def test_doctor_submission_rejects_invalid_range_and_break_overlap(self):
        self.set_session()
        self.state['schedule']['break_start'] = '09:30:00'
        self.state['schedule']['break_end'] = '10:00:00'
        with patch.object(doctor, 'date', FrozenDate), \
                patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            reversed_range = self.submit_leave(start_time='10:00', end_time='09:00')
            break_range = self.submit_leave(start_time='09:20', end_time='09:40')

        self.assertEqual(reversed_range.status_code, 302)
        self.assertEqual(break_range.status_code, 302)
        self.assertEqual(self.state['leave_requests'], [])

    def test_doctor_submission_rejects_invalid_date_and_missing_reason(self):
        self.set_session()
        with patch.object(doctor, 'date', FrozenDate), \
                patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'get_db') as database:
            bad_date = self.submit_leave(leave_date='2026-02-30')
            missing_reason = self.submit_leave(reason='   ')

        self.assertEqual(bad_date.status_code, 302)
        self.assertEqual(missing_reason.status_code, 302)
        database.assert_not_called()

    def test_doctor_submission_rejects_duplicate_overlap(self):
        self.set_session()
        self.state['existing_leaves'] = [{
            'is_full_day': False, 'start_time': '09:30:00', 'end_time': '10:00:00'
        }]
        with patch.object(doctor, 'date', FrozenDate), \
                patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            response = self.submit_leave()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state['leave_requests'], [])

    def test_concurrent_overlapping_submissions_serialize_on_doctor_row(self):
        self.state['existing_leaves'] = []
        lock = Lock()
        barrier = Barrier(2)

        class ConcurrentDatabase:
            def __enter__(inner_self):
                barrier.wait(timeout=5)
                inner_self.database = AtomicLeaveDatabase(self.state, lock)
                return inner_self.database.__enter__()

            def __exit__(inner_self, exc_type, exc_value, traceback):
                return inner_self.database.__exit__(exc_type, exc_value, traceback)

        def post_request():
            client = self.app.test_client()
            with client.session_transaction() as flask_session:
                flask_session['role'] = 'doctor'
                flask_session['user_id'] = 20
                flask_session['doctor_id'] = 8
            return client.post('/doctor/leave', data={
                'leave_date': '2026-10-13',
                'leave_kind': 'partial',
                'start_time': '09:15',
                'end_time': '09:45',
                'reason': 'Emergency'
            })

        with patch.object(doctor, 'date', FrozenDate), \
                patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'get_db', side_effect=ConcurrentDatabase):
            responses = []
            threads = [
                Thread(target=lambda: responses.append(post_request()))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(self.state['leave_requests']), 1)
        self.assertEqual(len(responses), 2)

    def test_doctor_leave_route_requires_doctor_and_verified_ownership(self):
        self.set_session(role='patient')
        denied = self.client.get('/doctor/leave')
        self.assertEqual(denied.status_code, 302)

        self.set_session(role='doctor')
        with patch.object(doctor, 'query_one', return_value=None), \
                patch.object(doctor, 'get_db') as database:
            unverified = self.client.get('/doctor/leave')
        self.assertEqual(unverified.status_code, 302)
        database.assert_not_called()

    def test_missing_leave_table_shows_actionable_setup_message(self):
        self.set_session()

        def query_leave_or_dashboard(sql, params=None):
            if 'FROM doctor_leave_requests' in sql:
                raise OperationalError(1146, "Table 'smart_hospital_db.doctor_leave_requests' doesn't exist")
            return []

        with patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'query_all', side_effect=query_leave_or_dashboard), \
                patch.object(self.app.logger, 'exception'):
            response = self.client.get('/doctor/leave', follow_redirects=True)

        page = response.get_data(as_text=True)
        self.assertIn('Emergency Leave database setup is incomplete.', page)
        self.assertIn('20261017_create_doctor_leave_requests.sql', page)
        self.assertIn('20261017_add_appointment_cancellation_reason.sql', page)

    def test_full_day_request_stores_no_specific_time(self):
        self.set_session()
        with patch.object(doctor, 'date', FrozenDate), \
                patch.object(doctor, 'query_one', return_value={'id': 8}), \
                patch.object(doctor, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            response = self.submit_leave(leave_kind='full_day', start_time='', end_time='')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.state['leave_requests'][0]['is_full_day'])
        self.assertIsNone(self.state['leave_requests'][0]['start_time'])
        self.assertIsNone(self.state['leave_requests'][0]['end_time'])

    def test_admin_can_reject_with_required_reason(self):
        self.set_session(role='admin', user_id=1)
        with patch.object(admin, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            response = self.client.post(
                '/admin/doctor-leaves/12/decision',
                data={'action': 'reject', 'rejection_reason': 'Insufficient coverage'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state['leave']['status'], 'rejected')
        self.assertEqual(self.state['leave']['rejection_reason'], 'Insufficient coverage')

    def test_approval_cancels_only_future_overlapping_appointments_and_notifies(self):
        self.set_session(role='admin', user_id=1)
        overlapping = self.appointment(1, '09:00:00')
        overlapping_two = self.appointment(2, '09:30:00', 'pending')
        checked_in = self.appointment(10, '09:30:00', 'checked_in')
        outside_range = self.appointment(3, '10:00:00')
        completed = self.appointment(4, '09:30:00', 'completed')
        already_cancelled = self.appointment(11, '09:30:00', 'cancelled')
        self.state['appointments'] = [
            overlapping, overlapping_two, checked_in, outside_range,
            completed, already_cancelled
        ]
        with patch.object(admin, 'datetime', FrozenDateTime), \
                patch.object(admin, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            response = self.decision()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state['leave']['status'], 'approved')
        self.assertEqual(overlapping['status'], 'cancelled')
        self.assertEqual(overlapping_two['status'], 'cancelled')
        self.assertEqual(checked_in['status'], 'cancelled')
        self.assertEqual(overlapping['cancellation_reason'], 'Cancelled due to emergency')
        self.assertEqual(outside_range['status'], 'approved')
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(already_cancelled['status'], 'cancelled')
        self.assertEqual(len(self.state['notifications']), 3)
        self.assertIn('2026-10-13 at 09:00', self.state['notifications'][0]['message'])
        self.assertIn('Cancelled due to emergency', self.state['notifications'][0]['message'])

    def test_full_day_approval_cancels_workday_bookings_not_completed(self):
        self.set_session(role='admin', user_id=1)
        self.state['leave']['is_full_day'] = True
        self.state['leave']['start_time'] = None
        self.state['leave']['end_time'] = None
        in_working_hours = self.appointment(5, '10:00:00')
        self.state['appointments'] = [
            in_working_hours, self.appointment(6, '11:00:00')
        ]
        with patch.object(admin, 'datetime', FrozenDateTime), \
                patch.object(admin, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            response = self.decision()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(in_working_hours['status'], 'cancelled')
        self.assertEqual(self.state['appointments'][1]['status'], 'approved')

    def test_approval_requires_confirmation_and_reason_when_bookings_are_affected(self):
        self.set_session(role='admin', user_id=1)
        appointment = self.appointment(7, '09:30:00')
        self.state['appointments'] = [appointment]
        with patch.object(admin, 'datetime', FrozenDateTime), \
                patch.object(admin, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            response = self.decision(confirmed='0', cancellation_reason='')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state['leave']['status'], 'pending')
        self.assertEqual(self.state['appointments'][0]['status'], 'approved')
        self.assertEqual(self.state['notifications'], [])

    def test_approval_without_affected_bookings_needs_no_cancellation_reason(self):
        self.set_session(role='admin', user_id=1)
        with patch.object(admin, 'datetime', FrozenDateTime), \
                patch.object(admin, 'get_db', return_value=AtomicLeaveDatabase(self.state)):
            response = self.decision(confirmed='0', cancellation_reason='')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state['leave']['status'], 'approved')
        self.assertIsNone(self.state['leave']['cancellation_reason'])
        self.assertEqual(self.state['notifications'], [])

    def test_approval_retry_does_not_duplicate_cancellations_or_notifications(self):
        self.set_session(role='admin', user_id=1)
        appointment = self.appointment(8, '09:30:00')
        self.state['appointments'] = [appointment]
        database = AtomicLeaveDatabase(self.state)
        with patch.object(admin, 'datetime', FrozenDateTime), \
                patch.object(admin, 'get_db', return_value=database):
            self.decision()
            retry = self.decision()

        self.assertEqual(retry.status_code, 302)
        self.assertEqual(len(self.state['notifications']), 1)
        self.assertEqual(appointment['status'], 'cancelled')

    def test_notification_failure_rolls_back_approval_and_cancellation(self):
        self.set_session(role='admin', user_id=1)
        appointment = self.appointment(9, '09:30:00')
        self.state['appointments'] = [appointment]
        with patch.object(admin, 'datetime', FrozenDateTime), \
                patch.object(admin, 'get_db', return_value=AtomicLeaveDatabase(
                    self.state, fail_notification=True
                )), patch.object(self.app.logger, 'exception') as error_log:
            response = self.decision()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state['leave']['status'], 'pending')
        self.assertEqual(self.state['appointments'][0]['status'], 'approved')
        self.assertEqual(self.state['notifications'], [])
        error_log.assert_called_once()

    def test_booking_endpoint_rejects_slot_covered_by_approved_leave(self):
        self.set_session(role='patient')
        leave = {'is_full_day': False, 'start_time': '09:15:00', 'end_time': '09:45:00'}
        database = BookingLeaveDatabase(self.schedule, [leave])
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', return_value=[
                    {'id': 8, 'full_name': 'Doctor', 'department': 'General',
                     'specialization': 'General', 'consulting_fee': 100}
                ]), patch.object(appointments, 'query_one', return_value={'id': 30}), \
                patch.object(appointments, 'get_db', return_value=database):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8', 'appointment_date': '2026-10-13',
                'appointment_time': '09:00', 'reason': 'Consultation'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('That slot is no longer available.', response.get_data(as_text=True))
        self.assertFalse(database.inserted)

    def test_slot_grid_marks_approved_leave_unavailable(self):
        slots, _ = build_slot_availability(
            [self.schedule], [], date(2026, 10, 13), datetime(2026, 10, 12),
            [{'is_full_day': False, 'start_time': '09:15:00', 'end_time': '09:45:00'}]
        )
        leave_slots = [
            slot['status'] for slot in slots
            if slot['time'] in ('09:00', '09:30')
        ]
        self.assertEqual(
            leave_slots,
            ['leave', 'leave']
        )
        self.assertFalse(is_slot_available(
            [self.schedule], [], date(2026, 10, 13), time(9, 0),
            datetime(2026, 10, 12),
            [{'is_full_day': False, 'start_time': '09:15:00', 'end_time': '09:45:00'}]
        ))

    def test_admin_review_route_rejects_non_admin(self):
        self.set_session(role='doctor')
        with patch.object(admin, 'get_db') as database:
            response = self.client.post('/admin/doctor-leaves/12/decision', data={
                'action': 'approve'
            })
        self.assertEqual(response.status_code, 302)
        database.assert_not_called()

    def test_admin_navigation_order_and_leave_link_on_overview(self):
        self.set_session(role='admin', user_id=1)

        def read_metric(sql, params=None):
            if 'doctor_leave_requests' in sql:
                return {'count': 3}
            if 'SUM(total_amount - paid_amount)' in sql:
                return {'sum': 0}
            return {'count': 0}

        with patch.object(admin, 'query_one', side_effect=read_metric), \
                patch.object(admin, 'query_all', return_value=[]):
            response = self.client.get('/admin/dashboard')

        page = response.get_data(as_text=True)
        nav = page.split('<nav class="main-nav" id="main-nav"', 1)[1].split('</nav>', 1)[0]
        expected_links = [
            '/admin/dashboard',
            '/admin/doctors',
            '/admin/staff',
            '/pharmacy/inventory',
            '/admin/reports',
            '/auth/profile',
            '/auth/logout'
        ]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [nav.index(href) for href in expected_links],
            sorted(nav.index(href) for href in expected_links)
        )
        self.assertNotIn('/admin/doctor-leaves', nav)
        self.assertNotIn('/admin/wards', nav)
        self.assertNotIn('/beds/availability', nav)
        for route in ('/admin/doctors', '/admin/staff', '/admin/reports'):
            self.assertEqual(page.count(f'href="{route}"'), 1)
        self.assertIn('3 pending doctor-leave request(s)', page)
        self.assertIn(
            '<a class="button button-quiet" href="/admin/doctor-leaves">Manage doctor leave</a>',
            page
        )
        self.assertIn('Recent appointments', page)
        self.assertIn('medicine item(s) at or below minimum stock', page)
        self.assertNotIn('Wards and beds', page)

    def test_admin_overview_shows_no_pending_leave_message(self):
        self.set_session(role='admin', user_id=1)

        def read_metric(sql, params=None):
            if 'doctor_leave_requests' in sql:
                return {'count': 0}
            if 'SUM(total_amount - paid_amount)' in sql:
                return {'sum': 0}
            return {'count': 0}

        with patch.object(admin, 'query_one', side_effect=read_metric), \
                patch.object(admin, 'query_all', return_value=[]):
            response = self.client.get('/admin/dashboard')

        self.assertEqual(response.status_code, 200)
        self.assertIn('No pending doctor-leave requests', response.get_data(as_text=True))

    def test_admin_review_page_lists_only_future_appointments_in_requested_range(self):
        self.set_session(role='admin', user_id=1)
        pending = dict(
            self.leave,
            doctor_id=8,
            doctor_name='Dr. Example',
            schedule_start='09:00:00',
            schedule_end='11:00:00',
            slot_duration_mins=30,
            submitted_at='2026-10-12 08:00:00'
        )
        appointments_for_day = [
            dict(self.appointment(20, '09:30:00'), patient_id=120),
            dict(self.appointment(21, '10:00:00'), patient_id=121)
        ]
        with patch.object(admin, 'datetime', FrozenDateTime), \
                patch.object(admin, 'query_all', side_effect=[
                    [pending], appointments_for_day
                ]):
            response = self.client.get('/admin/doctor-leaves')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Dr. Example', page)
        self.assertIn('Patient 20', page)
        self.assertNotIn('Patient 21', page)
        self.assertIn(
            '<strong>1</strong> appointment(s) for <strong>1</strong> patient(s)',
            page
        )

    def test_full_day_leave_marks_working_slots_as_leave(self):
        slots, _ = build_slot_availability(
            [self.schedule], [], date(2026, 10, 13), datetime(2026, 10, 12),
            [{'is_full_day': True, 'start_time': None, 'end_time': None}]
        )
        status_by_time = {slot['time']: slot['status'] for slot in slots}
        self.assertEqual(status_by_time['09:00'], 'leave')
        self.assertEqual(status_by_time['10:30'], 'leave')
        self.assertEqual(status_by_time['11:00'], 'outside')


class BookingLeaveCursor:
    def __init__(self, schedule, leaves):
        self.schedule = schedule
        self.leaves = leaves
        self._one = None
        self._many = []
        self.inserted = False
        self.lastrowid = 0

    def execute(self, sql, params=()):
        if 'SELECT id FROM patients' in sql:
            self._one = {'id': params[0]}
        elif 'SELECT id FROM doctors' in sql:
            self._one = {'id': params[0]}
        elif 'FROM doctor_schedules ds' in sql:
            self._many = [dict(self.schedule, doctor_user_id=99)]
        elif 'FROM appointments' in sql and 'patient_id' not in sql:
            self._many = []
        elif 'FROM doctor_leave_requests' in sql:
            self._many = self.leaves
        elif 'a.patient_id = %s' in sql:
            self._many = []
        elif 'INSERT INTO appointments' in sql:
            self.inserted = True
            self.lastrowid = 700

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class BookingLeaveDatabase:
    def __init__(self, schedule, leaves):
        self.cursor = BookingLeaveCursor(schedule, leaves)
        self.inserted = False

    def __enter__(self):
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        self.inserted = self.cursor.inserted
        return False


if __name__ == '__main__':
    unittest.main()
