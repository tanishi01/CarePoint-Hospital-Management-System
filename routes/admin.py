from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta

from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, jsonify, session
from database import query_one, query_all, execute_dml, get_db
from utils.appointment_helper import (
    build_slot_availability,
    parse_iso_date,
    parse_schedule_time,
)
from utils.auth_helpers import role_required, hash_password
from utils.leave_helper import (
    appointment_overlaps_leave,
    is_leave_schema_error,
    is_valid_partial_leave,
    leave_overlaps_existing,
)
from utils.notification_helper import create_notification
from utils.input_validation import (
    normalize_email,
    validate_email,
    validate_indian_mobile,
    validate_password,
    validate_person_name,
    validate_room_number,
)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
ROOM_ASSIGNMENT_LOCK = 'carepoint_room_assignments'


def _schedule_working_intervals(schedule):
    start = parse_schedule_time(schedule['start_time'])
    end = parse_schedule_time(schedule['end_time'])
    break_start = parse_schedule_time(schedule.get('break_start'))
    break_end = parse_schedule_time(schedule.get('break_end'))
    if start is None or end is None or end <= start:
        raise ValueError('Invalid doctor schedule found while checking room assignment.')
    if break_start is None and break_end is None:
        return [(start, end)]
    if (
        break_start is None or break_end is None or break_end <= break_start
        or break_start < start or break_end > end
    ):
        raise ValueError('Invalid doctor schedule break found while checking room assignment.')
    return [(start, break_start), (break_end, end)]


def _room_schedule_conflicts(cursor, room_number, doctor_id, candidate_schedules):
    cursor.execute("""
        SELECT d.id AS doctor_id, u.full_name AS doctor_name,
               ds.day_of_week, ds.start_time, ds.end_time,
               ds.break_start, ds.break_end
        FROM doctors d
        JOIN users u ON u.id = d.user_id
        JOIN doctor_schedules ds ON ds.doctor_id = d.id
        WHERE d.room_number = %s AND d.id != %s
          AND d.status = 'active' AND u.status = 'active'
        FOR UPDATE
    """, (room_number, doctor_id or 0))
    existing_schedules = cursor.fetchall()
    conflicts = []
    for candidate in candidate_schedules:
        candidate_intervals = _schedule_working_intervals(candidate)
        for existing in existing_schedules:
            if existing['day_of_week'] != candidate['day_of_week']:
                continue
            if any(
                candidate_start < existing_end and existing_start < candidate_end
                for candidate_start, candidate_end in candidate_intervals
                for existing_start, existing_end
                in _schedule_working_intervals(existing)
            ):
                conflicts.append(existing)
    return conflicts

@admin_bp.route('/dashboard')
@role_required('admin')
def dashboard():
    try:
        total_patients = query_one("SELECT COUNT(*) AS count FROM patients")['count']
        total_doctors = query_one("SELECT COUNT(*) AS count FROM doctors WHERE status = 'active'")['count']
        today_appointments = query_one("SELECT COUNT(*) AS count FROM appointments WHERE appointment_date = CURRENT_DATE()")['count']
        
        # Bed Occupancy Calculation
        total_beds = query_one("SELECT COUNT(*) AS count FROM beds")['count']
        occupied_beds = query_one("SELECT COUNT(*) AS count FROM beds WHERE status = 'occupied'")['count']
        occupancy_rate = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

        # Pending Dues Aggregate
        pending_dues = query_one("SELECT SUM(total_amount - paid_amount) AS sum FROM bills WHERE status != 'paid'")['sum'] or 0

        # Low Stock Alert Count
        low_stock_count = query_one("SELECT COUNT(*) AS count FROM medicines WHERE stock_quantity <= min_stock_threshold")['count']

        try:
            pending_leave_count = query_one("""
                SELECT COUNT(*) AS count
                FROM doctor_leave_requests
                WHERE status = 'pending'
            """)['count']
        except Exception as error:
            current_app.logger.exception('Could not load pending doctor leave count.')
            pending_leave_count = None
            if is_leave_schema_error(error):
                current_app.logger.error(
                    'Apply the Emergency Leave migrations to the configured database.'
                )

        recent_appointments = query_all("""
            SELECT a.*, p_u.full_name AS patient_name, d_u.full_name AS doctor_name
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN users p_u ON p.user_id = p_u.id
            JOIN doctors d ON a.doctor_id = d.id
            JOIN users d_u ON d.user_id = d_u.id
            ORDER BY a.created_at DESC LIMIT 5
        """)

        return render_template('admin/dashboard.html',
                               total_patients=total_patients,
                               total_doctors=total_doctors,
                               today_appointments=today_appointments,
                               total_beds=total_beds,
                               occupied_beds=occupied_beds,
                               occupancy_rate=occupancy_rate,
                               pending_dues=pending_dues,
                               low_stock_count=low_stock_count,
                               pending_leave_count=pending_leave_count,
                               recent_appointments=recent_appointments)
    except Exception as e:
        flash(f'Error loading dashboard metrics: {str(e)}', 'danger')
        return render_template('admin/dashboard.html')

