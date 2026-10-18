import math

from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash
from database import query_all, get_db
from utils.auth_helpers import role_required

beds_bp = Blueprint('beds', __name__, url_prefix='/beds')

@beds_bp.route('/availability')
@role_required('receptionist')
def bed_availability():
    try:
        wards_beds = query_all("""
            SELECT w.id AS ward_id, w.ward_name, w.ward_type, 
                   b.id AS bed_id, b.bed_number, b.status AS bed_status,
                   ba.id AS allocation_id, ba.admission_date, ba.daily_charge,
                   u.full_name AS patient_name
            FROM wards w
            LEFT JOIN beds b ON w.id = b.ward_id
            LEFT JOIN bed_allocations ba ON b.id = ba.bed_id AND ba.status = 'admitted'
            LEFT JOIN patients p ON ba.patient_id = p.id
            LEFT JOIN users u ON p.user_id = u.id
            ORDER BY w.ward_name, b.bed_number
        """)

        patients = query_all("""
            SELECT p.id, u.full_name, u.phone 
            FROM patients p 
            JOIN users u ON p.user_id = u.id 
            WHERE u.status = 'active'
            ORDER BY u.full_name
        """)

        available_beds = query_all("""
            SELECT b.id, b.bed_number, w.ward_name, w.ward_type 
            FROM beds b 
            JOIN wards w ON b.ward_id = w.id 
            WHERE b.status = 'available'
              AND NOT EXISTS (
                  SELECT 1 FROM bed_allocations ba
                  WHERE ba.bed_id = b.id AND ba.status = 'admitted'
              )
            ORDER BY w.ward_name, b.bed_number
        """)

        allocation_conflicts = query_all("""
            SELECT 'patient' AS conflict_type, COUNT(*) AS conflict_count
            FROM (
                SELECT patient_id
                FROM bed_allocations
                WHERE status = 'admitted'
                GROUP BY patient_id
                HAVING COUNT(*) > 1
            ) patient_conflicts
            UNION ALL
            SELECT 'bed' AS conflict_type, COUNT(*) AS conflict_count
            FROM (
                SELECT bed_id
                FROM bed_allocations
                WHERE status = 'admitted'
                GROUP BY bed_id
                HAVING COUNT(*) > 1
            ) bed_conflicts
        """)
        allocation_conflicts = {
            row['conflict_type']: row['conflict_count']
            for row in allocation_conflicts
        }

        return render_template('admin/beds_availability.html',
                               wards_beds=wards_beds,
                               patients=patients,
                               available_beds=available_beds,
                               allocation_conflicts=allocation_conflicts)
    except Exception as e:
        flash(f'Error loading bed availability map: {str(e)}', 'danger')
        return render_template('admin/beds_availability.html')

