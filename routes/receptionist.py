from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash
from database import query_one, query_all, execute_dml, get_db
from utils.auth_helpers import role_required
from utils.notification_helper import create_notification

receptionist_bp = Blueprint('receptionist', __name__, url_prefix='/receptionist')

@receptionist_bp.route('/dashboard')
@role_required('receptionist')
def dashboard():
    try:
        today_appointments = query_all("""
            SELECT a.*, p_u.full_name AS patient_name, d_u.full_name AS doctor_name, d.room_number
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN users p_u ON p.user_id = p_u.id
            JOIN doctors d ON a.doctor_id = d.id
            JOIN users d_u ON d.user_id = d_u.id
            WHERE a.appointment_date = CURRENT_DATE()
            ORDER BY a.appointment_time ASC
        """)

        pending_checkins = [
            appointment for appointment in today_appointments
            if appointment['status'] in ('approved', 'pending')
        ]
        checked_in = [a for a in today_appointments if a['status'] == 'checked_in']

        recent_bills = query_all("""
            SELECT b.*, u.full_name AS patient_name
            FROM bills b
            JOIN patients p ON b.patient_id = p.id
            JOIN users u ON p.user_id = u.id
            ORDER BY b.created_at DESC LIMIT 5
        """)

        return render_template('receptionist/dashboard.html',
                               today_appointments=today_appointments,
                               pending_checkins=pending_checkins,
                               checked_in=checked_in,
                               recent_bills=recent_bills)
    except Exception:
        current_app.logger.exception('Unable to load receptionist dashboard data.')
        flash('Receptionist dashboard is temporarily unavailable.', 'danger')
        return render_template(
            'receptionist/dashboard.html',
            checkin_data_unavailable=True
        )

@receptionist_bp.route('/appointments')
@role_required('receptionist')
def manage_appointments():
    try:
        appointments = query_all("""
            SELECT a.*, p_u.full_name AS patient_name, p_u.phone,
                   d_u.full_name AS doctor_name, d.department,
                   (a.appointment_date = CURRENT_DATE()
                    AND a.status IN ('pending', 'approved')) AS can_checkin
            FROM appointments a
            JOIN patients p ON a.patient_id = p.id
            JOIN users p_u ON p.user_id = p_u.id
            JOIN doctors d ON a.doctor_id = d.id
            JOIN users d_u ON d.user_id = d_u.id
            ORDER BY a.appointment_date DESC, a.appointment_time DESC
        """)
        return render_template('receptionist/appointments.html', appointments=appointments)
    except Exception as e:
        flash(f'Error loading appointment records: {str(e)}', 'danger')
        return render_template('receptionist/appointments.html')

@receptionist_bp.route('/checkin/<int:appointment_id>', methods=['POST'])
@role_required('receptionist')
def checkin_patient(appointment_id):
    try:
        with get_db() as (cursor, _):
            cursor.execute("""
            SELECT a.*, p.user_id AS patient_user_id, u.full_name AS patient_name, d.user_id AS doctor_user_id
            FROM appointments a 
            JOIN patients p ON a.patient_id = p.id 
            JOIN users u ON p.user_id = u.id
            JOIN doctors d ON a.doctor_id = d.id
            WHERE a.id = %s
              AND a.appointment_date = CURRENT_DATE()
              AND a.status IN ('pending', 'approved')
            FOR UPDATE
            """, (appointment_id,))
            appointment = cursor.fetchone()
            if not appointment:
                flash(
                    'This appointment is not eligible for check-in today.',
                    'warning'
                )
                return redirect(url_for('receptionist.dashboard'))

            cursor.execute("""
                UPDATE appointments
                SET status = 'checked_in'
                WHERE id = %s
                  AND appointment_date = CURRENT_DATE()
                  AND status IN ('pending', 'approved')
            """, (appointment_id,))
            if cursor.rowcount != 1:
                raise RuntimeError('Appointment changed before check-in.')

        create_notification(
            appointment['patient_user_id'],
            "You have been checked in. Please proceed to the doctor's waiting area."
        )
        create_notification(
            appointment['doctor_user_id'],
            f"Patient {appointment['patient_name']} has checked in for appointment "
            f"#{appointment['id']} ({appointment['appointment_time']})."
        )
        flash('Patient checked in successfully. Doctor has been notified.', 'success')
    except Exception:
        current_app.logger.exception('Unable to check in appointment %s.', appointment_id)
        flash('Check-in failed. The appointment status was not changed.', 'danger')
    return redirect(url_for('receptionist.dashboard'))

