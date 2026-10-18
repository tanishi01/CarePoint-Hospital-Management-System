from datetime import datetime

from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, jsonify, session
from database import query_one, query_all, get_db
from utils.appointment_helper import (
    DOCTOR_SCHEDULE_QUERY,
    build_slot_availability,
    has_patient_time_conflict,
    is_slot_available,
    parse_iso_date,
    parse_schedule_time,
)
from utils.auth_helpers import role_required
from utils.notification_helper import create_notification

appointments_bp = Blueprint('appointments', __name__, url_prefix='/appointments')


def _render_booking_page(doctors, patients, today, selected_doctor_id=None, selected_date=''):
    return render_template(
        'appointments/book.html',
        doctors=doctors,
        patients=patients,
        today=today.isoformat(),
        selected_doctor_id=selected_doctor_id,
        selected_date=selected_date
    )


def _appointments_for_day(cursor, doctor_id, booking_date, lock=False):
    cursor.execute("""
        SELECT appointment_time
        FROM appointments
        WHERE doctor_id = %s AND appointment_date = %s AND status != 'cancelled'
        ORDER BY appointment_time
        {lock_clause}
    """.format(lock_clause='FOR UPDATE' if lock else ''), (doctor_id, booking_date))
    return cursor.fetchall()


def _patient_appointments_for_day(cursor, patient_id, booking_date, lock=False):
    cursor.execute("""
        SELECT a.appointment_time, ds.slot_duration_mins, a.status
        FROM appointments a
        LEFT JOIN doctor_schedules ds
          ON ds.doctor_id = a.doctor_id AND ds.day_of_week = %s
        WHERE a.patient_id = %s AND a.appointment_date = %s
          AND a.status != 'cancelled'
        ORDER BY a.appointment_time
        {lock_clause}
    """.format(lock_clause='FOR UPDATE' if lock else ''),
        (booking_date.strftime('%A'), patient_id, booking_date))
    return cursor.fetchall()


@appointments_bp.route('/available-slots')
@role_required(['patient', 'receptionist'])
def available_slots():
    doctor_id = request.args.get('doctor_id', type=int)
    date_str = request.args.get('date', '').strip()

    if not doctor_id or doctor_id <= 0 or not date_str:
        return jsonify({'slots': [], 'error': 'Choose a doctor and date.'}), 400

    booking_date = parse_iso_date(date_str)
    if not booking_date:
        return jsonify({'slots': [], 'error': 'Enter a valid appointment date.'}), 400
    if booking_date < datetime.now().date():
        return jsonify({'slots': [], 'error': 'Appointment date cannot be in the past.'}), 400

    try:
        day_of_week = booking_date.strftime('%A')
        schedules = query_all(DOCTOR_SCHEDULE_QUERY, (doctor_id, day_of_week))
        if not schedules:
            return jsonify({
                'slots': [],
                'schedule': None,
                'message': 'This doctor has no availability on {0}.'.format(day_of_week)
            })

        appointments = query_all("""
            SELECT appointment_time
            FROM appointments
            WHERE doctor_id = %s AND appointment_date = %s AND status != 'cancelled'
        """, (doctor_id, booking_date))
        leaves = query_all("""
            SELECT is_full_day, start_time, end_time
            FROM doctor_leave_requests
            WHERE doctor_id = %s AND leave_date = %s AND status = 'approved'
        """, (doctor_id, booking_date))
        slots, schedule = build_slot_availability(
            schedules, appointments, booking_date, datetime.now(), leaves
        )
        message = None if any(s['status'] == 'available' for s in slots) else (
            'No available times for this doctor on the selected date.'
        )
        return jsonify({'slots': slots, 'schedule': schedule, 'message': message})
    except Exception:
        current_app.logger.exception('Unable to load appointment availability.')
        return jsonify({
            'slots': [],
            'error': 'Availability could not be loaded. Please try again.'
        }), 500