@admin_bp.route('/doctors', methods=['GET', 'POST'])
@role_required('admin')
def manage_doctors():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        full_name = ' '.join(request.form.get('full_name', '').split())
        email = normalize_email(request.form.get('email', ''))
        phone = request.form.get('phone', '').strip()
        department = request.form.get('department', '').strip()
        specialization = request.form.get('specialization', '').strip()
        qualification = request.form.get('qualification', '').strip()
        room_number = request.form.get('room_number', '').strip()

        fee_value = request.form.get('consulting_fee', '500.00').strip() or '500.00'
        try:
            consulting_fee = Decimal(fee_value)
        except InvalidOperation:
            consulting_fee = None

        if not email or not validate_email(email):
            flash('Enter a valid doctor email using gmail.com, yahoo.com, yahoo.co.in, outlook.com, or nmims.in.', 'danger')
            return redirect(url_for('admin.manage_doctors'))

        if not all([username, password, full_name, email, phone, department, specialization, qualification, room_number]) or len(room_number) > 20:
            flash('All doctor fields are required.', 'danger')
            return redirect(url_for('admin.manage_doctors'))
        if not validate_person_name(full_name):
            flash('Doctor name must contain letters and may include spaces, hyphens, or apostrophes.', 'danger')
            return redirect(url_for('admin.manage_doctors'))
        if not validate_indian_mobile(phone):
            flash('Doctor phone must be a 10-digit Indian mobile number starting with 6, 7, 8, or 9.', 'danger')
            return redirect(url_for('admin.manage_doctors'))
        if not validate_room_number(room_number):
            flash(
                'Enter a valid room number, such as 208, Room 101, or R-1.',
                'danger'
            )
            return redirect(url_for('admin.manage_doctors'))
        if not validate_password(password):
            flash('Temporary password must be at least 8 characters and include uppercase, lowercase, a number, and a special character.', 'danger')
            return redirect(url_for('admin.manage_doctors'))
        if consulting_fee is None or not consulting_fee.is_finite() or not Decimal('0') <= consulting_fee <= Decimal('99999999.99'):
            flash('Consultation fee must be a valid amount between 0 and 99999999.99.', 'danger')
            return redirect(url_for('admin.manage_doctors'))

        try:
            existing = query_one("SELECT id FROM users WHERE username = %s OR email = %s", (username, email))
            if existing:
                flash('Username or Email already exists.', 'warning')
                return redirect(url_for('admin.manage_doctors'))

            default_schedule = [{
                'day_of_week': day,
                'start_time': '09:00:00',
                'end_time': '13:00:00',
                'break_start': None,
                'break_end': None,
            } for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']]
            with get_db(lock_name=ROOM_ASSIGNMENT_LOCK) as (cursor, _):
                if _room_schedule_conflicts(cursor, room_number, None, default_schedule):
                    flash(
                        'This room is already assigned during overlapping working hours.',
                        'warning'
                    )
                    return redirect(url_for('admin.manage_doctors'))

                cursor.execute(
                    "INSERT INTO users (username, password_hash, role, full_name, email, phone) VALUES (%s, %s, %s, %s, %s, %s)",
                    (username, hash_password(password), 'doctor', full_name, email, phone)
                )
                user_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO doctors (user_id, department, specialization, qualification, room_number, consulting_fee) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id, department, specialization, qualification, room_number, consulting_fee)
                )
                doctor_id = cursor.lastrowid

                for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
                    cursor.execute("""
                        INSERT INTO doctor_schedules (doctor_id, day_of_week, start_time, end_time, slot_duration_mins)
                        VALUES (%s, %s, '09:00:00', '13:00:00', 30)
                    """, (doctor_id, day))

            flash(f'Doctor Dr. {full_name} added successfully with default weekday schedules.', 'success')
            return redirect(url_for('admin.manage_doctors'))
        except Exception as e:
            flash(f'Error adding doctor: {str(e)}', 'danger')

    doctors = query_all("""
        SELECT d.*, u.username, u.full_name, u.email, u.phone, u.status AS user_status,
               NOT EXISTS (
                   SELECT 1 FROM appointments a WHERE a.doctor_id = d.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM medical_records mr WHERE mr.doctor_id = d.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM doctor_schedules ds WHERE ds.doctor_id = d.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM doctor_leave_requests dlr WHERE dlr.doctor_id = d.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM notifications n WHERE n.user_id = u.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM doctor_leave_requests dlr
                   WHERE dlr.reviewed_by = u.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM password_reset_tokens prt WHERE prt.user_id = u.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM staff s WHERE s.user_id = u.id
               ) AND NOT EXISTS (
                   SELECT 1 FROM patients p WHERE p.user_id = u.id
               ) AS can_delete
        FROM doctors d
        JOIN users u ON d.user_id = u.id
        ORDER BY d.id DESC
    """)
    return render_template('admin/manage_doctors.html', doctors=doctors)

@admin_bp.route('/doctors/<int:doctor_id>/update', methods=['POST'])
@role_required('admin')
def update_doctor(doctor_id):
    full_name = ' '.join(request.form.get('full_name', '').split())
    email = normalize_email(request.form.get('email', ''))
    phone = request.form.get('phone', '').strip()
    department = request.form.get('department', '').strip()
    specialization = request.form.get('specialization', '').strip()
    qualification = request.form.get('qualification', '').strip()
    room_number = request.form.get('room_number', '').strip()

    fee_value = request.form.get('consulting_fee', '').strip()
    try:
        consulting_fee = Decimal(fee_value)
    except InvalidOperation:
        consulting_fee = None

    if not email or not validate_email(email):
        flash('Enter a valid doctor email using gmail.com, yahoo.com, yahoo.co.in, outlook.com, or nmims.in.', 'danger')
        return redirect(url_for('admin.manage_doctors'))

    if not all([full_name, email, phone, department, specialization, qualification, room_number, fee_value]) or len(room_number) > 20:
        flash('All doctor fields are required.', 'danger')
        return redirect(url_for('admin.manage_doctors'))
    if not validate_person_name(full_name):
        flash('Doctor name must contain letters and may include spaces, hyphens, or apostrophes.', 'danger')
        return redirect(url_for('admin.manage_doctors'))
    if not validate_indian_mobile(phone):
        flash('Doctor phone must be a 10-digit Indian mobile number starting with 6, 7, 8, or 9.', 'danger')
        return redirect(url_for('admin.manage_doctors'))
    if not validate_room_number(room_number):
        flash(
            'Enter a valid room number, such as 208, Room 101, or R-1.',
            'danger'
        )
        return redirect(url_for('admin.manage_doctors'))
    if consulting_fee is None or not consulting_fee.is_finite() or not Decimal('0') <= consulting_fee <= Decimal('99999999.99'):
        flash('Consultation fee must be a valid amount between 0 and 99999999.99.', 'danger')
        return redirect(url_for('admin.manage_doctors'))

    try:
        doctor = query_one("SELECT user_id FROM doctors WHERE id = %s", (doctor_id,))
        if not doctor:
            flash('Doctor not found.', 'danger')
            return redirect(url_for('admin.manage_doctors'))
        duplicate_email = query_one(
            "SELECT id FROM users WHERE email = %s AND id != %s",
            (email, doctor['user_id'])
        )
        if duplicate_email:
            flash('This email address is already in use by another user.', 'warning')
            return redirect(url_for('admin.manage_doctors'))

        with get_db(lock_name=ROOM_ASSIGNMENT_LOCK) as (cursor, _):
            cursor.execute(
                'SELECT id FROM doctors WHERE id = %s FOR UPDATE',
                (doctor_id,)
            )
            if not cursor.fetchone():
                flash('Doctor not found.', 'danger')
                return redirect(url_for('admin.manage_doctors'))
            cursor.execute("""
                SELECT day_of_week, start_time, end_time, break_start, break_end
                FROM doctor_schedules
                WHERE doctor_id = %s
                FOR UPDATE
            """, (doctor_id,))
            schedules = cursor.fetchall()
            if _room_schedule_conflicts(cursor, room_number, doctor_id, schedules):
                flash(
                    'This room is already assigned during overlapping working hours.',
                    'warning'
                )
                return redirect(url_for('admin.manage_doctors'))
            cursor.execute(
                "UPDATE users SET full_name = %s, email = %s, phone = %s WHERE id = %s",
                (full_name, email, phone, doctor['user_id'])
            )
            cursor.execute("""
                UPDATE doctors SET department = %s, specialization = %s, qualification = %s, room_number = %s, consulting_fee = %s
                WHERE id = %s
            """, (department, specialization, qualification, room_number, consulting_fee, doctor_id))

        flash('Doctor updated successfully.', 'success')
    except Exception as e:
        flash(f'Error updating doctor: {str(e)}', 'danger')

    return redirect(url_for('admin.manage_doctors'))