@receptionist_bp.route('/billing/create', methods=['GET', 'POST'])
@role_required('receptionist')
def create_bill():
    patients = query_all("""
        SELECT p.id, u.full_name, u.phone 
        FROM patients p 
        JOIN users u ON p.user_id = u.id 
        WHERE u.status = 'active'
        ORDER BY u.full_name ASC
    """)
    # Exclude appointments that have already been billed
    appointments = query_all("""
        SELECT a.id, a.patient_id, a.appointment_date, d_u.full_name AS doctor_name, d.consulting_fee
        FROM appointments a
        JOIN doctors d ON a.doctor_id = d.id
        JOIN users d_u ON d.user_id = d_u.id
        WHERE a.status = 'completed'
          AND a.id NOT IN (SELECT appointment_id FROM bills WHERE appointment_id IS NOT NULL)
    """)
    # Available bed allocations for billing link
    bed_allocations = query_all("""
        SELECT ba.id, ba.patient_id, b.bed_number, w.ward_name, ba.daily_charge, ba.status
        FROM bed_allocations ba
        JOIN beds b ON ba.bed_id = b.id
        JOIN wards w ON b.ward_id = w.id
        WHERE ba.id NOT IN (SELECT bed_allocation_id FROM bills WHERE bed_allocation_id IS NOT NULL)
        ORDER BY ba.admission_date DESC
    """)

    if request.method == 'POST':
        patient_id = request.form.get('patient_id')
        appointment_id = request.form.get('appointment_id') or None
        bed_allocation_id = request.form.get('bed_allocation_id') or None

        try:
            consultation_fee = float(request.form.get('consultation_fee', 0.0) or 0.0)
        except (ValueError, TypeError):
            consultation_fee = 0.0
        try:
            test_charges = float(request.form.get('test_charges', 0.0) or 0.0)
        except (ValueError, TypeError):
            test_charges = 0.0
        try:
            bed_charges = float(request.form.get('bed_charges', 0.0) or 0.0)
        except (ValueError, TypeError):
            bed_charges = 0.0
        try:
            medicine_charges = float(request.form.get('medicine_charges', 0.0) or 0.0)
        except (ValueError, TypeError):
            medicine_charges = 0.0

        if not patient_id:
            flash('Patient must be selected.', 'danger')
            return render_template('receptionist/billing_create.html', patients=patients, appointments=appointments, bed_allocations=bed_allocations)

        total_amount = consultation_fee + test_charges + bed_charges + medicine_charges

        if total_amount <= 0:
            flash('Bill total amount must be greater than 0.', 'warning')
            return render_template('receptionist/billing_create.html', patients=patients, appointments=appointments, bed_allocations=bed_allocations)

        try:
            bill_id = execute_dml("""
                INSERT INTO bills (patient_id, appointment_id, bed_allocation_id, consultation_fee, test_charges, bed_charges, medicine_charges, total_amount, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            """, (patient_id, appointment_id, bed_allocation_id, consultation_fee, test_charges, bed_charges, medicine_charges, total_amount))

            flash(f'Invoice #{bill_id} generated successfully for Total Amount ₹{total_amount:.2f}', 'success')
            return redirect(url_for('receptionist.manage_billing'))
        except Exception as e:
            flash(f'Error creating bill: {str(e)}', 'danger')

    return render_template('receptionist/billing_create.html', patients=patients, appointments=appointments, bed_allocations=bed_allocations)

@receptionist_bp.route('/billing')
@role_required('receptionist')
def manage_billing():
    try:
        bills = query_all("""
            SELECT b.*, u.full_name AS patient_name, u.phone
            FROM bills b
            JOIN patients p ON b.patient_id = p.id
            JOIN users u ON p.user_id = u.id
            ORDER BY b.created_at DESC
        """)
        return render_template('receptionist/billing_list.html', bills=bills)
    except Exception as e:
        flash(f'Error loading billing history: {str(e)}', 'danger')
        return render_template('receptionist/billing_list.html')

@receptionist_bp.route('/payment/record', methods=['POST'])
@role_required('receptionist')
def record_payment():
    bill_id = request.form.get('bill_id')
    payment_method = request.form.get('payment_method', 'cash')
    notes = request.form.get('notes', '').strip()

    try:
        amount_paid = float(request.form.get('amount_paid', 0.0) or 0.0)
    except (ValueError, TypeError):
        amount_paid = 0.0

    if not bill_id or amount_paid <= 0:
        flash('Invalid payment amount. Amount must be greater than zero.', 'danger')
        return redirect(url_for('receptionist.manage_billing'))

    try:
        with get_db() as (cursor, conn):
            cursor.execute("SELECT total_amount, paid_amount, status, patient_id FROM bills WHERE id = %s", (bill_id,))
            bill = cursor.fetchone()

            if not bill:
                flash('Bill not found.', 'danger')
                return redirect(url_for('receptionist.manage_billing'))

            total_due = float(bill['total_amount'])
            current_paid = float(bill['paid_amount'])
            remaining_balance = round(total_due - current_paid, 2)

            # Prevent payment on already fully paid bill
            if bill['status'] == 'paid' or remaining_balance <= 0:
                flash(f'Bill #{bill_id} is already fully paid. No additional payments can be recorded.', 'warning')
                return redirect(url_for('receptionist.manage_billing'))

            # Prevent payment amounts greater than remaining bill balance
            if amount_paid > remaining_balance:
                flash(f'Payment amount (₹{amount_paid:.2f}) cannot exceed remaining balance (₹{remaining_balance:.2f}).', 'warning')
                return redirect(url_for('receptionist.manage_billing'))

            new_paid_total = current_paid + amount_paid

            if new_paid_total >= total_due:
                new_status = 'paid'
            elif new_paid_total > 0:
                new_status = 'partially_paid'
            else:
                new_status = 'pending'

            # Record payment entry
            cursor.execute("""
                INSERT INTO payments (bill_id, amount_paid, payment_method, notes)
                VALUES (%s, %s, %s, %s)
            """, (bill_id, amount_paid, payment_method, notes))

            # Update master bill record
            cursor.execute("""
                UPDATE bills SET paid_amount = %s, status = %s WHERE id = %s
            """, (new_paid_total, new_status, bill_id))

            # Notify patient
            cursor.execute("SELECT user_id FROM patients WHERE id = %s", (bill['patient_id'],))
            pat = cursor.fetchone()
            if pat:
                cursor.execute("""
                    INSERT INTO notifications (user_id, message) VALUES (%s, %s)
                """, (pat['user_id'], f"Payment of ₹{amount_paid:.2f} received for Bill #{bill_id}. Status: {new_status.upper()}."))

        flash(f'Payment of ₹{amount_paid:.2f} recorded successfully for Bill #{bill_id}.', 'success')
    except Exception as e:
        flash(f'Error recording payment: {str(e)}', 'danger')

    return redirect(url_for('receptionist.manage_billing'))
