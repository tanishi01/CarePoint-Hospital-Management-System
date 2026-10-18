from datetime import date, datetime

from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session
from database import query_one, query_all, execute_dml, get_db
from utils.appointment_helper import (
    DOCTOR_SCHEDULE_QUERY,
    build_slot_availability,
    has_patient_time_conflict,
    parse_iso_date,
    parse_schedule_time,
)
from utils.auth_helpers import role_required
from utils.leave_helper import (
    is_leave_schema_error,
    is_valid_partial_leave,
    leave_overlaps_existing,
)
from utils.notification_helper import create_notification

doctor_bp = Blueprint('doctor', __name__, url_prefix='/doctor')

def get_current_doctor_id():
    """Retrieve doctor_id from session or database fallback."""
    doctor_id = session.get('doctor_id')
    if not doctor_id:
        doctor = query_one("SELECT id FROM doctors WHERE user_id = %s", (session.get('user_id'),))
        if doctor:
            doctor_id = doctor['id']
            session['doctor_id'] = doctor_id
    return doctor_id


@doctor_bp.route('/leave', methods=['GET', 'POST'])
@role_required('doctor')
def leave_requests():
    doctor_id = get_current_doctor_id()
    user_id = session.get('user_id')
    try:
        doctor_record = query_one(
            'SELECT id FROM doctors WHERE id = %s AND user_id = %s',
            (doctor_id, user_id)
        )
        if not doctor_record:
            flash('Your doctor profile could not be verified.', 'danger')
            return redirect(url_for('doctor.dashboard'))

        if request.method == 'POST':
            leave_date_value = request.form.get('leave_date', '').strip()
            leave_date = parse_iso_date(leave_date_value)
            leave_kind = request.form.get('leave_kind', '').strip()
            reason = request.form.get('reason', '').strip()
            start_value = request.form.get('start_time', '').strip()
            end_value = request.form.get('end_time', '').strip()

            if not leave_date or leave_date < date.today():
                flash('Choose a valid leave date that is today or later.', 'danger')
                return redirect(url_for('doctor.leave_requests'))
            if leave_kind not in ('full_day', 'partial'):
                flash('Choose a full working day or a specific time range.', 'danger')
                return redirect(url_for('doctor.leave_requests'))
            if not reason or len(reason) > 2000:
                flash('A reason of 1–2000 characters is required.', 'danger')
                return redirect(url_for('doctor.leave_requests'))
            if leave_kind == 'partial':
                start_time = parse_schedule_time(start_value)
                end_time = parse_schedule_time(end_value)
                if (
                    not start_time or not end_time
                    or start_time.second or start_time.microsecond
                    or end_time.second or end_time.microsecond
                ):
                    flash('Enter valid leave start and end times.', 'danger')
                    return redirect(url_for('doctor.leave_requests'))
            else:
                start_time = end_time = None

            with get_db() as (cursor, _):
                cursor.execute("""
                    SELECT id FROM doctors
                    WHERE id = %s AND user_id = %s
                    FOR UPDATE
                """, (doctor_id, user_id))
                if not cursor.fetchone():
                    flash('Your doctor profile could not be verified.', 'danger')
                    return redirect(url_for('doctor.dashboard'))

                cursor.execute("""
                    SELECT start_time, end_time, break_start, break_end
                    FROM doctor_schedules
                    WHERE doctor_id = %s AND day_of_week = %s
                    FOR UPDATE
                """, (doctor_id, leave_date.strftime('%A')))
                schedule = cursor.fetchone()
                if not schedule:
                    flash('No working schedule is registered for that date.', 'warning')
                    return redirect(url_for('doctor.leave_requests'))
                if leave_kind == 'partial' and not is_valid_partial_leave(
                    start_time, end_time, schedule
                ):
                    flash(
                        'Partial leave must be inside working hours and outside the scheduled break.',
                        'danger'
                    )
                    return redirect(url_for('doctor.leave_requests'))

                cursor.execute("""
                    SELECT is_full_day, start_time, end_time
                    FROM doctor_leave_requests
                    WHERE doctor_id = %s AND leave_date = %s
                      AND status IN ('pending', 'approved')
                    FOR UPDATE
                """, (doctor_id, leave_date))
                proposed_leave = {
                    'is_full_day': leave_kind == 'full_day',
                    'start_time': start_time.strftime('%H:%M:%S') if start_time else None,
                    'end_time': end_time.strftime('%H:%M:%S') if end_time else None
                }
                if any(
                    leave_overlaps_existing(proposed_leave, existing)
                    for existing in cursor.fetchall()
                ):
                    flash(
                        'An overlapping pending or approved leave request already exists.',
                        'warning'
                    )
                    return redirect(url_for('doctor.leave_requests'))

                cursor.execute("""
                    INSERT INTO doctor_leave_requests
                        (doctor_id, leave_date, is_full_day, start_time, end_time, reason)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    doctor_id, leave_date, leave_kind == 'full_day',
                    proposed_leave['start_time'], proposed_leave['end_time'], reason
                ))
            flash('Emergency leave request submitted for admin review.', 'success')
            return redirect(url_for('doctor.leave_requests'))

        requests = query_all("""
            SELECT id, leave_date, is_full_day, start_time, end_time, reason,
                   status, rejection_reason, cancellation_reason, submitted_at,
                   reviewed_at
            FROM doctor_leave_requests
            WHERE doctor_id = %s
            ORDER BY submitted_at DESC
        """, (doctor_id,))
        return render_template(
            'doctor/leave_requests.html',
            leave_requests=requests,
            today=date.today().isoformat()
        )
    except Exception as error:
        current_app.logger.exception('Could not load or submit doctor leave request.')
        if is_leave_schema_error(error):
            flash(
                'Emergency Leave database setup is incomplete. Apply '
                '20261017_create_doctor_leave_requests.sql and '
                '20261017_add_appointment_cancellation_reason.sql to the configured MySQL database.',
                'danger'
            )
        else:
            flash('Leave requests are temporarily unavailable. Please try again.', 'danger')
        return redirect(url_for('doctor.dashboard'))


@doctor_bp.route('/leave/<int:leave_id>/cancel', methods=['POST'])
@role_required('doctor')
def cancel_leave_request(leave_id):
    doctor_id = get_current_doctor_id()
    user_id = session.get('user_id')
    try:
        with get_db() as (cursor, _):
            cursor.execute("""
                SELECT id FROM doctors
                WHERE id = %s AND user_id = %s
                FOR UPDATE
            """, (doctor_id, user_id))
            if not cursor.fetchone():
                flash('Your doctor profile could not be verified.', 'danger')
                return redirect(url_for('doctor.leave_requests'))

            cursor.execute("""
                SELECT id
                FROM doctor_leave_requests
                WHERE id = %s AND doctor_id = %s AND status = 'pending'
                FOR UPDATE
            """, (leave_id, doctor_id))
            if not cursor.fetchone():
                flash(
                    'This leave request was not found, is not yours, or is no longer pending.',
                    'warning'
                )
                return redirect(url_for('doctor.leave_requests'))

            cursor.execute("""
                UPDATE doctor_leave_requests
                SET status = 'cancelled'
                WHERE id = %s AND doctor_id = %s AND status = 'pending'
            """, (leave_id, doctor_id))
            if cursor.rowcount != 1:
                flash(
                    'This leave request was not found, is not yours, or is no longer pending.',
                    'warning'
                )
                return redirect(url_for('doctor.leave_requests'))
        flash('Pending leave request cancelled.', 'success')
    except Exception as error:
        current_app.logger.exception('Could not cancel doctor leave request.')
        if is_leave_schema_error(error):
            flash(
                'The configured database does not support cancelled leave requests. Apply '
                '20261017_add_cancelled_leave_status.sql.',
                'danger'
            )
        else:
            flash('Leave request could not be cancelled. Please try again.', 'danger')
    return redirect(url_for('doctor.leave_requests'))


@doctor_bp.route('/dashboard')
@role_required('doctor')
def dashboard():
    doctor_id = get_current_doctor_id()

    try:
        today_appointments = query_all("""
            SELECT a.*, p_u.full_name AS patient_name, p_u.phone, p.gender, p.blood_group, TIMESTAMPDIFF(YEAR, p.dob, CURDATE()) AS age
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN users p_u ON p.user_id = p_u.id
            WHERE a.doctor_id = %s AND a.appointment_date = CURRENT_DATE()
            ORDER BY a.appointment_time ASC
        """, (doctor_id,))

        pending_approvals = query_all("""
            SELECT a.*, p_u.full_name AS patient_name
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN users p_u ON p.user_id = p_u.id
            WHERE a.doctor_id = %s AND a.status = 'pending'
            ORDER BY a.appointment_date ASC
        """, (doctor_id,))

        return render_template('doctor/dashboard.html',
                               today_appointments=today_appointments,
                               pending_approvals=pending_approvals)
    except Exception as e:
        flash('Error loading doctor dashboard.', 'danger')
        print(f"[Doctor Dashboard Error] {e}")
        return render_template('doctor/dashboard.html')

@doctor_bp.route('/appointments')
@role_required('doctor')
def appointments():
    doctor_id = get_current_doctor_id()
    try:
        all_appointments = query_all("""
            SELECT a.*, p_u.full_name AS patient_name, p_u.phone, p.gender, TIMESTAMPDIFF(YEAR, p.dob, CURDATE()) AS age
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN users p_u ON p.user_id = p_u.id
            WHERE a.doctor_id = %s
            ORDER BY a.appointment_date DESC, a.appointment_time DESC
        """, (doctor_id,))
        return render_template(
            'doctor/appointment_list.html',
            appointments=all_appointments,
            today=date.today().isoformat()
        )
    except Exception as e:
        flash('Error loading appointments list.', 'danger')
        print(f"[Doctor Appointments Error] {e}")
        return render_template(
            'doctor/appointment_list.html',
            today=date.today().isoformat()
        )

@doctor_bp.route('/appointments/<int:appointment_id>/action', methods=['POST'])
@role_required('doctor')
def appointment_action(appointment_id):
    action = request.form.get('action', '').strip() # approve, reschedule, cancel
    doctor_id = get_current_doctor_id()

    try:
        appointment = query_one("""
            SELECT a.*, p.user_id AS patient_user_id 
            FROM appointments a 
            JOIN patients p ON a.patient_id = p.id 
            WHERE a.id = %s AND a.doctor_id = %s
        """, (appointment_id, doctor_id))

        if not appointment:
            flash('Appointment record not found or access denied.', 'danger')
            return redirect(url_for('doctor.appointments'))

        if action == 'approve':
            if appointment['status'] != 'pending':
                flash('Only pending appointments can be approved.', 'warning')
                return redirect(url_for('doctor.appointments'))

            with get_db() as (cursor, _):
                cursor.execute("""
                    SELECT status FROM appointments
                    WHERE id = %s AND doctor_id = %s
                    FOR UPDATE
                """, (appointment_id, doctor_id))
                locked_appointment = cursor.fetchone()
                if not locked_appointment or locked_appointment['status'] != 'pending':
                    flash('This appointment is no longer pending.', 'warning')
                    return redirect(url_for('doctor.appointments'))
                cursor.execute("""
                    UPDATE appointments SET status = 'approved'
                    WHERE id = %s AND doctor_id = %s AND status = 'pending'
                """, (appointment_id, doctor_id))
                if cursor.rowcount != 1:
                    raise RuntimeError('Appointment could not be approved.')

            create_notification(
                appointment['patient_user_id'],
                f"Your appointment on {appointment['appointment_date']} at {appointment['appointment_time']} has been APPROVED by the doctor."
            )
            flash('Appointment approved successfully.', 'success')

        elif action == 'reschedule':
            new_date_value = request.form.get('new_date', '').strip()
            new_time_value = request.form.get('new_time', '').strip()
            new_date = parse_iso_date(new_date_value)
            new_time = parse_schedule_time(new_time_value)
            now = datetime.now()

            if not new_date or not new_time:
                flash('Please specify both new date and new time to reschedule.', 'warning')
                return redirect(url_for('doctor.appointments'))
            if new_time.second or new_time.microsecond:
                flash('Select a scheduled appointment time.', 'warning')
                return redirect(url_for('doctor.appointments'))
            if new_date < now.date():
                flash('Appointment date cannot be in the past.', 'warning')
                return redirect(url_for('doctor.appointments'))
            if datetime.combine(new_date, new_time) <= now:
                flash('Appointment time must be in the future.', 'warning')
                return redirect(url_for('doctor.appointments'))

            day_of_week = new_date.strftime('%A')
            with get_db() as (cursor, _):
                cursor.execute(
                    'SELECT id FROM patients WHERE id = %s FOR UPDATE',
                    (appointment['patient_id'],)
                )
                if not cursor.fetchone():
                    flash('Appointment patient record could not be verified.', 'warning')
                    return redirect(url_for('doctor.appointments'))
                cursor.execute(
                    'SELECT id FROM doctors WHERE id = %s FOR UPDATE',
                    (doctor_id,)
                )
                cursor.execute("""
                    SELECT id, patient_id, status
                    FROM appointments
                    WHERE id = %s AND doctor_id = %s
                    FOR UPDATE
                """, (appointment_id, doctor_id))
                locked_appointment = cursor.fetchone()
                if (
                    not locked_appointment
                    or locked_appointment['patient_id'] != appointment['patient_id']
                    or locked_appointment['status'] not in ('pending', 'approved', 'rescheduled')
                ):
                    flash('This appointment can no longer be rescheduled.', 'warning')
                    return redirect(url_for('doctor.appointments'))

                cursor.execute(
                    DOCTOR_SCHEDULE_QUERY + ' FOR UPDATE',
                    (doctor_id, day_of_week)
                )
                schedules = cursor.fetchall()
                if not schedules:
                    flash("The doctor's schedule is unavailable for that date.", 'warning')
                    return redirect(url_for('doctor.appointments'))
                cursor.execute("""
                    SELECT is_full_day, start_time, end_time
                    FROM doctor_leave_requests
                    WHERE doctor_id = %s AND leave_date = %s
                      AND status = 'approved'
                    FOR UPDATE
                """, (doctor_id, new_date))
                leaves = cursor.fetchall()
                cursor.execute("""
                    SELECT appointment_time
                    FROM appointments
                    WHERE doctor_id = %s AND appointment_date = %s
                      AND status != 'cancelled' AND id != %s
                    FOR UPDATE
                """, (doctor_id, new_date, appointment_id))
                booked_appointments = cursor.fetchall()
                slots, _ = build_slot_availability(
                    schedules, booked_appointments, new_date, datetime.now(), leaves
                )
                if not any(
                    slot['time'] == new_time.strftime('%H:%M')
                    and slot['status'] == 'available'
                    for slot in slots
                ):
                    flash("That time is unavailable. Choose a future time from the doctor's schedule.", 'warning')
                    return redirect(url_for('doctor.appointments'))

                cursor.execute("""
                    SELECT a.appointment_time, ds.slot_duration_mins, a.status
                    FROM appointments a
                    LEFT JOIN doctor_schedules ds
                      ON ds.doctor_id = a.doctor_id AND ds.day_of_week = %s
                    WHERE a.patient_id = %s AND a.appointment_date = %s
                      AND a.status != 'cancelled' AND a.id != %s
                    FOR UPDATE
                """, (
                    day_of_week, appointment['patient_id'], new_date, appointment_id
                ))
                patient_appointments = cursor.fetchall()
                if has_patient_time_conflict(
                    patient_appointments, new_time, schedules[0]['slot_duration_mins']
                ):
                    flash('The patient already has an overlapping appointment.', 'warning')
                    return redirect(url_for('doctor.appointments'))

                cursor.execute("""
                    UPDATE appointments
                    SET appointment_date = %s, appointment_time = %s, status = 'rescheduled'
                    WHERE id = %s AND doctor_id = %s
                      AND status IN ('pending', 'approved', 'rescheduled')
                """, (
                    new_date, new_time.strftime('%H:%M:%S'), appointment_id, doctor_id
                ))
                if cursor.rowcount != 1:
                    raise RuntimeError('Appointment could not be rescheduled.')

            create_notification(
                appointment['patient_user_id'],
                f"Your appointment has been RESCHEDULED to {new_date} at {new_time.strftime('%H:%M')}."
            )
            flash('Appointment rescheduled successfully.', 'success')

        elif action == 'cancel':
            if appointment['status'] not in ('pending', 'approved', 'rescheduled'):
                flash('This appointment can no longer be cancelled.', 'warning')
                return redirect(url_for('doctor.appointments'))

            with get_db() as (cursor, _):
                cursor.execute("""
                    UPDATE appointments SET status = 'cancelled'
                    WHERE id = %s AND doctor_id = %s
                      AND status IN ('pending', 'approved', 'rescheduled')
                """, (appointment_id, doctor_id))
                if cursor.rowcount != 1:
                    raise RuntimeError('Appointment could not be cancelled.')
            create_notification(
                appointment['patient_user_id'],
                f"Your appointment on {appointment['appointment_date']} has been CANCELLED by the doctor."
            )
            flash('Appointment cancelled.', 'info')

    except Exception:
        current_app.logger.exception('Could not process doctor appointment action.')
        flash('Could not process appointment action. Please try again.', 'danger')

    return redirect(url_for('doctor.appointments'))

@doctor_bp.route('/consultation/<int:appointment_id>', methods=['GET', 'POST'])
@role_required('doctor')
def consultation(appointment_id):
    doctor_id = get_current_doctor_id()
    appointment = query_one("""
        SELECT a.*, p.id AS patient_id, p.gender, p.blood_group, TIMESTAMPDIFF(YEAR, p.dob, CURDATE()) AS age,
               p_u.full_name AS patient_name, p_u.phone
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        JOIN users p_u ON p.user_id = p_u.id
        WHERE a.id = %s AND a.doctor_id = %s
    """, (appointment_id, doctor_id))

    if not appointment:
        flash('Appointment not found or unauthorized access.', 'danger')
        return redirect(url_for('doctor.dashboard'))

    medicines = query_all("SELECT * FROM medicines WHERE stock_quantity > 0 ORDER BY name ASC")
    existing_record = query_one("SELECT * FROM medical_records WHERE appointment_id = %s", (appointment_id,))
    existing_items = []
    if existing_record:
        existing_prescription = query_one("SELECT id FROM prescriptions WHERE medical_record_id = %s", (existing_record['id'],))
        if existing_prescription:
            existing_items = query_all("SELECT * FROM prescription_items WHERE prescription_id = %s", (existing_prescription['id'],))

    if request.method == 'POST':
        diagnosis = request.form.get('diagnosis', '').strip()
        treatment_notes = request.form.get('treatment_notes', '').strip()
        
        # Lists for prescription items
        medicine_ids = request.form.getlist('medicine_id[]')
        dosages = request.form.getlist('dosage[]')
        durations = request.form.getlist('duration[]')
        frequencies = request.form.getlist('frequency[]')
        quantities = request.form.getlist('quantity[]')

        if not diagnosis or not treatment_notes:
            flash('Diagnosis and Treatment Notes are required fields.', 'danger')
            return render_template('doctor/consultation.html', appointment=appointment, medicines=medicines, existing_record=existing_record, existing_items=existing_items)

        try:
            with get_db() as (cursor, conn):
                if existing_record:
                    record_id = existing_record['id']
                    cursor.execute("""
                        UPDATE medical_records SET diagnosis = %s, treatment_notes = %s WHERE id = %s
                    """, (diagnosis, treatment_notes, record_id))
                else:
                    cursor.execute("""
                        INSERT INTO medical_records (appointment_id, patient_id, doctor_id, diagnosis, treatment_notes)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (appointment_id, appointment['patient_id'], doctor_id, diagnosis, treatment_notes))
                    record_id = cursor.lastrowid
                    cursor.execute("UPDATE appointments SET status = 'completed' WHERE id = %s", (appointment_id,))

                # Check if prescription already exists for this medical record
                cursor.execute("SELECT id FROM prescriptions WHERE medical_record_id = %s", (record_id,))
                presc_row = cursor.fetchone()

                # Revert previously decremented stock for old items if updating existing prescription
                if presc_row:
                    prescription_id = presc_row['id']
                    cursor.execute("SELECT medicine_id, quantity FROM prescription_items WHERE prescription_id = %s", (prescription_id,))
                    old_items = cursor.fetchall()
                    for item in old_items:
                        cursor.execute("UPDATE medicines SET stock_quantity = stock_quantity + %s WHERE id = %s", (item['quantity'], item['medicine_id']))
                    cursor.execute("DELETE FROM prescription_items WHERE prescription_id = %s", (prescription_id,))
                else:
                    prescription_id = None

                # Process new prescription items if provided
                valid_meds = [m for m in medicine_ids if m]
                if valid_meds:
                    if not prescription_id:
                        cursor.execute("INSERT INTO prescriptions (medical_record_id) VALUES (%s)", (record_id,))
                        prescription_id = cursor.lastrowid

                    for i in range(len(medicine_ids)):
                        m_id = medicine_ids[i]
                        if not m_id:
                            continue
                        try:
                            qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
                        except (ValueError, TypeError):
                            qty = 1
                        dos = dosages[i] if i < len(dosages) else 'As directed'
                        dur = durations[i] if i < len(durations) else '5 days'
                        freq = frequencies[i] if i < len(frequencies) else '1-0-1'

                        # Fetch price, stock, threshold & name
                        cursor.execute("SELECT unit_price, stock_quantity, min_stock_threshold, name FROM medicines WHERE id = %s", (m_id,))
                        med = cursor.fetchone()
                        if med:
                            new_stock = max(0, med['stock_quantity'] - qty)
                            cursor.execute("UPDATE medicines SET stock_quantity = %s WHERE id = %s", (new_stock, m_id))

                            # Trigger Low Stock Alert notification if stock reaches or drops below min_stock_threshold
                            if new_stock <= med['min_stock_threshold']:
                                cursor.execute("SELECT id FROM users WHERE role = 'admin'")
                                admins = cursor.fetchall()
                                for adm in admins:
                                    cursor.execute("""
                                        INSERT INTO notifications (user_id, message) 
                                        VALUES (%s, %s)
                                    """, (adm['id'], f"Low Stock Alert: Medicine '{med['name']}' has only {new_stock} units left in stock (threshold: {med['min_stock_threshold']})."))

                        cursor.execute("""
                            INSERT INTO prescription_items (prescription_id, medicine_id, dosage, duration, frequency, quantity)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (prescription_id, m_id, dos, dur, freq, qty))

            flash('Consultation notes & Prescription saved successfully!', 'success')
            return redirect(url_for('doctor.dashboard'))
        except Exception as e:
            flash(f'Error saving consultation: {str(e)}', 'danger')

    return render_template('doctor/consultation.html', appointment=appointment, medicines=medicines, existing_record=existing_record, existing_items=existing_items)

@doctor_bp.route('/patient-history/<int:patient_id>')
@role_required('doctor')
def patient_history(patient_id):
    try:
        patient = query_one("""
            SELECT p.*, u.full_name, u.email, u.phone, TIMESTAMPDIFF(YEAR, p.dob, CURDATE()) AS age
            FROM patients p
            JOIN users u ON p.user_id = u.id
            WHERE p.id = %s
        """, (patient_id,))

        medical_history = query_all("""
            SELECT mr.*, a.appointment_date, d_u.full_name AS doctor_name
            FROM medical_records mr
            JOIN appointments a ON mr.appointment_id = a.id
            JOIN doctors d ON mr.doctor_id = d.id
            JOIN users d_u ON d.user_id = d_u.id
            WHERE mr.patient_id = %s
            ORDER BY mr.consultation_date DESC
        """, (patient_id,))

        return render_template('doctor/patient_history.html', patient=patient, medical_history=medical_history)
    except Exception as e:
        flash(f'Error loading patient history: {str(e)}', 'danger')
        return redirect(url_for('doctor.dashboard'))