@appointments_bp.route('/book', methods=['GET', 'POST'])
@role_required(['patient', 'receptionist'])
def book_appointment():
    now = datetime.now()
    today = now.date()
    requested_doctor_id = request.args.get('doctor_id', type=int)
    has_requested_doctor_id = 'doctor_id' in request.args
    try:
        doctors = query_all("""
            SELECT d.id, u.full_name, d.department, d.specialization, d.consulting_fee
            FROM doctors d
            JOIN users u ON d.user_id = u.id
            WHERE d.status = 'active' AND u.status = 'active'
            ORDER BY d.department, u.full_name
        """)

        patients = []
        if session.get('role') == 'receptionist':
            patients = query_all("""
                SELECT p.id, u.full_name, u.phone
                FROM patients p
                JOIN users u ON p.user_id = u.id
                WHERE u.status = 'active'
                ORDER BY u.full_name
            """)
    except Exception:
        current_app.logger.exception('Unable to load appointment booking form.')
        flash('Appointment booking is temporarily unavailable. Please try again.', 'danger')
        return _render_booking_page([], [], today)

    active_doctor_ids = {doctor['id'] for doctor in doctors}
    if request.method == 'GET' and has_requested_doctor_id:
        if requested_doctor_id is None or requested_doctor_id not in active_doctor_ids:
            flash('That doctor is unavailable. Choose an active doctor to continue.', 'warning')
            requested_doctor_id = None

    if request.method == 'POST':
        doctor_id = request.form.get('doctor_id', type=int)
        appointment_date = request.form.get('appointment_date', '').strip()
        appointment_time = request.form.get('appointment_time', '').strip()
        reason = request.form.get('reason', '').strip()

        if session.get('role') == 'patient':
            patient_id = None
        else:
            patient_id = request.form.get('patient_id', type=int)

        if session.get('role') == 'receptionist' and not patient_id:
            flash('Valid patient record is required for booking.', 'danger')
            return _render_booking_page(
                doctors, patients, today, selected_doctor_id=requested_doctor_id
            )

        if not doctor_id or doctor_id <= 0 or not all([appointment_date, appointment_time, reason]):
            flash('All booking fields are required.', 'danger')
            return _render_booking_page(doctors, patients, today)
        if doctor_id not in active_doctor_ids:
            flash('Select a valid active doctor.', 'warning')
            return _render_booking_page(doctors, patients, today)

        booking_date = parse_iso_date(appointment_date)
        booking_time = parse_schedule_time(appointment_time)
        if not booking_date:
            flash('Enter a valid appointment date.', 'danger')
            return _render_booking_page(
                doctors, patients, today, selected_doctor_id=requested_doctor_id
            )
        if not booking_time or booking_time.second or booking_time.microsecond:
            flash('Select a valid appointment time.', 'danger')
            return _render_booking_page(doctors, patients, today)
        if booking_date < today:
            flash('Appointment date cannot be in the past.', 'danger')
            return _render_booking_page(doctors, patients, today)
        if datetime.combine(booking_date, booking_time) <= now:
            flash('Appointment time must be in the future.', 'danger')
            return _render_booking_page(doctors, patients, today)

        patient_user_id = None
        try:
            if session.get('role') == 'patient':
                user_id = session.get('user_id')
                patient = query_one("""
                    SELECT p.id FROM patients p
                    JOIN users u ON p.user_id = u.id
                    WHERE p.user_id = %s AND u.status = 'active'
                """, (user_id,))
                if not patient:
                    flash('Valid active patient record is required for booking.', 'danger')
                    return _render_booking_page(doctors, patients, today)
                patient_id = patient['id']
                session['patient_id'] = patient_id
                patient_user_id = user_id
            else:
                patient = query_one("""
                    SELECT p.id, u.id AS user_id
                    FROM patients p
                    JOIN users u ON p.user_id = u.id
                    WHERE p.id = %s AND u.status = 'active'
                """, (patient_id,))
                if not patient:
                    flash('Select a valid active patient.', 'danger')
                    return _render_booking_page(doctors, patients, today)
                patient_user_id = patient['user_id']

            day_of_week = booking_date.strftime('%A')
            with get_db() as (cursor, _):
                # Lock the patient first so concurrent bookings for that
                # patient serialize even when they select different doctors.
                cursor.execute(
                    'SELECT id FROM patients WHERE id = %s FOR UPDATE',
                    (patient_id,)
                )
                if not cursor.fetchone():
                    flash('Valid active patient record is required for booking.', 'danger')
                    return _render_booking_page(doctors, patients, today)

                # Leave approvals lock this same row before cancelling bookings.
                cursor.execute(
                    'SELECT id FROM doctors WHERE id = %s FOR UPDATE',
                    (doctor_id,)
                )
                cursor.fetchone()
                cursor.execute(
                    DOCTOR_SCHEDULE_QUERY + ' FOR UPDATE',
                    (doctor_id, day_of_week)
                )
                schedules = cursor.fetchall()
                appointments = _appointments_for_day(
                    cursor, doctor_id, booking_date, lock=True
                )
                cursor.execute("""
                    SELECT is_full_day, start_time, end_time
                    FROM doctor_leave_requests
                    WHERE doctor_id = %s AND leave_date = %s
                      AND status = 'approved'
                    FOR UPDATE
                """, (doctor_id, booking_date))
                leaves = cursor.fetchall()
                if not is_slot_available(
                    schedules, appointments, booking_date, booking_time,
                    datetime.now(), leaves
                ):
                    flash(
                        'That slot is no longer available. Please choose another time.',
                        'warning'
                    )
                    return _render_booking_page(
                        doctors, patients, today, doctor_id, appointment_date
                    )

                patient_appointments = _patient_appointments_for_day(
                    cursor, patient_id, booking_date, lock=True
                )
                appointment_duration = schedules[0]['slot_duration_mins']
                if has_patient_time_conflict(
                    patient_appointments, booking_time, appointment_duration
                ):
                    flash(
                        'You already have an appointment that overlaps this time. '
                        'Please choose a different time.',
                        'warning'
                    )
                    return _render_booking_page(
                        doctors, patients, today, doctor_id, appointment_date
                    )

                doctor_user_id = schedules[0]['doctor_user_id']
                cursor.execute("""
                    INSERT INTO appointments
                        (patient_id, doctor_id, appointment_date, appointment_time, status, reason)
                    VALUES (%s, %s, %s, %s, 'pending', %s)
                """, (
                    patient_id, doctor_id, booking_date,
                    booking_time.strftime('%H:%M:%S'), reason
                ))
                app_id = cursor.lastrowid

            create_notification(
                doctor_user_id,
                f"New appointment request #{app_id} booked for {booking_date} at {booking_time.strftime('%H:%M')}."
            )

            if session.get('role') == 'receptionist':
                create_notification(
                    patient_user_id,
                    f"An appointment has been scheduled for you on {booking_date} at {booking_time.strftime('%H:%M')}."
                )

            flash('Appointment request booked successfully! Awaiting doctor approval.', 'success')
            if session.get('role') == 'patient':
                return redirect(url_for('patient.my_appointments'))
            return redirect(url_for('receptionist.manage_appointments'))

        except Exception:
            current_app.logger.exception('Appointment booking failed.')
            flash(
                'Booking failed because of a database or availability error. Please try again.',
                'danger'
            )

    return _render_booking_page(
        doctors, patients, today, selected_doctor_id=requested_doctor_id
    )
