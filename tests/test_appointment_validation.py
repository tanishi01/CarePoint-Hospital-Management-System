import unittest
from copy import deepcopy
from datetime import date, datetime
from threading import Barrier, Lock, Thread
from unittest.mock import patch

from app import create_app
from routes import admin, appointments, auth, doctor, patient


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 12, 8, 0, 0)


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 12)


class FakeCursor:
    def __init__(
        self, schedules=None, conflict=False, appointments=None,
        patient_appointments=None, leaves=None, locked_appointment=None
    ):
        self.schedules = schedules or []
        self.conflict = conflict
        self.appointments = appointments or (
            [{'appointment_time': '09:00:00'}] if conflict else []
        )
        self.patient_appointments = patient_appointments or []
        self.leaves = leaves or []
        self.lastrowid = 501
        self.insert_params = None
        self.update_params = None
        self.update_sql = None
        self.patient_appointments_query = None
        self.locked_appointment = locked_appointment or {
            'id': 9, 'patient_id': 77, 'status': 'approved'
        }
        self.rowcount = 0
        self._one = None

    def execute(self, sql, params=()):
        if 'SELECT id FROM patients' in sql:
            self._one = {'id': params[0]}
        elif 'SELECT id FROM doctors' in sql:
            self._one = {'id': params[0]}
        elif 'SELECT id, patient_id, status' in sql:
            self._one = self.locked_appointment
        elif 'SELECT status FROM appointments' in sql:
            self._one = {'status': 'pending'}
        elif 'a.patient_id = %s' in sql:
            self.patient_appointments_query = sql
            self._many = self.patient_appointments
        elif 'FROM doctor_schedules ds' in sql:
            self._many = self.schedules
        elif 'FROM doctor_leave_requests' in sql:
            self._many = self.leaves
        elif 'SELECT id FROM appointments' in sql:
            self._one = {'id': 99} if self.conflict else None
        elif 'FROM appointments' in sql:
            self._many = self.appointments
        elif 'INSERT INTO appointments' in sql:
            self.insert_params = params
        elif 'UPDATE appointments' in sql:
            self.update_params = params
            self.update_sql = sql
            self.rowcount = 1

    def fetchall(self):
        return self._many

    def fetchone(self):
        return self._one


class FakeDatabase:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor, None

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class ScheduleRemovalCursor:
    def __init__(self, state, fail_notification=False):
        self.state = state
        self.fail_notification = fail_notification
        self.rowcount = 0
        self._one = None
        self._many = []

    def execute(self, sql, params=()):
        self.rowcount = 0
        if 'FROM doctor_schedules ds' in sql and 'FOR UPDATE' in sql:
            schedule_id, doctor_id = params
            schedule = self.state['schedule']
            self._one = (
                dict(schedule)
                if schedule and schedule['id'] == schedule_id
                and schedule['doctor_id'] == doctor_id
                else None
            )
        elif 'FROM doctor_schedules' in sql and 'FOR UPDATE' in sql:
            schedule_id, doctor_id = params
            schedule = self.state['schedule']
            self._one = (
                dict(schedule)
                if schedule and schedule['id'] == schedule_id
                and schedule['doctor_id'] == doctor_id
                else None
            )
        elif 'FROM appointments a' in sql and 'FOR UPDATE' in sql:
            self._many = [
                appointment for appointment in self.state['appointments']
                if appointment['doctor_id'] == params[0]
                and appointment['day_of_week'] == params[1]
                and appointment['future']
                and appointment['status'] != 'cancelled'
            ]
        elif "SET status = 'cancelled', cancellation_reason = %s" in sql:
            reason, appointment_id = params
            for appointment in self.state['appointments']:
                if appointment['id'] == appointment_id and appointment['status'] != 'cancelled':
                    appointment['status'] = 'cancelled'
                    appointment['cancellation_reason'] = reason
                    self.rowcount = 1
                    break
        elif 'INSERT INTO notifications' in sql:
            if self.fail_notification:
                raise RuntimeError('notification insert failed')
            self.state['notifications'].append({
                'user_id': params[0],
                'message': params[1]
            })
            self.rowcount = 1
        elif 'DELETE FROM doctor_schedules' in sql:
            schedule_id, doctor_id = params
            schedule = self.state['schedule']
            if schedule and schedule['id'] == schedule_id and schedule['doctor_id'] == doctor_id:
                self.state['schedule'] = None
                self.rowcount = 1

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)


class AtomicScheduleRemovalDatabase:
    def __init__(self, state, fail_notification=False):
        self.state = state
        self.fail_notification = fail_notification

    def __enter__(self):
        self.snapshot = deepcopy(self.state)
        self.cursor = ScheduleRemovalCursor(self.state, self.fail_notification)
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            self.state.clear()
            self.state.update(self.snapshot)
        return False