@admin_bp.route('/doctors/<int:doctor_id>/toggle-status', methods=['POST'])
@role_required('admin')
def toggle_doctor_status(doctor_id):
    try:
        if request.form.get('confirmed') != '1':
            flash('Confirm the doctor account status change before continuing.', 'warning')
            return redirect(url_for('admin.manage_doctors'))
        with get_db(lock_name=ROOM_ASSIGNMENT_LOCK) as (cursor, _):
            cursor.execute("""
                    SELECT d.id, d.status, d.user_id, d.room_number
                    FROM doctors d
                    WHERE d.id = %s
                    FOR UPDATE
            """, (doctor_id,))
            doctor = cursor.fetchone()
            if not doctor:
                flash('Doctor not found.', 'danger')
                return redirect(url_for('admin.manage_doctors'))
            new_status = 'inactive' if doctor['status'] == 'active' else 'active'
            if new_status == 'active':
                cursor.execute("""
                        SELECT day_of_week, start_time, end_time, break_start, break_end
                        FROM doctor_schedules
                        WHERE doctor_id = %s
                        FOR UPDATE
                """, (doctor_id,))
                schedules = cursor.fetchall()
                if _room_schedule_conflicts(
                    cursor, doctor['room_number'], doctor_id, schedules
                ):
                    flash(
                        'Doctor cannot be activated because this room is assigned '
                        'during overlapping working hours.',
                        'warning'
                    )
                    return redirect(url_for('admin.manage_doctors'))
            cursor.execute(
                'UPDATE doctors SET status = %s WHERE id = %s',
                (new_status, doctor_id)
            )
            if cursor.rowcount != 1:
                raise RuntimeError('Doctor status could not be updated.')
            cursor.execute(
                'UPDATE users SET status = %s WHERE id = %s',
                (new_status, doctor['user_id'])
            )
            if cursor.rowcount != 1:
                raise RuntimeError('Doctor user status could not be updated.')
        flash(f'Doctor status changed to {new_status}.', 'info')
    except Exception:
        current_app.logger.exception('Could not change doctor account status.')
        flash('Doctor status could not be changed. Please try again.', 'danger')
    return redirect(url_for('admin.manage_doctors'))