@beds_bp.route('/allocate', methods=['POST'])
@role_required('receptionist')
def allocate_bed():
    bed_id = request.form.get('bed_id', type=int)
    patient_id = request.form.get('patient_id', type=int)
    try:
        daily_charge = float(request.form.get('daily_charge', 1000.0) or 1000.0)
    except (ValueError, TypeError):
        daily_charge = 1000.0

    if not bed_id or not patient_id or not math.isfinite(daily_charge) or daily_charge <= 0:
        flash('Bed, Patient, and positive Daily Charge are required.', 'danger')
        return redirect(url_for('beds.bed_availability'))

    try:
        with get_db() as (cursor, conn):
            cursor.execute("""
                SELECT p.id, u.status AS user_status
                FROM patients p
                JOIN users u ON u.id = p.user_id
                WHERE p.id = %s
                FOR UPDATE
            """, (patient_id,))
            patient = cursor.fetchone()
            if not patient or patient['user_status'] != 'active':
                flash('Selected patient is not available for admission.', 'warning')
                return redirect(url_for('beds.bed_availability'))

            cursor.execute("""
                SELECT id
                FROM bed_allocations
                WHERE patient_id = %s AND status = 'admitted'
                FOR UPDATE
            """, (patient_id,))
            if cursor.fetchall():
                flash(
                    'This patient already has an active bed allocation. '
                    'Resolve the existing allocation before assigning another bed.',
                    'warning'
                )
                return redirect(url_for('beds.bed_availability'))

            cursor.execute(
                "SELECT id, status FROM beds WHERE id = %s FOR UPDATE",
                (bed_id,)
            )
            bed = cursor.fetchone()
            if not bed or bed['status'] != 'available':
                flash('Selected bed is not available.', 'warning')
                return redirect(url_for('beds.bed_availability'))

            cursor.execute("""
                SELECT id
                FROM bed_allocations
                WHERE bed_id = %s AND status = 'admitted'
                FOR UPDATE
            """, (bed_id,))
            if cursor.fetchall():
                flash(
                    'Selected bed has an active allocation record despite its availability status. '
                    'Resolve this conflict before allocating the bed.',
                    'danger'
                )
                return redirect(url_for('beds.bed_availability'))

            # Insert allocation record
            cursor.execute("""
                INSERT INTO bed_allocations (bed_id, patient_id, admission_date, daily_charge, status)
                VALUES (%s, %s, NOW(), %s, 'admitted')
            """, (bed_id, patient_id, daily_charge))

            # Update bed status to occupied
            cursor.execute(
                "UPDATE beds SET status = 'occupied' WHERE id = %s AND status = 'available'",
                (bed_id,)
            )
            if cursor.rowcount != 1:
                raise RuntimeError('Bed status changed during allocation.')

        flash('Bed allocated successfully.', 'success')
    except Exception:
        current_app.logger.exception('Bed allocation transaction failed.')
        flash('Bed allocation failed. No allocation was saved.', 'danger')

    return redirect(url_for('beds.bed_availability'))

@beds_bp.route('/discharge/<int:allocation_id>', methods=['POST'])
@role_required('receptionist')
def discharge_patient(allocation_id):
    try:
        with get_db() as (cursor, conn):
            cursor.execute("""
                SELECT bed_id
                FROM bed_allocations
                WHERE id = %s AND status = 'admitted'
            """, (allocation_id,))
            allocation_bed = cursor.fetchone()
            if not allocation_bed:
                flash('Active allocation record not found.', 'danger')
                return redirect(url_for('beds.bed_availability'))

            cursor.execute(
                "SELECT id, status FROM beds WHERE id = %s FOR UPDATE",
                (allocation_bed['bed_id'],)
            )
            bed = cursor.fetchone()
            if not bed or bed['status'] != 'occupied':
                flash(
                    'The bed status does not match its active allocation. '
                    'Resolve this conflict before discharge.',
                    'danger'
                )
                return redirect(url_for('beds.bed_availability'))

            cursor.execute("""
                SELECT id, bed_id
                FROM bed_allocations
                WHERE id = %s AND bed_id = %s AND status = 'admitted'
                FOR UPDATE
            """, (allocation_id, allocation_bed['bed_id']))
            alloc = cursor.fetchone()
            if not alloc:
                flash('Active allocation record changed before discharge.', 'warning')
                return redirect(url_for('beds.bed_availability'))

            cursor.execute("""
                SELECT id
                FROM bed_allocations
                WHERE bed_id = %s AND status = 'admitted'
                FOR UPDATE
            """, (alloc['bed_id'],))
            if len(cursor.fetchall()) != 1:
                flash(
                    'The bed has conflicting active allocation records. '
                    'Resolve the conflict before discharge.',
                    'danger'
                )
                return redirect(url_for('beds.bed_availability'))

            # Update allocation to discharged
            cursor.execute("""
                UPDATE bed_allocations
                SET discharge_date = NOW(), status = 'discharged'
                WHERE id = %s AND bed_id = %s AND status = 'admitted'
            """, (allocation_id, alloc['bed_id']))
            if cursor.rowcount != 1:
                raise RuntimeError('Allocation could not be discharged.')
            
            # Revert bed status to available
            cursor.execute("""
                UPDATE beds SET status = 'available'
                WHERE id = %s AND status = 'occupied'
            """, (alloc['bed_id'],))
            if cursor.rowcount != 1:
                raise RuntimeError('Bed could not be made available.')

        flash('Patient discharged successfully. Bed freed for next allocation.', 'success')
    except Exception:
        current_app.logger.exception('Bed discharge transaction failed.')
        flash('Discharge failed. No allocation or bed status was changed.', 'danger')

    return redirect(url_for('beds.bed_availability'))
