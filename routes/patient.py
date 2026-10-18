from flask import Blueprint, render_template, session, flash, request
from database import query_one, query_all
from utils.auth_helpers import role_required

patient_bp = Blueprint('patient', __name__, url_prefix='/patient')

def get_current_patient_id():
    """Resolve the patient record from the authenticated user's database ID."""
    user_id = session.get('user_id')
    if not user_id:
        return None

    patient = query_one("SELECT id FROM patients WHERE user_id = %s", (user_id,))
    patient_id = patient['id'] if patient else None
    session['patient_id'] = patient_id
    return patient_id

@patient_bp.route('/dashboard')
@role_required('patient')
def dashboard():
    user_id = session.get('user_id')

    try:
        patient_id = get_current_patient_id()
        upcoming_appointments = query_all("""
            SELECT a.*, d_u.full_name AS doctor_name, d.department, d.specialization, d.room_number
            FROM appointments a
            JOIN doctors d ON a.doctor_id = d.id
            JOIN users d_u ON d.user_id = d_u.id
            WHERE a.patient_id = %s AND a.appointment_date >= CURRENT_DATE() AND a.status IN ('pending', 'approved', 'rescheduled')
            ORDER BY a.appointment_date ASC, a.appointment_time ASC
        """, (patient_id,))

        notifications = query_all("""
            SELECT * FROM notifications 
            WHERE user_id = %s 
            ORDER BY created_at DESC LIMIT 5
        """, (user_id,))

        pending_bills = query_all("""
            SELECT * FROM bills 
            WHERE patient_id = %s AND status != 'paid'
        """, (patient_id,))

        return render_template('patient/dashboard.html',
                               upcoming_appointments=upcoming_appointments,
                               notifications=notifications,
                               pending_bills=pending_bills)
    except Exception as e:
        flash(f'Error loading patient dashboard: {str(e)}', 'danger')
        return render_template('patient/dashboard.html')

@patient_bp.route('/doctors')
@role_required('patient')
def view_doctors():
    search_query = request.args.get('search', '').strip()
    department = request.args.get('department', '').strip()
    specialization = request.args.get('specialization', '').strip()

    try:
        sql = """
            SELECT d.*, u.full_name, u.email, u.phone
            FROM doctors d
            JOIN users u ON d.user_id = u.id
            WHERE d.status = 'active' AND u.status = 'active'
        """
        params = []

        if search_query:
            sql += " AND (u.full_name LIKE %s OR d.specialization LIKE %s OR d.department LIKE %s)"
            wildcard = f"%{search_query}%"
            params.extend([wildcard, wildcard, wildcard])

        if department:
            sql += " AND d.department = %s"
            params.append(department)

        if specialization:
            sql += " AND d.specialization = %s"
            params.append(specialization)

        sql += " ORDER BY d.department, u.full_name"
        doctors = query_all(sql, tuple(params) if params else None)

        departments = query_all("SELECT DISTINCT department FROM doctors WHERE status = 'active' ORDER BY department")
        specializations = query_all("SELECT DISTINCT specialization FROM doctors WHERE status = 'active' ORDER BY specialization")

        return render_template('patient/doctors_list.html',
                               doctors=doctors,
                               departments=departments,
                               specializations=specializations,
                               search_query=search_query,
                               selected_department=department,
                               selected_specialization=specialization)
    except Exception as e:
        flash(f'Error loading doctor directory: {str(e)}', 'danger')
        return render_template('patient/doctors_list.html', doctors=[])

@patient_bp.route('/appointments')
@role_required('patient')
def my_appointments():
    try:
        patient_id = get_current_patient_id()
        appointments = query_all("""
            SELECT a.*, d_u.full_name AS doctor_name, d.department, d.specialization, d.room_number
            FROM appointments a
            JOIN doctors d ON a.doctor_id = d.id
            JOIN users d_u ON d.user_id = d_u.id
            WHERE a.patient_id = %s
            ORDER BY a.appointment_date DESC, a.appointment_time DESC
        """, (patient_id,))
        return render_template('patient/my_appointments.html', appointments=appointments)
    except Exception as e:
        flash(f'Error loading appointments: {str(e)}', 'danger')
        return render_template('patient/my_appointments.html')

@patient_bp.route('/records')
@role_required('patient')
def my_records():
    try:
        patient_id = get_current_patient_id()
        records = query_all("""
            SELECT mr.*, a.appointment_date, d_u.full_name AS doctor_name, d.department
            FROM medical_records mr
            JOIN appointments a ON mr.appointment_id = a.id
            JOIN doctors d ON mr.doctor_id = d.id
            JOIN users d_u ON d.user_id = d_u.id
            WHERE mr.patient_id = %s
            ORDER BY mr.consultation_date DESC
        """, (patient_id,))

        # Fetch prescription details for each medical record
        for record in records:
            prescription = query_one("SELECT id FROM prescriptions WHERE medical_record_id = %s", (record['id'],))
            if prescription:
                items = query_all("""
                    SELECT pi.*, m.name AS medicine_name, m.generic_name
                    FROM prescription_items pi
                    JOIN medicines m ON pi.medicine_id = m.id
                    WHERE pi.prescription_id = %s
                """, (prescription['id'],))
                record['prescription_items'] = items
            else:
                record['prescription_items'] = []

        return render_template('patient/my_records.html', records=records)
    except Exception as e:
        flash(f'Error loading medical records: {str(e)}', 'danger')
        return render_template('patient/my_records.html')

@patient_bp.route('/bills')
@role_required('patient')
def my_bills():
    try:
        patient_id = get_current_patient_id()
        bills = query_all("""
            SELECT b.*, 
                   (SELECT COUNT(*) FROM payments p WHERE p.bill_id = b.id) AS payment_count
            FROM bills b
            WHERE b.patient_id = %s
            ORDER BY b.created_at DESC
        """, (patient_id,))

        for bill in bills:
            payments = query_all("SELECT * FROM payments WHERE bill_id = %s ORDER BY payment_date DESC", (bill['id'],))
            bill['payments'] = payments

        return render_template('patient/my_bills.html', bills=bills)
    except Exception as e:
        flash(f'Error loading billing records: {str(e)}', 'danger')
        return render_template('patient/my_bills.html')