@admin_bp.route('/doctors/<int:doctor_id>/delete', methods=['POST'])
@role_required('admin')
def delete_doctor(doctor_id):
    if request.form.get('confirmed') != '1':
        flash('Confirm doctor deletion before continuing.', 'warning')
        return redirect(url_for('admin.manage_doctors'))

    try:
        with get_db(lock_name=ROOM_ASSIGNMENT_LOCK) as (cursor, _):
            cursor.execute("""
                SELECT id, user_id
                FROM doctors
                WHERE id = %s
                FOR UPDATE
            """, (doctor_id,))
            doctor = cursor.fetchone()
            if not doctor:
                flash('Doctor not found.', 'danger')
                return redirect(url_for('admin.manage_doctors'))

            cursor.execute(
                'SELECT id, role FROM users WHERE id = %s FOR UPDATE',
                (doctor['user_id'],)
            )
            doctor_user = cursor.fetchone()
            if not doctor_user or doctor_user['role'] != 'doctor':
                raise RuntimeError('The linked doctor account is missing or inconsistent.')

            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM appointments WHERE doctor_id = %s) AS appointments,
                    (SELECT COUNT(*) FROM medical_records WHERE doctor_id = %s) AS medical_records,
                    (SELECT COUNT(*) FROM doctor_schedules WHERE doctor_id = %s) AS schedules,
                    (SELECT COUNT(*) FROM doctor_leave_requests WHERE doctor_id = %s) AS leave_requests,
                    (SELECT COUNT(*) FROM notifications WHERE user_id = %s) AS notifications,
                    (SELECT COUNT(*) FROM doctor_leave_requests WHERE reviewed_by = %s) AS reviewed_leaves,
                    (SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s) AS reset_tokens,
                    (SELECT COUNT(*) FROM staff WHERE user_id = %s) AS staff_records,
                    (SELECT COUNT(*) FROM patients WHERE user_id = %s) AS patient_records
            """, (
                doctor_id, doctor_id, doctor_id, doctor_id,
                doctor['user_id'], doctor['user_id'], doctor['user_id'],
                doctor['user_id'], doctor['user_id']
            ))
            dependencies = cursor.fetchone()
            linked_data = {
                'appointments': 'appointments',
                'medical_records': 'clinical records and prescriptions',
                'schedules': 'working schedules',
                'leave_requests': 'leave requests',
                'notifications': 'notifications',
                'reviewed_leaves': 'leave requests reviewed by this doctor',
                'reset_tokens': 'password reset records',
                'staff_records': 'staff records',
                'patient_records': 'patient records',
            }
            linked = [
                label for key, label in linked_data.items()
                if dependencies[key]
            ]
            if linked:
                flash(
                    'This doctor cannot be deleted because linked records exist: '
                    + ', '.join(linked)
                    + '. Deactivate the account to preserve history.',
                    'warning'
                )
                return redirect(url_for('admin.manage_doctors'))

            cursor.execute('DELETE FROM doctors WHERE id = %s', (doctor_id,))
            if cursor.rowcount != 1:
                raise RuntimeError('Doctor record could not be deleted.')
            cursor.execute(
                "DELETE FROM users WHERE id = %s AND role = 'doctor'",
                (doctor['user_id'],)
            )
            if cursor.rowcount != 1:
                raise RuntimeError('Doctor account could not be deleted.')

        flash('Doctor account deleted successfully.', 'success')
    except Exception:
        current_app.logger.exception('Could not safely delete doctor %s.', doctor_id)
        flash('Doctor could not be deleted. No linked records were removed.', 'danger')
    return redirect(url_for('admin.manage_doctors'))

@admin_bp.route('/doctors/<int:doctor_id>/schedules', methods=['GET', 'POST'])
@role_required('admin')
def doctor_schedules(doctor_id):
    doctor = query_one("""
        SELECT d.*, u.full_name, u.email 
        FROM doctors d 
        JOIN users u ON d.user_id = u.id 
        WHERE d.id = %s
    """, (doctor_id,))

    if not doctor:
        flash('Doctor not found.', 'danger')
        return redirect(url_for('admin.manage_doctors'))

    if request.method == 'POST':
        day_of_week = request.form.get('day_of_week', '').strip()
        start_time = request.form.get('start_time', '').strip()
        end_time = request.form.get('end_time', '').strip()
        break_start = request.form.get('break_start', '').strip() or None
        break_end = request.form.get('break_end', '').strip() or None
        try:
            slot_duration_mins = int(request.form.get('slot_duration_mins', '') or '')
        except (ValueError, TypeError):
            slot_duration_mins = None

        valid_days = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        parsed_start = parse_schedule_time(start_time)
        parsed_end = parse_schedule_time(end_time)
        parsed_break_start = parse_schedule_time(break_start)
        parsed_break_end = parse_schedule_time(break_end)
        if (
            day_of_week not in valid_days
            or parsed_start is None
            or parsed_end is None
            or parsed_end <= parsed_start
            or slot_duration_mins is None
            or slot_duration_mins <= 0
            or (break_start is None) != (break_end is None)
            or (break_start is not None and (
                parsed_break_start is None
                or parsed_break_end is None
                or parsed_break_end <= parsed_break_start
                or parsed_break_start < parsed_start
                or parsed_break_end > parsed_end
            ))
        ):
            flash(
                'Enter a valid day, a start time before the end time, a positive slot duration, and break start/end within working hours.',
                'danger'
            )
        else:
            try:
                with get_db(lock_name=ROOM_ASSIGNMENT_LOCK) as (cursor, _):
                    cursor.execute(
                        'SELECT room_number FROM doctors WHERE id = %s FOR UPDATE',
                        (doctor_id,)
                    )
                    doctor_room = cursor.fetchone()
                    if not doctor_room:
                        flash('Doctor not found.', 'danger')
                        return redirect(url_for('admin.manage_doctors'))
                    candidate_schedule = {
                        'day_of_week': day_of_week,
                        'start_time': start_time,
                        'end_time': end_time,
                        'break_start': break_start,
                        'break_end': break_end,
                    }
                    if _room_schedule_conflicts(
                        cursor,
                        doctor_room['room_number'],
                        doctor_id,
                        [candidate_schedule]
                    ):
                        flash(
                            'Another doctor is already assigned to this room during '
                            'overlapping working hours.',
                            'warning'
                        )
                        return redirect(url_for('admin.doctor_schedules', doctor_id=doctor_id))
                    cursor.execute("""
                        INSERT INTO doctor_schedules
                            (doctor_id, day_of_week, start_time, end_time, break_start, break_end, slot_duration_mins)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            start_time = VALUES(start_time),
                            end_time = VALUES(end_time),
                            break_start = VALUES(break_start),
                            break_end = VALUES(break_end),
                            slot_duration_mins = VALUES(slot_duration_mins)
                    """, (
                        doctor_id, day_of_week, start_time, end_time,
                        break_start, break_end, slot_duration_mins
                    ))
                flash(f'Schedule for {day_of_week} saved successfully.', 'success')
                return redirect(url_for('admin.doctor_schedules', doctor_id=doctor_id))
            except Exception as e:
                flash(f'Error saving schedule: {str(e)}', 'danger')

    schedules = query_all("""
        SELECT * FROM doctor_schedules 
        WHERE doctor_id = %s
        ORDER BY FIELD(day_of_week, 'Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
    """, (doctor_id,))
    impact_rows = query_all("""
        SELECT DAYNAME(appointment_date) AS day_of_week,
               COUNT(*) AS appointment_count,
               COUNT(DISTINCT patient_id) AS patient_count
        FROM appointments
        WHERE doctor_id = %s AND status != 'cancelled'
          AND (
              appointment_date > CURRENT_DATE()
              OR (
                  appointment_date = CURRENT_DATE()
                  AND appointment_time > CURRENT_TIME()
              )
          )
        GROUP BY DAYNAME(appointment_date)
    """, (doctor_id,))
    schedule_impacts = {
        row['day_of_week']: {
            'appointment_count': row['appointment_count'],
            'patient_count': row['patient_count']
        }
        for row in impact_rows
    }

    return render_template(
        'admin/doctor_schedules.html',
        doctor=doctor,
        schedules=schedules,
        schedule_impacts=schedule_impacts,
        today=datetime.now().date().isoformat()
    )


@admin_bp.route('/doctors/<int:doctor_id>/schedule-availability')
@role_required('admin')
def doctor_schedule_availability(doctor_id):
    date_value = request.args.get('date', '').strip()
    booking_date = parse_iso_date(date_value)
    if not booking_date:
        return jsonify({'error': 'Enter a valid appointment date.'}), 400

    try:
        day_of_week = booking_date.strftime('%A')
        schedules = query_all("""
            SELECT start_time, end_time, break_start, break_end, slot_duration_mins
            FROM doctor_schedules
            WHERE doctor_id = %s AND day_of_week = %s
        """, (doctor_id, day_of_week))
        if not schedules:
            return jsonify({
                'date': booking_date.isoformat(),
                'schedule': None,
                'slots': [],
                'message': 'No working hours are registered for this day.'
            })

        appointments = query_all("""
            SELECT a.id, a.appointment_time, a.status, u.full_name AS patient_name
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN users u ON p.user_id = u.id
            WHERE a.doctor_id = %s AND a.appointment_date = %s
            ORDER BY a.appointment_time
        """, (doctor_id, booking_date))
        leaves = query_all("""
            SELECT is_full_day, start_time, end_time
            FROM doctor_leave_requests
            WHERE doctor_id = %s AND leave_date = %s AND status = 'approved'
        """, (doctor_id, booking_date))
        active_appointments = [
            appointment for appointment in appointments
            if appointment['status'] != 'cancelled'
        ]
        slots, schedule = build_slot_availability(
            schedules, active_appointments, booking_date, datetime.min, leaves
        )
        duration = schedules[0]['slot_duration_mins']
        slot_endpoints = []
        for slot in slots:
            if slot['status'] == 'outside':
                continue

            slot_start_time = parse_schedule_time(slot['time'])
            slot_start = datetime.combine(booking_date, slot_start_time)
            slot_end = slot_start + timedelta(minutes=duration)
            slot_appointments = []
            for appointment in appointments:
                appointment_time = parse_schedule_time(appointment['appointment_time'])
                if appointment_time is None:
                    raise ValueError('Invalid appointment time in database.')
                appointment_start = datetime.combine(booking_date, appointment_time)
                appointment_end = appointment_start + timedelta(minutes=duration)
                if slot_start < appointment_end and slot_end > appointment_start:
                    raw_status = appointment['status']
                    status_key = {
                        'checked_in': 'in_progress',
                    }.get(raw_status, raw_status)
                    slot_appointments.append({
                        'patient_name': appointment['patient_name'],
                        'time': appointment_time.strftime('%H:%M'),
                        'status': status_key,
                        'status_label': status_key.replace('_', ' ').title()
                    })

            slot_endpoints.append({
                'time': slot['time'],
                'availability': 'booked' if any(
                    appointment['status'] != 'cancelled'
                    for appointment in slot_appointments
                ) else (
                    'leave' if slot['status'] == 'leave'
                    else ('break' if slot['status'] == 'break' else 'available')
                ),
                'appointments': slot_appointments
            })

        return jsonify({
            'date': booking_date.isoformat(),
            'schedule': schedule,
            'slots': slot_endpoints,
            'message': None
        })
    except Exception:
        current_app.logger.exception('Unable to load doctor schedule availability.')
        return jsonify({
            'error': 'Schedule availability could not be loaded. Please try again.'
        }), 500

@admin_bp.route('/doctors/<int:doctor_id>/schedules/<int:schedule_id>/delete', methods=['POST'])
@role_required('admin')
def delete_doctor_schedule(doctor_id, schedule_id):
    confirmed = request.form.get('confirmed') == '1'
    cancellation_reason = request.form.get('cancellation_reason', '').strip()
    try:
        expected_appointment_count = int(
            request.form.get('expected_appointment_count', '-1')
        )
        expected_patient_count = int(request.form.get('expected_patient_count', '-1'))
    except (TypeError, ValueError):
        expected_appointment_count = -1
        expected_patient_count = -1

    try:
        with get_db() as (cursor, _):
            cursor.execute("""
                SELECT ds.id, ds.day_of_week, u.full_name AS doctor_name
                FROM doctor_schedules ds
                JOIN doctors d ON ds.doctor_id = d.id
                JOIN users u ON d.user_id = u.id
                WHERE ds.id = %s AND ds.doctor_id = %s
                FOR UPDATE
            """, (schedule_id, doctor_id))
            schedule = cursor.fetchone()
            if not schedule:
                flash('This schedule day no longer exists.', 'warning')
                return redirect(url_for('admin.doctor_schedules', doctor_id=doctor_id))

            cursor.execute("""
                SELECT a.id, a.patient_id, a.appointment_date, a.appointment_time,
                       a.status, p.user_id AS patient_user_id, u.full_name AS patient_name
                FROM appointments a
                JOIN patients p ON a.patient_id = p.id
                JOIN users u ON p.user_id = u.id
                WHERE a.doctor_id = %s AND DAYNAME(a.appointment_date) = %s
                  AND a.status != 'cancelled'
                  AND (
                      a.appointment_date > CURRENT_DATE()
                      OR (
                          a.appointment_date = CURRENT_DATE()
                          AND a.appointment_time > CURRENT_TIME()
                      )
                  )
                ORDER BY a.appointment_date, a.appointment_time
                FOR UPDATE
            """, (doctor_id, schedule['day_of_week']))
            affected_appointments = cursor.fetchall()
            affected_patient_ids = {
                appointment['patient_id'] for appointment in affected_appointments
            }
            appointment_count = len(affected_appointments)
            patient_count = len(affected_patient_ids)

            if not confirmed:
                flash('Confirm removal of this schedule day before continuing.', 'warning')
                return redirect(url_for('admin.doctor_schedules', doctor_id=doctor_id))

            if (
                appointment_count != expected_appointment_count
                or patient_count != expected_patient_count
            ):
                flash(
                    'Future appointments changed since the page loaded. Review the updated '
                    'affected appointment and patient counts, then confirm again.',
                    'warning'
                )
                return redirect(url_for('admin.doctor_schedules', doctor_id=doctor_id))

            if appointment_count and (
                not cancellation_reason or len(cancellation_reason) > 500
            ):
                flash(
                    'Enter a cancellation reason of 1 to 500 characters for affected appointments.',
                    'danger'
                )
                return redirect(url_for('admin.doctor_schedules', doctor_id=doctor_id))

            for appointment in affected_appointments:
                cursor.execute("""
                    UPDATE appointments
                    SET status = 'cancelled', cancellation_reason = %s
                    WHERE id = %s AND status != 'cancelled'
                """, (cancellation_reason, appointment['id']))
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        'Appointment changed while the schedule day was being removed.'
                    )

                message = (
                    'Your appointment with {doctor} on {date} at {time} has been '
                    'cancelled. Reason: {reason}'
                ).format(
                    doctor=schedule['doctor_name'],
                    date=appointment['appointment_date'],
                    time=parse_schedule_time(appointment['appointment_time']).strftime('%H:%M'),
                    reason=cancellation_reason
                )
                cursor.execute(
                    'INSERT INTO notifications (user_id, message) VALUES (%s, %s)',
                    (appointment['patient_user_id'], message)
                )

            cursor.execute(
                'DELETE FROM doctor_schedules WHERE id = %s AND doctor_id = %s',
                (schedule_id, doctor_id)
            )
            if cursor.rowcount != 1:
                raise RuntimeError('Schedule day could not be removed.')

        if appointment_count:
            flash(
                'Schedule day removed. {0} appointment(s) for {1} patient(s) were cancelled '
                'and the patients were notified.'.format(appointment_count, patient_count),
                'success'
            )
        else:
            flash('Schedule day removed successfully.', 'info')
    except Exception:
        current_app.logger.exception('Unable to remove doctor schedule day.')
        flash(
            'Schedule day could not be removed. No appointments or notifications were changed.',
            'danger'
        )
    return redirect(url_for('admin.doctor_schedules', doctor_id=doctor_id))


def _future_appointments_affected_by_leave(leave, schedule, appointments, now):
    leave_date = leave['leave_date']
    if isinstance(leave_date, str):
        leave_date = parse_iso_date(leave_date)
    if leave_date is None or not schedule:
        return []

    affected = []
    for appointment in appointments:
        if appointment['status'] in ('cancelled', 'completed'):
            continue
        appointment_time = parse_schedule_time(appointment['appointment_time'])
        if appointment_time is None:
            raise ValueError('Invalid appointment time in database.')
        if datetime.combine(leave_date, appointment_time) <= now:
            continue
        appointment_with_duration = dict(appointment)
        appointment_with_duration['slot_duration_mins'] = schedule['slot_duration_mins']
        if appointment_overlaps_leave(appointment_with_duration, leave, schedule):
            affected.append(appointment)
    return affected


@admin_bp.route('/doctor-leaves')
@role_required('admin')
def doctor_leave_requests():
    try:
        leave_requests = query_all("""
            SELECT lr.*, d.id AS doctor_id, u.full_name AS doctor_name,
                   ds.start_time AS schedule_start, ds.end_time AS schedule_end,
                   ds.slot_duration_mins
            FROM doctor_leave_requests lr
            JOIN doctors d ON lr.doctor_id = d.id
            JOIN users u ON d.user_id = u.id
            LEFT JOIN doctor_schedules ds
              ON ds.doctor_id = d.id
             AND ds.day_of_week = DAYNAME(lr.leave_date)
            ORDER BY (lr.status = 'pending') DESC, lr.submitted_at DESC
        """)

        for leave in leave_requests:
            leave['requested_hours'] = (
                'Full working day'
                if leave['is_full_day']
                else '{0}–{1}'.format(
                    parse_schedule_time(leave['start_time']).strftime('%H:%M'),
                    parse_schedule_time(leave['end_time']).strftime('%H:%M')
                )
            )
            leave['affected_appointments'] = []
            leave['affected_patient_count'] = 0
            if leave['status'] != 'pending' or not leave['slot_duration_mins']:
                continue
            schedule = {
                'start_time': leave['schedule_start'],
                'end_time': leave['schedule_end'],
                'slot_duration_mins': leave['slot_duration_mins']
            }
            appointments = query_all("""
                SELECT a.id, a.patient_id, a.appointment_time, a.status,
                       p_u.full_name AS patient_name
                FROM appointments a
                JOIN patients p ON a.patient_id = p.id
                JOIN users p_u ON p.user_id = p_u.id
                WHERE a.doctor_id = %s AND a.appointment_date = %s
                ORDER BY a.appointment_time
            """, (leave['doctor_id'], leave['leave_date']))
            leave['affected_appointments'] = _future_appointments_affected_by_leave(
                leave, schedule, appointments, datetime.now()
            )
            leave['affected_patient_count'] = len({
                appointment['patient_id']
                for appointment in leave['affected_appointments']
            })

        return render_template(
            'admin/doctor_leaves.html',
            leave_requests=leave_requests,
            today=datetime.now().date().isoformat()
        )
    except Exception as error:
        current_app.logger.exception('Could not load doctor leave requests.')
        if is_leave_schema_error(error):
            flash(
                'Emergency Leave database setup is incomplete. Apply '
                '20261017_create_doctor_leave_requests.sql and '
                '20261017_add_appointment_cancellation_reason.sql to the configured MySQL database.',
                'danger'
            )
        else:
            flash('Doctor leave requests are temporarily unavailable.', 'danger')
        return redirect(url_for('admin.dashboard'))


@admin_bp.route('/doctor-leaves/<int:leave_id>/decision', methods=['POST'])
@role_required('admin')
def review_doctor_leave(leave_id):
    action = request.form.get('action', '').strip()
    rejection_reason = request.form.get('rejection_reason', '').strip()
    cancellation_reason = request.form.get('cancellation_reason', '').strip()
    confirmed = request.form.get('confirmed') == '1'
    if action not in ('approve', 'reject'):
        flash('Choose approve or reject for this leave request.', 'danger')
        return redirect(url_for('admin.doctor_leave_requests'))
    if action == 'reject' and (
        not rejection_reason or len(rejection_reason) > 2000
    ):
        flash('A rejection reason of 1–2000 characters is required.', 'danger')
        return redirect(url_for('admin.doctor_leave_requests'))

    try:
        with get_db() as (cursor, _):
            cursor.execute(
                'SELECT doctor_id FROM doctor_leave_requests WHERE id = %s',
                (leave_id,)
            )
            request_header = cursor.fetchone()
            if not request_header:
                flash('Leave request was not found.', 'warning')
                return redirect(url_for('admin.doctor_leave_requests'))

            doctor_id = request_header['doctor_id']
            cursor.execute(
                'SELECT id FROM doctors WHERE id = %s FOR UPDATE',
                (doctor_id,)
            )
            if not cursor.fetchone():
                flash('The doctor for this leave request no longer exists.', 'warning')
                return redirect(url_for('admin.doctor_leave_requests'))

            cursor.execute("""
                SELECT * FROM doctor_leave_requests
                WHERE id = %s AND doctor_id = %s
                FOR UPDATE
            """, (leave_id, doctor_id))
            leave = cursor.fetchone()
            if not leave or leave['status'] != 'pending':
                flash('This leave request has already been reviewed.', 'warning')
                return redirect(url_for('admin.doctor_leave_requests'))

            if action == 'reject':
                cursor.execute("""
                    UPDATE doctor_leave_requests
                    SET status = 'rejected', rejection_reason = %s,
                        reviewed_by = %s, reviewed_at = NOW()
                    WHERE id = %s AND status = 'pending'
                """, (rejection_reason, session.get('user_id'), leave_id))
                if cursor.rowcount != 1:
                    raise RuntimeError('Leave request changed during rejection.')
                flash('Leave request rejected.', 'info')
                return redirect(url_for('admin.doctor_leave_requests'))

            leave_date = leave['leave_date']
            if isinstance(leave_date, str):
                leave_date = parse_iso_date(leave_date)
            if leave_date is None:
                raise ValueError('Invalid leave date in database.')
            cursor.execute("""
                SELECT start_time, end_time, slot_duration_mins
                FROM doctor_schedules
                WHERE doctor_id = %s AND day_of_week = %s
                FOR UPDATE
            """, (doctor_id, leave_date.strftime('%A')))
            schedule = cursor.fetchone()
            if not schedule:
                flash('The doctor has no registered working schedule for this date.', 'warning')
                return redirect(url_for('admin.doctor_leave_requests'))
            if not leave['is_full_day'] and not is_valid_partial_leave(
                leave['start_time'], leave['end_time'], schedule
            ):
                flash(
                    "The doctor's schedule changed; this leave request no longer fits the "
                    'registered working hours.',
                    'warning'
                )
                return redirect(url_for('admin.doctor_leave_requests'))

            cursor.execute("""
                SELECT is_full_day, start_time, end_time
                FROM doctor_leave_requests
                WHERE doctor_id = %s AND leave_date = %s AND id != %s
                  AND status IN ('pending', 'approved')
                FOR UPDATE
            """, (doctor_id, leave_date, leave_id))
            if any(
                leave_overlaps_existing(leave, other_leave)
                for other_leave in cursor.fetchall()
            ):
                flash(
                    'An overlapping pending or approved leave request already exists.',
                    'warning'
                )
                return redirect(url_for('admin.doctor_leave_requests'))

            cursor.execute("""
                SELECT id, patient_id, appointment_time, status
                FROM appointments
                WHERE doctor_id = %s AND appointment_date = %s
                  AND status NOT IN ('cancelled', 'completed')
                ORDER BY appointment_time
                FOR UPDATE
            """, (doctor_id, leave_date))
            appointments = cursor.fetchall()
            affected = _future_appointments_affected_by_leave(
                leave, schedule, appointments, datetime.now()
            )
            if affected and (
                not confirmed
                or not cancellation_reason
                or len(cancellation_reason) > 500
            ):
                flash(
                    'Confirm cancellation of {0} affected appointment(s) and enter a '
                    'cancellation reason before approving.'.format(len(affected)),
                    'warning'
                )
                return redirect(url_for('admin.doctor_leave_requests'))

            for appointment in affected:
                cursor.execute("""
                    SELECT p.user_id AS patient_user_id,
                           du.full_name AS doctor_name
                    FROM appointments a
                    JOIN patients p ON a.patient_id = p.id
                    JOIN doctors d ON a.doctor_id = d.id
                    JOIN users du ON d.user_id = du.id
                    WHERE a.id = %s
                """, (appointment['id'],))
                notification_recipient = cursor.fetchone()
                if not notification_recipient:
                    raise RuntimeError('Could not resolve an affected appointment patient.')
                cursor.execute("""
                    UPDATE appointments
                    SET status = 'cancelled', cancellation_reason = %s
                    WHERE id = %s AND status NOT IN ('cancelled', 'completed')
                """, (cancellation_reason, appointment['id']))
                if cursor.rowcount != 1:
                    raise RuntimeError('Affected appointment changed during leave approval.')
                create_notification(
                    notification_recipient['patient_user_id'],
                    'Your appointment with {0} on {1} at {2} was cancelled: {3}'.format(
                        notification_recipient['doctor_name'],
                        leave_date.isoformat(),
                        parse_schedule_time(appointment['appointment_time']).strftime('%H:%M'),
                        cancellation_reason
                    ),
                    cursor=cursor
                )

            cursor.execute("""
                UPDATE doctor_leave_requests
                SET status = 'approved', cancellation_reason = %s,
                    reviewed_by = %s, reviewed_at = NOW()
                WHERE id = %s AND status = 'pending'
            """, (
                cancellation_reason if affected else None,
                session.get('user_id'),
                leave_id
            ))
            if cursor.rowcount != 1:
                raise RuntimeError('Leave request changed during approval.')
        flash(
            'Leave approved. {0} future appointment(s) cancelled and patients notified.'
            .format(len(affected)),
            'success'
        )
    except Exception as error:
        current_app.logger.exception('Could not review doctor leave request.')
        if is_leave_schema_error(error):
            flash(
                'Emergency Leave database setup is incomplete. Apply '
                '20261017_create_doctor_leave_requests.sql and '
                '20261017_add_appointment_cancellation_reason.sql to the configured MySQL database.',
                'danger'
            )
        else:
            flash(
                'Leave review failed. No cancellations, notifications, or approval were committed.',
                'danger'
            )
    return redirect(url_for('admin.doctor_leave_requests'))


@admin_bp.route('/staff', methods=['GET', 'POST'])
@role_required('admin')
def manage_staff():
    if request.method == 'POST':
        staff_id = request.form.get('staff_id', type=int)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role_type = request.form.get('role', 'receptionist').strip()
        full_name = ' '.join(request.form.get('full_name', '').split())
        email = normalize_email(request.form.get('email', ''))
        phone = request.form.get('phone', '').strip()
        role_title = request.form.get('role_title', '').strip()
        department = request.form.get('department', '').strip()

        if role_type != 'receptionist':
            flash('Only receptionist accounts can be created through staff management.', 'danger')
            return redirect(url_for('admin.manage_staff'))

        if (
            not username or len(username) > 50
            or not full_name or len(full_name) > 100
            or not role_title or len(role_title) > 50
            or not department or len(department) > 100
            or not phone or len(phone) > 20
        ):
            flash('Enter valid staff details within the supported field lengths.', 'danger')
            return redirect(url_for('admin.manage_staff'))

        if not email or len(email) > 100 or not validate_email(email):
            flash('Enter a valid staff email using gmail.com, yahoo.com, yahoo.co.in, outlook.com, or nmims.in.', 'danger')
            return redirect(url_for('admin.manage_staff'))

        if not validate_person_name(full_name):
            flash('Staff name must contain letters and may include spaces, hyphens, or apostrophes.', 'danger')
            return redirect(url_for('admin.manage_staff'))
        if not validate_indian_mobile(phone):
            flash('Staff phone must be a 10-digit Indian mobile number starting with 6, 7, 8, or 9.', 'danger')
            return redirect(url_for('admin.manage_staff'))
        if not staff_id and not validate_password(password):
            flash('Temporary password must be at least 8 characters and include uppercase, lowercase, a number, and a special character.', 'danger')
            return redirect(url_for('admin.manage_staff'))

        try:
            editing_staff = None
            if staff_id:
                editing_staff = query_one("""
                    SELECT s.id, s.user_id, u.role
                    FROM staff s
                    JOIN users u ON s.user_id = u.id
                    WHERE s.id = %s
                """, (staff_id,))
                if not editing_staff or editing_staff['role'] != 'receptionist':
                    flash('Receptionist staff member not found.', 'warning')
                    return redirect(url_for('admin.manage_staff'))

            existing = query_one("""
                SELECT id FROM users
                WHERE (username = %s OR email = %s)
                  AND id != %s
            """, (
                username, email,
                editing_staff['user_id'] if editing_staff else 0
            ))
            if existing:
                flash('Username or email is already in use.', 'warning')
                return redirect(url_for('admin.manage_staff'))

            with get_db() as (cursor, _):
                if editing_staff:
                    cursor.execute("""
                        UPDATE users
                        SET username = %s, full_name = %s, email = %s, phone = %s
                        WHERE id = %s AND role = 'receptionist'
                    """, (
                        username, full_name, email, phone,
                        editing_staff['user_id']
                    ))
                    cursor.execute("""
                        UPDATE staff SET role_title = %s, department = %s
                        WHERE id = %s
                    """, (role_title, department, staff_id))
                    if cursor.rowcount != 1:
                        raise RuntimeError('Staff details could not be updated.')
                else:
                    cursor.execute("""
                        INSERT INTO users
                            (username, password_hash, role, full_name, email, phone)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        username, hash_password(password), role_type,
                        full_name, email, phone
                    ))
                    user_id = cursor.lastrowid
                    cursor.execute("""
                        INSERT INTO staff (user_id, role_title, department)
                        VALUES (%s, %s, %s)
                    """, (user_id, role_title, department))

            if editing_staff:
                flash('Staff member updated successfully.', 'success')
            else:
                flash(f'Staff member {full_name} added successfully.', 'success')
            return redirect(url_for('admin.manage_staff'))
        except Exception:
            current_app.logger.exception('Could not save staff member.')
            flash('Staff details could not be saved. Check for duplicate accounts and try again.', 'danger')

    edit_staff_id = request.args.get('edit', type=int)
    editing_staff = None
    if edit_staff_id:
        editing_staff = query_one("""
            SELECT s.id AS staff_id, s.role_title, s.department,
                   u.username, u.full_name, u.email, u.phone, u.role, u.status
            FROM staff s
            JOIN users u ON s.user_id = u.id
            WHERE s.id = %s AND u.role = 'receptionist'
        """, (edit_staff_id,))
        if not editing_staff:
            flash('Receptionist staff member not found.', 'warning')
    staff_members = query_all("""
        SELECT s.*, u.username, u.full_name, u.email, u.phone, u.role, u.status
        FROM staff s
        JOIN users u ON s.user_id = u.id
        ORDER BY s.id DESC
    """)
    return render_template(
        'admin/manage_staff.html',
        staff_members=staff_members,
        editing_staff=editing_staff
    )