class ConcurrentBookingCursor:
    def __init__(self, schedules, state, lock):
        self.schedules = schedules
        self.state = state
        self.lock = lock
        self.lastrowid = 500
        self.insert_params = None
        self._one = None

    def execute(self, sql, params=()):
        if 'SELECT id FROM patients' in sql:
            self.lock.acquire()
            self.state['patient_locked'] = True
            self._one = {'id': params[0]}
        elif 'a.patient_id = %s' in sql:
            _, patient_id, booking_date = params
            self._many = [
                appointment for appointment in self.state['appointments']
                if appointment['patient_id'] == patient_id
                and appointment['appointment_date'] == booking_date
                and appointment['status'] != 'cancelled'
            ]
        elif 'FROM doctor_schedules ds' in sql:
            self.state['schedule_locked'] = self.state['schedule_locked'] or 'FOR UPDATE' in sql
            self._many = self.schedules
        elif 'FROM doctor_leave_requests' in sql:
            self._many = []
        elif 'FROM appointments' in sql:
            self._many = list(self.state['appointments'])
        elif 'INSERT INTO appointments' in sql:
            self.insert_params = params
            self.state['appointments'].append({
                'patient_id': params[0],
                'doctor_id': params[1],
                'appointment_date': params[2],
                'appointment_time': params[3],
                'slot_duration_mins': self.schedules[0]['slot_duration_mins'],
                'status': 'pending'
            })

    def fetchall(self):
        return self._many

    def fetchone(self):
        return self._one


class ConcurrentBookingDatabase:
    def __init__(self, schedules, state, lock, barrier):
        self.schedules = schedules
        self.state = state
        self.lock = lock
        self.barrier = barrier

    def __enter__(self):
        self.barrier.wait(timeout=5)
        return ConcurrentBookingCursor(self.schedules, self.state, self.lock), self

    def __exit__(self, exc_type, exc_value, traceback):
        self.lock.release()
        return False


class CarePointRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY='test-only')
        self.client = self.app.test_client()
        self.future_day = date(2026, 10, 13)
        self.schedule = [{
            'start_time': '09:00:00',
            'end_time': '10:00:00',
            'break_start': None,
            'break_end': None,
            'slot_duration_mins': 30,
            'doctor_user_id': 8
        }]

    def set_session(self, role='patient', user_id=20, patient_id=None):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = role
            flask_session['user_id'] = user_id
            if patient_id is not None:
                flask_session['patient_id'] = patient_id

    def booking_page_queries(self, sql, params=None):
        if 'FROM doctors d' in sql:
            return [{'id': 8, 'full_name': 'Doctor', 'department': 'General',
                     'specialization': 'General', 'consulting_fee': 100}]
        return []

    def future_doctor_appointment(
        self, appointment_id, patient_id, patient_user_id, appointment_time
    ):
        return {
            'id': appointment_id,
            'doctor_id': 8,
            'patient_id': patient_id,
            'patient_user_id': patient_user_id,
            'patient_name': 'Patient {0}'.format(patient_id),
            'appointment_date': date(2026, 10, 19),
            'appointment_time': appointment_time,
            'day_of_week': 'Monday',
            'future': True,
            'status': 'approved',
            'reason': 'Original visit reason',
            'cancellation_reason': None
        }

    def schedule_removal_state(self, appointments=None):
        return {
            'schedule': {
                'id': 40,
                'doctor_id': 8,
                'day_of_week': 'Monday',
                'doctor_name': 'Dr. Schedule'
            },
            'appointments': list(appointments or []),
            'notifications': []
        }

    def post_schedule_removal(
        self, state, expected_appointments, expected_patients,
        reason='Cancelled due to emergency', confirmed='1', fail_notification=False
    ):
        self.set_session(role='admin', user_id=1)
        database = AtomicScheduleRemovalDatabase(state, fail_notification)
        with patch.object(admin, 'get_db', return_value=database):
            return self.client.post(
                '/admin/doctors/8/schedules/40/delete',
                data={
                    'confirmed': confirmed,
                    'expected_appointment_count': str(expected_appointments),
                    'expected_patient_count': str(expected_patients),
                    'cancellation_reason': reason
                }
            )

    def test_selecting_doctor_and_date_returns_available_slots(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=[
                    self.schedule,
                    [],
                    []
                ]):
            response = self.client.get(
                f'/appointments/available-slots?doctor_id=8&date={self.future_day.isoformat()}'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [slot for slot in response.get_json()['slots'] if slot['status'] == 'available'],
            [{'time': '09:00', 'status': 'available'}, {'time': '09:30', 'status': 'available'}]
        )
        self.assertEqual(response.get_json()['schedule']['working_hours'], {'start': '09:00', 'end': '10:00'})

    def test_already_booked_time_is_not_returned(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=[
                    self.schedule,
                    [{'appointment_time': '09:00:00'}],
                    []
                ]):
            response = self.client.get(
                f'/appointments/available-slots?doctor_id=8&date={self.future_day.isoformat()}'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [slot['time'] for slot in response.get_json()['slots'] if slot['status'] == 'available'],
            ['09:30']
        )
        self.assertEqual(
            next(slot['status'] for slot in response.get_json()['slots'] if slot['time'] == '09:00'),
            'unavailable'
        )

    def test_break_slots_and_outside_hours_are_marked_unavailable(self):
        self.set_session()
        schedule = [{
            'start_time': '09:00:00',
            'end_time': '11:00:00',
            'break_start': '09:30:00',
            'break_end': '10:00:00',
            'slot_duration_mins': 30,
            'doctor_user_id': 8
        }]
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=[schedule, [], []]):
            response = self.client.get(
                f'/appointments/available-slots?doctor_id=8&date={self.future_day.isoformat()}'
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result['schedule']['break_times'], {'start': '09:30', 'end': '10:00'})
        self.assertEqual(
            next(slot['status'] for slot in result['slots'] if slot['time'] == '09:30'),
            'break'
        )
        self.assertEqual(
            next(slot['status'] for slot in result['slots'] if slot['time'] == '08:30'),
            'outside'
        )

    def test_invalid_doctor_date_and_past_date_inputs_are_rejected(self):
        self.set_session()
        invalid_doctor = self.client.get('/appointments/available-slots?doctor_id=0&date=2026-10-13')
        invalid_date = self.client.get('/appointments/available-slots?doctor_id=8&date=2026-02-30')
        self.assertEqual(invalid_doctor.status_code, 400)
        self.assertEqual(invalid_date.status_code, 400)

        with patch.object(appointments, 'datetime', FrozenDateTime):
            past_date = self.client.get('/appointments/available-slots?doctor_id=8&date=2026-10-11')
        self.assertEqual(past_date.status_code, 400)
        self.assertIn('cannot be in the past', past_date.get_json()['error'])

    def test_unavailable_doctor_has_no_schedule(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', return_value=[]):
            response = self.client.get(
                f'/appointments/available-slots?doctor_id=999&date={self.future_day.isoformat()}'
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()['schedule'])
        self.assertEqual(response.get_json()['slots'], [])

    def test_booking_page_exposes_date_limit_and_change_handlers(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=self.booking_page_queries):
            response = self.client.get('/appointments/book')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('min="2026-10-12"', page)
        self.assertIn("doctor.addEventListener('change', loadSlots)", page)
        self.assertIn("date.addEventListener('change', loadSlots)", page)
        self.assertIn("fetch('/appointments/available-slots?'", page)
        self.assertIn('slot-grid', page)
        self.assertIn('slot-key-unavailable', page)

    def test_booking_page_preselects_requested_active_doctor_and_keeps_dropdown_changeable(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=self.booking_page_queries):
            response = self.client.get('/appointments/book?doctor_id=8')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertRegex(page, r'<option value="8"\s+selected>')
        self.assertIn('<select id="doctor_id" name="doctor_id" required>', page)
        self.assertIn("doctor.addEventListener('change', loadSlots)", page)
        self.assertIn("new URLSearchParams({doctor_id: doctor.value, date: date.value})", page)

    def test_booking_page_handles_invalid_or_inactive_requested_doctor(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=self.booking_page_queries):
            response = self.client.get('/appointments/book?doctor_id=999')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('That doctor is unavailable.', page)
        self.assertIn('<option value="8"', page)
        self.assertNotRegex(page, r'<option value="8"\s+selected>')

    def test_doctor_directory_book_link_passes_each_doctor_id(self):
        self.set_session()
        doctors = [
            {'id': 8, 'full_name': 'Dr. Shukla', 'department': 'General',
             'specialization': 'General', 'qualification': 'MBBS',
             'room_number': '1', 'consulting_fee': 100},
            {'id': 9, 'full_name': 'Dr. Other', 'department': 'General',
             'specialization': 'General', 'qualification': 'MBBS',
             'room_number': '2', 'consulting_fee': 120}
        ]

        def directory_query(sql, params=None):
            if 'FROM doctors d' in sql:
                return doctors
            return []

        with patch.object(patient, 'query_all', side_effect=directory_query):
            response = self.client.get('/patient/doctors')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('/appointments/book?doctor_id=8', page)
        self.assertIn('/appointments/book?doctor_id=9', page)

    def test_booking_post_rejects_inactive_or_invalid_doctor_id(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=self.booking_page_queries), \
                patch.object(appointments, 'query_one') as patient_lookup, \
                patch.object(appointments, 'get_db') as database:
            response = self.client.post('/appointments/book', data={
                'doctor_id': '999',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('Select a valid active doctor.', response.get_data(as_text=True))
        patient_lookup.assert_not_called()
        database.assert_not_called()

    def test_date_without_schedule_shows_helpful_message(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', return_value=[]):
            response = self.client.get(
                f'/appointments/available-slots?doctor_id=8&date={self.future_day.isoformat()}'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['slots'], [])
        self.assertIn('no availability', response.get_json()['message'].lower())

    def test_past_appointment_date_is_rejected(self):
        self.set_session()
        with patch.object(appointments, 'query_all', side_effect=self.booking_page_queries):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': '2026-10-11',
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('Appointment date cannot be in the past.', response.get_data(as_text=True))

    def test_malformed_appointment_date_is_rejected(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=self.booking_page_queries):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': '2026-02-30',
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('Enter a valid appointment date.', response.get_data(as_text=True))

    def test_past_time_today_is_rejected(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=self.booking_page_queries):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': '2026-10-12',
                'appointment_time': '07:30',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('Appointment time must be in the future.', response.get_data(as_text=True))

    def test_valid_booking_uses_patient_mapping_and_doctor_id(self):
        self.set_session(patient_id=999)
        cursor = FakeCursor(schedules=self.schedule)

        def lookup_patient(sql, params=None):
            if 'FROM doctors d' in sql:
                return self.booking_page_queries(sql, params)
            if 'FROM patients p' in sql:
                return {'id': 77}
            return None

        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=lookup_patient), \
                patch.object(appointments, 'query_one', side_effect=lookup_patient), \
                patch.object(appointments, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(appointments, 'create_notification'):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            cursor.insert_params,
            (77, 8, self.future_day, '09:00:00', 'Checkup')
        )

    def test_duplicate_booking_is_rejected(self):
        self.set_session()
        cursor = FakeCursor(schedules=self.schedule, conflict=True)

        def lookup(sql, params=None):
            if 'FROM doctors d' in sql:
                return self.booking_page_queries(sql, params)
            return {'id': 77}

        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=lookup), \
                patch.object(appointments, 'query_one', side_effect=lookup), \
                patch.object(appointments, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('no longer available', response.get_data(as_text=True))
        self.assertIsNone(cursor.insert_params)

    def test_overlapping_patient_appointment_with_different_doctor_is_rejected(self):
        self.set_session()
        cursor = FakeCursor(
            schedules=self.schedule,
            patient_appointments=[{
                'appointment_time': '09:15:00',
                'slot_duration_mins': 30,
                'status': 'approved',
                'doctor_id': 9
            }]
        )

        def lookup(sql, params=None):
            if 'FROM doctors d' in sql:
                return self.booking_page_queries(sql, params)
            return {'id': 77}

        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=lookup), \
                patch.object(appointments, 'query_one', side_effect=lookup), \
                patch.object(appointments, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('overlaps this time', response.get_data(as_text=True))
        self.assertIsNone(cursor.insert_params)

    def test_non_overlapping_patient_appointment_is_allowed(self):
        self.set_session()
        cursor = FakeCursor(
            schedules=self.schedule,
            patient_appointments=[{
                'appointment_time': '09:30:00',
                'slot_duration_mins': 30,
                'status': 'approved',
                'doctor_id': 9
            }]
        )

        def lookup(sql, params=None):
            if 'FROM doctors d' in sql:
                return self.booking_page_queries(sql, params)
            return {'id': 77}

        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=lookup), \
                patch.object(appointments, 'query_one', side_effect=lookup), \
                patch.object(appointments, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(appointments, 'create_notification'):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(cursor.insert_params)

    def test_cancelled_patient_appointment_does_not_block_booking(self):
        self.set_session()
        cursor = FakeCursor(
            schedules=self.schedule,
            patient_appointments=[{
                'appointment_time': '09:00:00',
                'slot_duration_mins': 30,
                'status': 'cancelled',
                'doctor_id': 9
            }]
        )

        def lookup(sql, params=None):
            if 'FROM doctors d' in sql:
                return self.booking_page_queries(sql, params)
            return {'id': 77}

        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=lookup), \
                patch.object(appointments, 'query_one', side_effect=lookup), \
                patch.object(appointments, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(appointments, 'create_notification'):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(cursor.insert_params)
        self.assertIn("a.status != 'cancelled'", cursor.patient_appointments_query)

    def test_two_concurrent_requests_cannot_book_the_same_slot(self):
        state = {
            'appointments': [],
            'patient_locked': False,
            'schedule_locked': False
        }
        lock = Lock()
        barrier = Barrier(2)
        clients = [self.app.test_client(), self.app.test_client()]
        for client in clients:
            with client.session_transaction() as flask_session:
                flask_session['role'] = 'patient'
                flask_session['user_id'] = 20

        def lookup(sql, params=None):
            if 'FROM doctors d' in sql:
                return self.booking_page_queries(sql, params)
            return {'id': 77}

        responses = [None, None]

        def submit(index):
            responses[index] = clients[index].post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=lookup), \
                patch.object(appointments, 'query_one', side_effect=lookup), \
                patch.object(
                    appointments, 'get_db',
                    side_effect=lambda: ConcurrentBookingDatabase(
                        self.schedule, state, lock, barrier
                    )
                ), \
                patch.object(appointments, 'create_notification'):
            threads = [Thread(target=submit, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(state['appointments']), 1)
        self.assertTrue(state['patient_locked'])
        self.assertTrue(state['schedule_locked'])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 302])

    def test_time_outside_doctor_schedule_is_rejected(self):
        self.set_session()
        cursor = FakeCursor(schedules=self.schedule)

        def lookup(sql, params=None):
            if 'FROM doctors d' in sql:
                return self.booking_page_queries(sql, params)
            return {'id': 77}

        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=lookup), \
                patch.object(appointments, 'query_one', side_effect=lookup), \
                patch.object(appointments, 'get_db', return_value=FakeDatabase(cursor)):
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:15',
                'reason': 'Checkup'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('slot is no longer available', response.get_data(as_text=True).lower())
        self.assertIsNone(cursor.insert_params)

    def test_doctor_reschedule_validates_and_updates_slot(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'doctor'
            flask_session['user_id'] = 8
            flask_session['doctor_id'] = 8
        cursor = FakeCursor(schedules=self.schedule, locked_appointment={
            'id': 9, 'patient_id': 77, 'status': 'approved'
        })
        appointment = {
            'id': 9,
            'doctor_id': 8,
            'patient_id': 77,
            'patient_user_id': 20,
            'appointment_date': date(2026, 10, 12),
            'appointment_time': '09:00:00'
        }
        with patch.object(doctor, 'datetime', FrozenDateTime), \
                patch.object(doctor, 'query_one', return_value=appointment), \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(doctor, 'create_notification'):
            response = self.client.post('/doctor/appointments/9/action', data={
                'action': 'reschedule',
                'new_date': self.future_day.isoformat(),
                'new_time': '09:00'
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            cursor.update_params,
            (self.future_day, '09:00:00', 9, 8)
        )

    def test_doctor_reschedule_rejects_approved_leave(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'doctor'
            flask_session['user_id'] = 8
            flask_session['doctor_id'] = 8
        cursor = FakeCursor(
            schedules=self.schedule,
            leaves=[{
                'is_full_day': False,
                'start_time': '09:00:00',
                'end_time': '09:30:00'
            }]
        )
        appointment = {
            'id': 9,
            'doctor_id': 8,
            'patient_id': 77,
            'patient_user_id': 20,
            'appointment_date': date(2026, 10, 12),
            'appointment_time': '09:00:00'
        }
        with patch.object(doctor, 'datetime', FrozenDateTime), \
                patch.object(doctor, 'query_one', return_value=appointment), \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(doctor, 'create_notification'):
            response = self.client.post('/doctor/appointments/9/action', data={
                'action': 'reschedule',
                'new_date': self.future_day.isoformat(),
                'new_time': '09:00'
            })

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(cursor.update_params)

    def test_doctor_cancel_button_is_visible_only_for_eligible_statuses(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'doctor'
            flask_session['user_id'] = 8
            flask_session['doctor_id'] = 8
        appointments_for_page = [
            {'id': 1, 'appointment_date': self.future_day, 'appointment_time': '09:00',
             'patient_id': 70, 'patient_name': 'Pending Patient', 'age': 30,
             'gender': 'Other', 'blood_group': 'O+', 'phone': '1234567890',
             'reason': 'Checkup', 'status': 'pending'},
            {'id': 2, 'appointment_date': self.future_day, 'appointment_time': '09:30',
             'patient_id': 71, 'patient_name': 'Approved Patient', 'age': 30,
             'gender': 'Other', 'blood_group': 'O+', 'phone': '1234567890',
             'reason': 'Checkup', 'status': 'approved'},
            {'id': 3, 'appointment_date': self.future_day, 'appointment_time': '10:00',
             'patient_id': 72, 'patient_name': 'Completed Patient', 'age': 30,
             'gender': 'Other', 'blood_group': 'O+', 'phone': '1234567890',
             'reason': 'Checkup', 'status': 'completed'},
            {'id': 4, 'appointment_date': self.future_day, 'appointment_time': '10:30',
             'patient_id': 73, 'patient_name': 'Cancelled Patient', 'age': 30,
             'gender': 'Other', 'blood_group': 'O+', 'phone': '1234567890',
             'reason': 'Checkup', 'status': 'cancelled'}
        ]
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'query_all', return_value=appointments_for_page):
            response = self.client.get('/doctor/appointments')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertEqual(page.count('doctor-cancel-button'), 2)
        self.assertEqual(page.count('value="approve"'), 1)
        self.assertIn("window.confirm('Cancel this appointment?')", page)
        self.assertIn('No status actions', page)

    def test_doctor_cancel_updates_only_owned_eligible_appointment_and_notifies_patient(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'doctor'
            flask_session['user_id'] = 8
            flask_session['doctor_id'] = 8
        appointment = {
            'id': 9,
            'doctor_id': 8,
            'patient_id': 77,
            'patient_user_id': 20,
            'appointment_date': self.future_day,
            'appointment_time': '09:00:00',
            'status': 'approved',
            'reason': 'Original reason',
            'cancellation_reason': None
        }
        cursor = FakeCursor()
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'query_one', return_value=appointment) as lookup, \
                patch.object(doctor, 'get_db', return_value=FakeDatabase(cursor)), \
                patch.object(doctor, 'create_notification') as notification:
            response = self.client.post(
                '/doctor/appointments/9/action',
                data={'action': 'cancel'}
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('WHERE id = %s AND doctor_id = %s', cursor.update_sql)
        self.assertEqual(cursor.update_params, (9, 8))
        self.assertIn("SET status = 'cancelled'", cursor.update_sql)
        self.assertNotIn('reason =', cursor.update_sql)
        self.assertEqual(lookup.call_args.args[1], (9, 8))
        notification.assert_called_once()
        self.assertEqual(notification.call_args.args[0], 20)
        self.assertIn('CANCELLED', notification.call_args.args[1])

    def test_doctor_cannot_cancel_appointment_outside_its_doctor_scope(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'doctor'
            flask_session['user_id'] = 9
            flask_session['doctor_id'] = 9
        with patch.object(doctor, 'get_current_doctor_id', return_value=9), \
                patch.object(doctor, 'query_one', return_value=None) as lookup, \
                patch.object(doctor, 'get_db') as update, \
                patch.object(doctor, 'create_notification') as notification:
            response = self.client.post(
                '/doctor/appointments/9/action',
                data={'action': 'cancel'}
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(lookup.call_args.args[1], (9, 9))
        update.assert_not_called()
        notification.assert_not_called()

    def test_doctor_cannot_cancel_appointment_after_eligible_statuses(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'doctor'
            flask_session['user_id'] = 8
            flask_session['doctor_id'] = 8
        appointment = {
            'id': 9,
            'patient_user_id': 20,
            'status': 'completed',
            'appointment_date': self.future_day,
            'appointment_time': '09:00:00'
        }
        with patch.object(doctor, 'get_current_doctor_id', return_value=8), \
                patch.object(doctor, 'query_one', return_value=appointment), \
                patch.object(doctor, 'get_db') as update, \
                patch.object(doctor, 'create_notification') as notification:
            response = self.client.post(
                '/doctor/appointments/9/action',
                data={'action': 'cancel'}
            )

        self.assertEqual(response.status_code, 302)
        update.assert_not_called()
        notification.assert_not_called()

    def test_patient_cannot_access_doctor_cancellation_route(self):
        self.set_session(role='patient')
        with patch.object(doctor, 'query_one') as lookup, \
                patch.object(doctor, 'execute_dml') as update:
            response = self.client.post(
                '/doctor/appointments/9/action',
                data={'action': 'cancel'}
            )

        self.assertEqual(response.status_code, 302)
        lookup.assert_not_called()
        update.assert_not_called()

    def test_doctor_reschedule_date_input_has_minimum(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'doctor'
            flask_session['user_id'] = 8
            flask_session['doctor_id'] = 8
        appointment = {
            'id': 9,
            'appointment_id': 9,
            'patient_id': 77,
            'appointment_date': self.future_day,
            'appointment_time': '09:00:00',
            'patient_name': 'Patient',
            'age': 30,
            'gender': 'Other',
            'blood_group': 'O+',
            'phone': '1234567890',
            'reason': 'Checkup',
            'status': 'pending'
        }
        with patch.object(doctor, 'date', FrozenDate), \
                patch.object(doctor, 'query_all', return_value=[appointment]):
            response = self.client.get('/doctor/appointments')

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="new_date" type="date" min="2026-10-12"', response.get_data(as_text=True))

    def test_admin_rejects_invalid_doctor_schedule(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'admin'
            flask_session['user_id'] = 1
        doctor_record = {
            'id': 8,
            'full_name': 'Doctor',
            'department': 'General',
            'specialization': 'General',
            'email': 'doctor@example.test'
        }
        with patch.object(admin, 'query_one', return_value=doctor_record), \
                patch.object(admin, 'query_all', return_value=[]), \
                patch.object(admin, 'execute_dml') as write_schedule:
            response = self.client.post('/admin/doctors/8/schedules', data={
                'day_of_week': 'Monday',
                'start_time': '10:00',
                'end_time': '09:00',
                'slot_duration_mins': '0'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('start time before the end time', response.get_data(as_text=True))
        write_schedule.assert_not_called()

    def test_admin_schedule_availability_matches_selected_date_and_appointment_records(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'admin'
            flask_session['user_id'] = 1
        appointments_for_date = [
            {
                'id': 101,
                'appointment_time': '09:00:00',
                'status': 'pending',
                'patient_name': 'Asha Patient'
            },
            {
                'id': 102,
                'appointment_time': '09:30:00',
                'status': 'cancelled',
                'patient_name': 'Ravi Patient'
            },
            {
                'id': 103,
                'appointment_time': '10:30:00',
                'status': 'checked_in',
                'patient_name': 'Mina Patient'
            },
            {
                'id': 104,
                'appointment_time': '11:00:00',
                'status': 'completed',
                'patient_name': 'Dev Patient'
            }
        ]
        schedule = [{
            'start_time': '09:00:00',
            'end_time': '12:00:00',
            'break_start': '10:00:00',
            'break_end': '10:30:00',
            'slot_duration_mins': 30
        }]
        with patch.object(
                admin, 'query_all',
                side_effect=[schedule, appointments_for_date, []]
        ) as read_records:
            response = self.client.get(
                '/admin/doctors/8/schedule-availability?date=2026-10-13'
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result['date'], '2026-10-13')
        self.assertEqual(
            result['schedule']['working_hours'],
            {'start': '09:00', 'end': '12:00'}
        )
        self.assertEqual(
            result['schedule']['break_times'],
            {'start': '10:00', 'end': '10:30'}
        )
        self.assertEqual(
            read_records.call_args_list[0].args[1],
            (8, 'Tuesday')
        )
        self.assertEqual(
            read_records.call_args_list[1].args[1],
            (8, date(2026, 10, 13))
        )
        booked_slot = next(slot for slot in result['slots'] if slot['time'] == '09:00')
        self.assertEqual(booked_slot['availability'], 'booked')
        self.assertEqual(booked_slot['appointments'], [{
            'patient_name': 'Asha Patient',
            'time': '09:00',
            'status': 'pending',
            'status_label': 'Pending'
        }])
        cancelled_slot = next(slot for slot in result['slots'] if slot['time'] == '09:30')
        self.assertEqual(cancelled_slot['availability'], 'available')
        self.assertEqual(cancelled_slot['appointments'][0]['patient_name'], 'Ravi Patient')
        self.assertEqual(cancelled_slot['appointments'][0]['status'], 'cancelled')
        in_progress_slot = next(slot for slot in result['slots'] if slot['time'] == '10:30')
        self.assertEqual(in_progress_slot['availability'], 'booked')
        self.assertEqual(in_progress_slot['appointments'][0]['status'], 'in_progress')
        completed_slot = next(slot for slot in result['slots'] if slot['time'] == '11:00')
        self.assertEqual(completed_slot['appointments'][0]['status'], 'completed')

    def test_admin_schedule_availability_rejects_non_admin_without_patient_data(self):
        self.set_session(role='patient')
        with patch.object(admin, 'query_all') as read_records:
            response = self.client.get(
                '/admin/doctors/8/schedule-availability?date=2026-10-13'
            )

        self.assertEqual(response.status_code, 302)
        read_records.assert_not_called()

    def test_admin_schedule_page_renders_daily_date_grid(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'admin'
            flask_session['user_id'] = 1
        doctor_record = {
            'id': 8,
            'full_name': 'Doctor',
            'department': 'General',
            'specialization': 'General'
        }
        with patch.object(admin, 'query_one', return_value=doctor_record), \
                patch.object(admin, 'query_all', return_value=[]):
            response = self.client.get('/admin/doctors/8/schedules')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('id="schedule-date"', page)
        self.assertIn('id="daily-slot-grid"', page)
        self.assertIn('/admin/doctors/8/schedule-availability', page)
        self.assertIn('In progress', page)
        self.assertIn('Cancelled', page)

    def test_schedule_page_shows_future_appointment_impact_before_removal(self):
        with self.client.session_transaction() as flask_session:
            flask_session['role'] = 'admin'
            flask_session['user_id'] = 1
        doctor_record = {
            'id': 8,
            'full_name': 'Doctor',
            'department': 'General',
            'specialization': 'General'
        }
        schedules = [{
            'id': 40,
            'day_of_week': 'Monday',
            'start_time': '09:00:00',
            'end_time': '12:00:00',
            'break_start': None,
            'break_end': None,
            'slot_duration_mins': 30
        }]
        impacts = [{
            'day_of_week': 'Monday',
            'appointment_count': 2,
            'patient_count': 1
        }]
        with patch.object(admin, 'query_one', return_value=doctor_record), \
                patch.object(admin, 'query_all', side_effect=[schedules, impacts]):
            response = self.client.get('/admin/doctors/8/schedules')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('2 future appointment(s) for', page)
        self.assertIn('value="2"', page)
        self.assertIn('value="1"', page)
        self.assertIn('value="Cancelled due to emergency"', page)
        self.assertIn('window.confirm(message)', page)
        self.assertIn('schedule-remove-button', page)

    def test_removing_schedule_day_without_future_appointments(self):
        state = self.schedule_removal_state()
        response = self.post_schedule_removal(state, 0, 0)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(state['schedule'])
        self.assertEqual(state['appointments'], [])
        self.assertEqual(state['notifications'], [])

    def test_removing_schedule_day_cancels_one_and_notifies_patient(self):
        appointment = self.future_doctor_appointment(51, 71, 91, '09:30:00')
        state = self.schedule_removal_state([appointment])

        response = self.post_schedule_removal(state, 1, 1)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(state['schedule'])
        self.assertEqual(appointment['status'], 'cancelled')
        self.assertEqual(
            appointment['cancellation_reason'],
            'Cancelled due to emergency'
        )
        self.assertEqual(appointment['reason'], 'Original visit reason')
        self.assertEqual(len(state['notifications']), 1)
        self.assertEqual(state['notifications'][0]['user_id'], 91)
        self.assertIn('Dr. Schedule', state['notifications'][0]['message'])
        self.assertIn('2026-10-19 at 09:30', state['notifications'][0]['message'])
        self.assertIn('Cancelled due to emergency', state['notifications'][0]['message'])

        retry = self.post_schedule_removal(state, 1, 1)
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(len(state['notifications']), 1)

    def test_removing_schedule_day_cancels_multiple_but_preserves_unrelated_appointments(self):
        affected_first = self.future_doctor_appointment(61, 71, 91, '09:00:00')
        affected_second = self.future_doctor_appointment(62, 71, 91, '10:00:00')
        affected_third = self.future_doctor_appointment(63, 72, 92, '11:00:00')
        other_weekday = self.future_doctor_appointment(64, 73, 93, '12:00:00')
        other_weekday['day_of_week'] = 'Tuesday'
        past_appointment = self.future_doctor_appointment(65, 74, 94, '13:00:00')
        past_appointment['future'] = False
        already_cancelled = self.future_doctor_appointment(66, 75, 95, '14:00:00')
        already_cancelled['status'] = 'cancelled'
        state = self.schedule_removal_state([
            affected_first, affected_second, affected_third, other_weekday,
            past_appointment, already_cancelled
        ])

        response = self.post_schedule_removal(state, 3, 2)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(state['schedule'])
        for appointment in (affected_first, affected_second, affected_third):
            self.assertEqual(appointment['status'], 'cancelled')
            self.assertEqual(
                appointment['cancellation_reason'],
                'Cancelled due to emergency'
            )
        self.assertEqual(other_weekday['status'], 'approved')
        self.assertEqual(past_appointment['status'], 'approved')
        self.assertEqual(already_cancelled['status'], 'cancelled')
        self.assertEqual(len(state['notifications']), 3)
        self.assertEqual(
            sorted(notification['user_id'] for notification in state['notifications']),
            [91, 91, 92]
        )

    def test_removal_requires_confirmation_and_reason_when_appointments_exist(self):
        appointment = self.future_doctor_appointment(71, 81, 101, '09:30:00')
        state = self.schedule_removal_state([appointment])
        unconfirmed = self.post_schedule_removal(
            state, 1, 1, confirmed='0'
        )
        self.assertEqual(unconfirmed.status_code, 302)
        self.assertIsNotNone(state['schedule'])
        self.assertEqual(appointment['status'], 'approved')

        missing_reason = self.post_schedule_removal(state, 1, 1, reason='   ')
        self.assertEqual(missing_reason.status_code, 302)
        self.assertIsNotNone(state['schedule'])
        self.assertEqual(appointment['status'], 'approved')
        self.assertEqual(state['notifications'], [])

    def test_notification_failure_rolls_back_cancellations_and_schedule_removal(self):
        appointment = self.future_doctor_appointment(81, 91, 111, '09:30:00')
        state = self.schedule_removal_state([appointment])
        self.set_session(role='admin', user_id=1)
        database = AtomicScheduleRemovalDatabase(state, fail_notification=True)
        with patch.object(admin, 'get_db', return_value=database), \
                patch.object(self.app.logger, 'exception') as error_log:
            response = self.client.post(
                '/admin/doctors/8/schedules/40/delete',
                data={
                    'confirmed': '1',
                    'expected_appointment_count': '1',
                    'expected_patient_count': '1',
                    'cancellation_reason': 'Cancelled due to emergency'
                }
            )

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(state['schedule'])
        self.assertEqual(state['appointments'][0]['status'], 'approved')
        self.assertIsNone(state['appointments'][0]['cancellation_reason'])
        self.assertEqual(state['notifications'], [])
        error_log.assert_called_once()

    def test_schedule_removal_rejects_unauthorised_access(self):
        self.set_session(role='patient')
        state = self.schedule_removal_state()
        with patch.object(
                admin, 'get_db',
                return_value=AtomicScheduleRemovalDatabase(state)
        ) as database:
            response = self.client.post(
                '/admin/doctors/8/schedules/40/delete',
                data={'confirmed': '1'}
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(state['schedule'])
        database.assert_not_called()

    def test_cancelled_appointment_is_visible_to_patient_with_original_details(self):
        appointment = {
            'appointment_date': self.future_day,
            'appointment_time': '09:30:00',
            'doctor_name': 'Dr. Schedule',
            'specialization': 'General',
            'department': 'General',
            'room_number': 'Room 4',
            'reason': 'Original visit reason',
            'status': 'cancelled',
            'cancellation_reason': 'Cancelled due to emergency'
        }
        self.set_session(role='patient')
        with patch.object(patient, 'get_current_patient_id', return_value=71), \
                patch.object(patient, 'query_all', return_value=[appointment]):
            response = self.client.get('/patient/appointments')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Dr. Schedule', page)
        self.assertIn('2026-10-13', page)
        self.assertIn('09:30:00', page)
        self.assertIn('Cancelled', page)
        self.assertIn('Original visit reason', page)
        self.assertIn('<th>Visit reason</th><th>Cancellation reason</th>', page)
        self.assertIn('Cancelled due to emergency', page)

    def test_cancelled_appointment_no_longer_blocks_availability(self):
        self.set_session(role='patient')
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=[
                    self.schedule,
                    [],
                    []
                ]) as availability_queries:
            response = self.client.get(
                '/appointments/available-slots?doctor_id=8&date=2026-10-13'
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            {'time': '09:00', 'status': 'available'},
            response.get_json()['slots']
        )
        self.assertIn("status != 'cancelled'", availability_queries.call_args_list[1].args[0])

    def test_database_error_is_reported_without_exposing_details(self):
        self.set_session()
        with patch.object(appointments, 'datetime', FrozenDateTime), \
                patch.object(appointments, 'query_all', side_effect=self.booking_page_queries), \
                patch.object(appointments, 'query_one', return_value={'id': 77}), \
                patch.object(appointments, 'get_db', side_effect=RuntimeError('private-db-detail')), \
                patch.object(self.app.logger, 'exception') as error_log:
            response = self.client.post('/appointments/book', data={
                'doctor_id': '8',
                'appointment_date': self.future_day.isoformat(),
                'appointment_time': '09:00',
                'reason': 'Checkup'
            })

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Booking failed because of a database or availability error.', page)
        self.assertNotIn('private-db-detail', page)
        error_log.assert_called_once()

    def test_future_date_of_birth_is_rejected(self):
        registration = {
            'username': 'newpatient',
            'password': 'Validpass1!',
            'full_name': 'New Patient',
            'email': 'new@gmail.com',
            'phone': '9876543210',
            'dob': '2026-10-13',
            'gender': 'Other',
            'blood_group': 'O+',
            'address': 'Test address',
            'emergency_contact': '9876543210'
        }
        with patch.object(auth, 'date', FrozenDate), \
                patch.object(auth, 'get_db') as database:
            response = self.client.post('/auth/register', data=registration)

        self.assertEqual(response.status_code, 200)
        self.assertIn('Date of birth cannot be in the future.', response.get_data(as_text=True))
        database.assert_not_called()

    def test_impossible_date_of_birth_is_rejected(self):
        registration = {
            'username': 'newpatient',
            'password': 'Validpass1!',
            'full_name': 'New Patient',
            'email': 'new@gmail.com',
            'phone': '9876543210',
            'dob': '2025-02-30',
            'gender': 'Other',
            'blood_group': 'O+',
            'address': 'Test address',
            'emergency_contact': '9876543210'
        }
        with patch.object(auth, 'date', FrozenDate), \
                patch.object(auth, 'get_db') as database:
            response = self.client.post('/auth/register', data=registration)

        self.assertEqual(response.status_code, 200)
        self.assertIn('Enter a valid date of birth', response.get_data(as_text=True))
        database.assert_not_called()

    def test_dob_form_has_maximum_of_today(self):
        with patch.object(auth, 'date', FrozenDate):
            response = self.client.get('/auth/register')

        self.assertEqual(response.status_code, 200)
        self.assertIn('max="2026-10-12"', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
