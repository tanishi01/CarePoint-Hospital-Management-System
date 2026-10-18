import unittest
from copy import deepcopy
from pathlib import Path
from threading import Lock, Thread
from unittest.mock import MagicMock, patch

from app import create_app
import database
from routes import admin, beds, receptionist


class BedAllocationCursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self.lastrowid = 100
        self._one = None
        self._many = []
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if 'FROM patients p' in sql and 'FOR UPDATE' in sql:
            patient = self.state['patients'].get(params[0])
            self._one = dict(patient, id=params[0]) if patient else None
        elif 'FROM bed_allocations' in sql and 'patient_id = %s' in sql:
            self._many = [
                {'id': allocation['id']}
                for allocation in self.state['allocations']
                if allocation['patient_id'] == params[0]
                and allocation['status'] == 'admitted'
            ]
        elif 'SELECT bed_id' in sql and 'WHERE id = %s' in sql:
            allocation = next((
                allocation for allocation in self.state['allocations']
                if allocation['id'] == params[0]
                and allocation['status'] == 'admitted'
            ), None)
            self._one = {'bed_id': allocation['bed_id']} if allocation else None
        elif 'SELECT id, bed_id' in sql and 'WHERE id = %s' in sql:
            allocation_id, bed_id = params
            allocation = next((
                allocation for allocation in self.state['allocations']
                if allocation['id'] == allocation_id
                and allocation['bed_id'] == bed_id
                and allocation['status'] == 'admitted'
            ), None)
            self._one = (
                {'id': allocation_id, 'bed_id': bed_id}
                if allocation else None
            )
        elif 'FROM beds WHERE id' in sql:
            bed = self.state['beds'].get(params[0])
            self._one = dict(bed, id=params[0]) if bed else None
        elif 'FROM bed_allocations' in sql and 'bed_id = %s' in sql:
            self._many = [
                {'id': allocation['id']}
                for allocation in self.state['allocations']
                if allocation['bed_id'] == params[0]
                and allocation['status'] == 'admitted'
            ]
        elif "UPDATE bed_allocations" in sql:
            allocation_id, bed_id = params
            allocation = next((
                allocation for allocation in self.state['allocations']
                if allocation['id'] == allocation_id
                and allocation['bed_id'] == bed_id
                and allocation['status'] == 'admitted'
            ), None)
            if allocation:
                allocation['status'] = 'discharged'
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif 'INSERT INTO bed_allocations' in sql:
            bed_id, patient_id, daily_charge = params
            self.state['allocations'].append({
                'id': self.lastrowid,
                'bed_id': bed_id,
                'patient_id': patient_id,
                'daily_charge': daily_charge,
                'status': 'admitted',
            })
            self.rowcount = 1
        elif "UPDATE beds SET status = 'occupied'" in sql:
            bed = self.state['beds'].get(params[0])
            if bed and bed['status'] == 'available':
                bed['status'] = 'occupied'
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif "UPDATE beds SET status = 'available'" in sql:
            bed_id = params[0]
            bed = self.state['beds'].get(bed_id)
            if bed and bed['status'] == 'occupied':
                bed['status'] = 'available'
                self.rowcount = 1
            else:
                self.rowcount = 0

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)


class BedAllocationDatabase:
    def __init__(self, state, lock=None):
        self.state = state
        self.lock = lock

    def __enter__(self):
        if self.lock:
            self.lock.acquire()
        self.snapshot = deepcopy(self.state)
        self.cursor = BedAllocationCursor(self.state)
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            self.state.clear()
            self.state.update(self.snapshot)
        if self.lock:
            self.lock.release()
        return False