@admin_bp.route('/staff/<int:staff_id>/toggle-status', methods=['POST'])
@role_required('admin')
def toggle_staff_status(staff_id):
    try:
        if request.form.get('confirmed') != '1':
            flash('Confirm the staff account status change before continuing.', 'warning')
            return redirect(url_for('admin.manage_staff'))
        with get_db() as (cursor, _):
            cursor.execute("""
                SELECT s.id, s.user_id, u.status, u.role
                FROM staff s
                JOIN users u ON s.user_id = u.id
                WHERE s.id = %s
                FOR UPDATE
            """, (staff_id,))
            staff_member = cursor.fetchone()
            if not staff_member:
                flash('Staff member not found.', 'danger')
                return redirect(url_for('admin.manage_staff'))
            if (
                staff_member['status'] == 'active'
                and (
                    staff_member['role'] == 'admin'
                    or staff_member['user_id'] == session.get('user_id')
                )
            ):
                flash('Your active administrator account cannot be deactivated.', 'danger')
                return redirect(url_for('admin.manage_staff'))

            new_status = (
                'inactive' if staff_member['status'] == 'active' else 'active'
            )
            cursor.execute(
                'UPDATE users SET status = %s WHERE id = %s',
                (new_status, staff_member['user_id'])
            )
            if cursor.rowcount != 1:
                raise RuntimeError('User account status could not be updated.')
            cursor.execute(
                'UPDATE staff SET status = %s WHERE id = %s',
                (new_status, staff_id)
            )
            if cursor.rowcount != 1:
                raise RuntimeError('Staff account status could not be updated.')
        flash(f'Staff status changed to {new_status}.', 'success')
    except Exception:
        current_app.logger.exception('Could not change staff account status.')
        flash('Staff status could not be changed. Please try again.', 'danger')
    return redirect(url_for('admin.manage_staff'))

@admin_bp.route('/wards', methods=['GET', 'POST'])
@role_required('admin')
def manage_wards():
    if request.method == 'POST':
        ward_name = request.form.get('ward_name', '').strip()
        ward_type = request.form.get('ward_type', '').strip()
        try:
            total_beds = int(request.form.get('total_beds', 0) or 0)
        except (ValueError, TypeError):
            total_beds = 0

        if not ward_name or not ward_type or total_beds <= 0:
            flash('Invalid ward details or bed count. Total beds must be a positive integer.', 'danger')
            return redirect(url_for('admin.manage_wards'))

        try:
            ward_id = execute_dml("INSERT INTO wards (ward_name, ward_type, total_beds) VALUES (%s, %s, %s)", (ward_name, ward_type, total_beds))
            
            # Automatically generate beds for this ward
            for i in range(1, total_beds + 1):
                bed_num = f"{ward_name[:3].upper()}-{100 + i}"
                execute_dml("INSERT INTO beds (ward_id, bed_number, status) VALUES (%s, %s, 'available')", (ward_id, bed_num))

            flash(f'Ward "{ward_name}" with {total_beds} beds created successfully.', 'success')
            return redirect(url_for('admin.manage_wards'))
        except Exception as e:
            flash(f'Error creating ward: {str(e)}', 'danger')

    wards = query_all("""
        SELECT w.*, 
               COUNT(b.id) AS generated_beds,
               SUM(CASE WHEN b.status = 'occupied' THEN 1 ELSE 0 END) AS occupied_beds,
               SUM(CASE WHEN b.status = 'available' THEN 1 ELSE 0 END) AS available_beds
        FROM wards w
        LEFT JOIN beds b ON w.id = b.ward_id
        GROUP BY w.id
    """)
    return render_template('admin/manage_wards_beds.html', wards=wards)

@admin_bp.route('/reports')
@role_required('admin')
def reports():
    try:
        appointment_summary = query_all("""
            SELECT status, COUNT(*) AS count
            FROM appointments
            GROUP BY status
        """)

        revenue_summary = query_one("""
            SELECT SUM(total_amount) AS total_billed, SUM(paid_amount) AS total_collected
            FROM bills
        """)

        pharmacy_summary = query_all("""
            SELECT name, stock_quantity, min_stock_threshold
            FROM medicines
            WHERE stock_quantity <= min_stock_threshold
        """)

        return render_template('admin/reports.html',
                               appointment_summary=appointment_summary,
                               revenue_summary=revenue_summary,
                               pharmacy_summary=pharmacy_summary)
    except Exception as e:
        flash(f'Error loading reports: {str(e)}', 'danger')
        return render_template('admin/reports.html')