class RoomScheduleCursor:
    def __init__(self, peer_schedules=None, own_schedules=None):
        self.peer_schedules = peer_schedules or []
        self.own_schedules = own_schedules or []
        self._one = None
        self._many = []
        self.rowcount = 1
        self.lastrowid = 301
        self.calls = []
        self.schedule_inserted = False
        self.user_inserted = False
        self.doctor_inserted = False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if 'GET_LOCK' in sql:
            self._one = {'acquired': 1}
        elif 'RELEASE_LOCK' in sql:
            self._one = {'released': 1}
        elif 'SELECT room_number FROM doctors' in sql:
            self._one = {'room_number': 'R-1', 'status': 'active'}
        elif 'SELECT id FROM doctors WHERE id = %s FOR UPDATE' in sql:
            self._one = {'id': params[0]}
        elif 'SELECT day_of_week, start_time, end_time, break_start, break_end' in sql:
            self._many = self.own_schedules
        elif 'SELECT d.id AS doctor_id' in sql:
            self._many = self.peer_schedules
        elif 'INSERT INTO users' in sql:
            self.user_inserted = True
            self.lastrowid = 101
        elif 'INSERT INTO doctors' in sql:
            self.doctor_inserted = True
            self.lastrowid = 202
        elif 'INSERT INTO doctor_schedules' in sql:
            self.schedule_inserted = True

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)


class DoctorStatusCursor:
    def __init__(self, doctor, schedules=None, peer_schedules=None):
        self.doctor = doctor
        self.schedules = schedules or []
        self.peer_schedules = peer_schedules or []
        self._one = None
        self._many = []
        self.rowcount = 1
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if 'GET_LOCK' in sql:
            self._one = {'acquired': 1}
        elif 'RELEASE_LOCK' in sql:
            self._one = {'released': 1}
        elif 'SELECT d.id, d.status, d.user_id, d.room_number' in sql:
            self._one = self.doctor
        elif 'SELECT day_of_week, start_time, end_time, break_start, break_end' in sql:
            self._many = self.schedules
        elif 'SELECT d.id AS doctor_id' in sql:
            self._many = self.peer_schedules

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)


class CursorDatabase:
    def __init__(self, cursor):
        self.cursor = cursor
        self.committed = False

    def __enter__(self):
        return self.cursor, self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False


class AdvisoryLockCursor:
    def __init__(self, events):
        self.events = events
        self._one = None

    def execute(self, sql, params=()):
        if 'GET_LOCK' in sql:
            self.events.append('lock')
            self._one = {'acquired': 1}
        elif 'RELEASE_LOCK' in sql:
            self.events.append('release')
            self._one = {'released': 1}
        else:
            self.events.append('write')

    def fetchone(self):
        return self._one

    def close(self):
        self.events.append('cursor-close')


class AdvisoryLockConnection:
    def __init__(self, events):
        self.events = events
        self.cursor_instance = AdvisoryLockCursor(events)

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.events.append('commit')

    def rollback(self):
        self.events.append('rollback')

    def close(self):
        self.events.append('connection-close')


class BedAndRoomSafeguardTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY='test-only')
        self.state = {
            'patients': {11: {'user_status': 'active'}, 12: {'user_status': 'active'}},
            'beds': {
                21: {'status': 'available'},
                22: {'status': 'available'},
                23: {'status': 'occupied'},
            },
            'allocations': [],
        }

    def client_for(self, role='admin', user_id=1):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['role'] = role
            session['user_id'] = user_id
        return client

    def peer_schedule(self, start='09:00:00', end='12:00:00',
                      break_start=None, break_end=None):
        return {
            'doctor_id': 9,
            'doctor_name': 'Another Doctor',
            'day_of_week': 'Monday',
            'start_time': start,
            'end_time': end,
            'break_start': break_start,
            'break_end': break_end,
        }

    def test_allocates_available_bed_and_uses_row_locks_in_transaction(self):
        cursor = BedAllocationCursor(self.state)
        database = BedAllocationDatabase(self.state)
        with patch.object(beds, 'get_db', return_value=database):
            response = self.client_for('receptionist').post('/beds/allocate', data={
                'bed_id': '21', 'patient_id': '11', 'daily_charge': '250.00'
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(database.cursor.statements)
        self.assertEqual(len(self.state['allocations']), 1)
        self.assertEqual(self.state['allocations'][0]['patient_id'], 11)
        self.assertEqual(self.state['beds'][21]['status'], 'occupied')
        self.assertTrue(any(
            'FOR UPDATE' in sql for sql, _ in database.cursor.statements
        ))
        self.assertIsNotNone(cursor)

    def test_patient_with_active_allocation_cannot_be_allocated_to_second_bed(self):
        self.state['allocations'].append({
            'id': 30, 'bed_id': 21, 'patient_id': 11,
            'daily_charge': 100, 'status': 'admitted'
        })
        self.state['beds'][21]['status'] = 'occupied'
        before = deepcopy(self.state)
        database = BedAllocationDatabase(self.state)
        with patch.object(beds, 'get_db', return_value=database):
            response = self.client_for('receptionist').post('/beds/allocate', data={
                'bed_id': '22', 'patient_id': '11', 'daily_charge': '250'
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state, before)

    def test_occupied_bed_cannot_be_allocated_to_another_patient(self):
        before = deepcopy(self.state)
        database = BedAllocationDatabase(self.state)
        with patch.object(beds, 'get_db', return_value=database):
            response = self.client_for('receptionist').post('/beds/allocate', data={
                'bed_id': '23', 'patient_id': '12', 'daily_charge': '250'
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state, before)

    def test_discharge_preserves_allocation_history_and_releases_bed(self):
        allocation = {
            'id': 30, 'bed_id': 23, 'patient_id': 11,
            'daily_charge': 100, 'status': 'admitted'
        }
        self.state['allocations'].append(allocation)
        database = BedAllocationDatabase(self.state)
        with patch.object(beds, 'get_db', return_value=database):
            response = self.client_for('receptionist').post('/beds/discharge/30')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.state['allocations']), 1)
        self.assertEqual(self.state['allocations'][0]['status'], 'discharged')
        self.assertEqual(self.state['beds'][23]['status'], 'available')
        self.assertTrue(database.cursor.statements)

    def test_two_concurrent_allocations_cannot_occupy_same_bed(self):
        lock = Lock()
        results = []

        def allocate(patient_id):
            client = self.client_for('receptionist')
            with patch.object(
                beds, 'get_db',
                side_effect=lambda: BedAllocationDatabase(self.state, lock)
            ):
                response = client.post('/beds/allocate', data={
                    'bed_id': '21',
                    'patient_id': str(patient_id),
                    'daily_charge': '250'
                })
            results.append(response.status_code)

        threads = [Thread(target=allocate, args=(patient_id,)) for patient_id in (11, 12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(results, [302, 302])
        self.assertEqual(len(self.state['allocations']), 1)
        self.assertEqual(self.state['beds'][21]['status'], 'occupied')

    def test_two_concurrent_allocations_cannot_assign_patient_to_different_beds(self):
        lock = Lock()
        results = []

        def allocate(bed_id):
            client = self.client_for('receptionist')
            with patch.object(
                beds, 'get_db',
                side_effect=lambda: BedAllocationDatabase(self.state, lock)
            ):
                response = client.post('/beds/allocate', data={
                    'bed_id': str(bed_id),
                    'patient_id': '11',
                    'daily_charge': '250'
                })
            results.append(response.status_code)

        threads = [Thread(target=allocate, args=(bed_id,)) for bed_id in (21, 22)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(results), 2)
        patient_allocations = [
            allocation for allocation in self.state['allocations']
            if allocation['patient_id'] == 11 and allocation['status'] == 'admitted'
        ]
        self.assertEqual(len(patient_allocations), 1)

    def test_existing_multiple_active_allocations_are_reported_without_repairing_records(self):
        with patch.object(beds, 'query_all', side_effect=[
            [], [], [], [
                {'conflict_type': 'patient', 'conflict_count': 1},
                {'conflict_type': 'bed', 'conflict_count': 0},
            ]
        ]):
            response = self.client_for('receptionist').get('/beds/availability')
        self.assertEqual(response.status_code, 200)
        self.assertIn('1 patient(s) have multiple active bed allocations', response.get_data(as_text=True))

    def test_overlapping_room_schedule_is_rejected_but_non_overlapping_reuse_is_allowed(self):
        peer = self.peer_schedule(
            start='09:00:00', end='12:00:00',
            break_start='10:00:00', break_end='11:00:00'
        )
        cursor = RoomScheduleCursor([peer])
        with patch.object(admin, 'query_one', return_value={
            'id': 8, 'full_name': 'Target Doctor', 'room_number': 'R-1',
            'department': 'General', 'specialization': 'General'
        }), patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = self.client_for().post('/admin/doctors/8/schedules', data={
                'day_of_week': 'Monday',
                'start_time': '09:30',
                'end_time': '10:30',
                'slot_duration_mins': '30',
            })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(cursor.schedule_inserted)

        cursor = RoomScheduleCursor([peer])
        with patch.object(admin, 'query_one', return_value={
            'id': 8, 'full_name': 'Target Doctor', 'room_number': 'R-1',
            'department': 'General', 'specialization': 'General'
        }), patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = self.client_for().post('/admin/doctors/8/schedules', data={
                'day_of_week': 'Monday',
                'start_time': '10:00',
                'end_time': '10:45',
                'slot_duration_mins': '15',
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(cursor.schedule_inserted)

    def test_doctor_creation_rejects_room_overlap_in_default_weekday_schedule(self):
        cursor = RoomScheduleCursor([self.peer_schedule()])
        doctor_form = {
            'username': 'newdoctor', 'password': 'Validpass1!',
            'full_name': 'New Doctor', 'email': 'newdoctor@gmail.com',
            'phone': '9876543210', 'department': 'General',
            'specialization': 'General', 'qualification': 'MBBS',
            'room_number': 'R-1', 'consulting_fee': '500',
        }
        with patch.object(admin, 'query_one', return_value=None), \
                patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = self.client_for().post('/admin/doctors', data=doctor_form)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(cursor.user_inserted)
        self.assertFalse(cursor.doctor_inserted)

    def test_doctor_creation_allows_room_reuse_outside_default_working_hours(self):
        cursor = RoomScheduleCursor([self.peer_schedule(start='14:00:00', end='16:00:00')])
        doctor_form = {
            'username': 'newdoctor', 'password': 'Validpass1!',
            'full_name': 'New Doctor', 'email': 'newdoctor@gmail.com',
            'phone': '9876543210', 'department': 'General',
            'specialization': 'General', 'qualification': 'MBBS',
            'room_number': 'R-1', 'consulting_fee': '500',
        }
        with patch.object(admin, 'query_one', return_value=None), \
                patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = self.client_for().post('/admin/doctors', data=doctor_form)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(cursor.user_inserted)
        self.assertTrue(cursor.doctor_inserted)

    def test_doctor_edit_rejects_overlapping_room_and_schedule_assignment(self):
        cursor = RoomScheduleCursor(
            [self.peer_schedule()],
            own_schedules=[{
                'day_of_week': 'Monday', 'start_time': '09:00:00',
                'end_time': '13:00:00', 'break_start': None, 'break_end': None,
            }]
        )
        with patch.object(admin, 'query_one', side_effect=[
            {'user_id': 5}, None
        ]), patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = self.client_for().post('/admin/doctors/8/update', data={
                'full_name': 'Target Doctor', 'email': 'target@gmail.com',
                'phone': '9876543210', 'department': 'General',
                'specialization': 'General', 'qualification': 'MBBS',
                'room_number': 'R-1', 'consulting_fee': '500',
            })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any('UPDATE doctors' in sql for sql, _ in cursor.calls))

    def test_doctor_edit_allows_room_reuse_outside_existing_working_hours(self):
        cursor = RoomScheduleCursor(
            [self.peer_schedule(start='14:00:00', end='16:00:00')],
            own_schedules=[{
                'day_of_week': 'Monday', 'start_time': '09:00:00',
                'end_time': '13:00:00', 'break_start': None, 'break_end': None,
            }]
        )
        with patch.object(admin, 'query_one', side_effect=[
            {'user_id': 5}, None
        ]), patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = self.client_for().post('/admin/doctors/8/update', data={
                'full_name': 'Target Doctor', 'email': 'target@gmail.com',
                'phone': '9876543210', 'department': 'General',
                'specialization': 'General', 'qualification': 'MBBS',
                'room_number': 'R-1', 'consulting_fee': '500',
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('UPDATE doctors' in sql for sql, _ in cursor.calls))

    def test_doctor_status_change_requires_confirmation_and_checks_reactivation_room(self):
        client = self.client_for()
        with patch.object(admin, 'get_db') as unconfirmed_database:
            response = client.post('/admin/doctors/8/toggle-status')
        self.assertEqual(response.status_code, 302)
        unconfirmed_database.assert_not_called()

        cursor = DoctorStatusCursor({
            'id': 8, 'status': 'active', 'user_id': 80, 'room_number': 'R-1'
        })
        with patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = client.post(
                '/admin/doctors/8/toggle-status', data={'confirmed': '1'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('UPDATE doctors SET status' in sql for sql, _ in cursor.calls))
        self.assertTrue(any('UPDATE users SET status' in sql for sql, _ in cursor.calls))

        cursor = DoctorStatusCursor(
            {'id': 8, 'status': 'inactive', 'user_id': 80, 'room_number': 'R-1'},
            schedules=[{
                'day_of_week': 'Monday', 'start_time': '09:00:00',
                'end_time': '13:00:00', 'break_start': None, 'break_end': None,
            }],
            peer_schedules=[self.peer_schedule()],
        )
        with patch.object(admin, 'get_db', return_value=CursorDatabase(cursor)):
            response = client.post(
                '/admin/doctors/8/toggle-status', data={'confirmed': '1'}
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any('UPDATE doctors SET status' in sql for sql, _ in cursor.calls))

    def test_receptionist_can_access_existing_appointment_and_billing_routes(self):
        client = self.client_for('receptionist', user_id=44)
        with patch.object(receptionist, 'query_all', return_value=[]):
            appointments_response = client.get('/receptionist/appointments')
            bills_response = client.get('/receptionist/billing')
        self.assertEqual(appointments_response.status_code, 200)
        self.assertEqual(bills_response.status_code, 200)
        admin_response = client.post('/admin/staff', data={})
        self.assertEqual(admin_response.status_code, 302)

    def test_receptionist_checkin_workflow_remains_available(self):
        client = self.client_for('receptionist', user_id=44)
        appointment = {
            'id': 31, 'patient_user_id': 51, 'doctor_user_id': 61,
            'patient_name': 'Patient', 'appointment_time': '09:00:00',
            'status': 'pending', 'appointment_date': 'today',
        }
        cursor = MagicMock()
        cursor.fetchone.return_value = appointment
        cursor.rowcount = 1
        db_context = MagicMock()
        db_context.__enter__.return_value = (cursor, MagicMock())
        with patch.object(receptionist, 'get_db', return_value=db_context), \
                patch.object(receptionist, 'create_notification'):
            response = client.post('/receptionist/checkin/31')
        self.assertEqual(response.status_code, 302)
        self.assertIn('FOR UPDATE', cursor.execute.call_args_list[0].args[0])
        self.assertIn("status IN ('pending', 'approved')", cursor.execute.call_args_list[1].args[0])
        self.assertEqual(cursor.execute.call_args_list[1].args[1], (31,))

    def test_action_buttons_keep_primary_destructive_focus_and_confirmation_styles(self):
        root = Path(__file__).resolve().parents[1]
        stylesheet = (root / 'static' / 'css' / 'site.css').read_text(encoding='utf-8')
        bed_template = (root / 'templates' / 'admin' / 'beds_availability.html').read_text(encoding='utf-8')
        doctor_template = (root / 'templates' / 'admin' / 'manage_doctors.html').read_text(encoding='utf-8')
        staff_template = (root / 'templates' / 'admin' / 'manage_staff.html').read_text(encoding='utf-8')
        self.assertIn('.button-primary { background: var(--teal); color: white; }', stylesheet)
        self.assertIn('.button.button-danger { background: #fff0ef; color: #7a1f22 !important; border: 1px solid #b53a3d; }', stylesheet)
        self.assertIn('.button:focus-visible', stylesheet)
        self.assertIn('.button:disabled', stylesheet)
        self.assertIn("window.confirm('Discharge this patient and free the bed?')", bed_template)
        self.assertIn("window.confirm('Change this doctor account status?')", doctor_template)
        self.assertIn("window.confirm('Change this staff account status?')", staff_template)

    def test_room_assignment_lock_is_released_after_transaction_commit(self):
        events = []
        connection = AdvisoryLockConnection(events)
        with patch.object(database, 'get_db_connection', return_value=connection):
            with database.get_db(lock_name='room-lock') as (cursor, _):
                cursor.execute('UPDATE doctors SET room_number = %s', ('R-1',))
        self.assertEqual(events[:4], ['lock', 'write', 'commit', 'release'])
        self.assertEqual(events[-2:], ['cursor-close', 'connection-close'])


if __name__ == '__main__':
    unittest.main()
