from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session
from database import query_one, query_all, execute_dml, get_db
from utils.auth_helpers import role_required

pharmacy_bp = Blueprint('pharmacy', __name__, url_prefix='/pharmacy')

@pharmacy_bp.route('/inventory', methods=['GET', 'POST'])
@role_required(['admin', 'receptionist', 'doctor'])
def inventory():
    if request.method == 'POST':
        # Enforce business rule: Only Administrators manage pharmacy inventory
        if session.get('role') != 'admin':
            flash('Access denied. Only Administrators can add medicines to inventory.', 'danger')
            return redirect(url_for('pharmacy.inventory'))

        name = request.form.get('name', '').strip()
        generic_name = request.form.get('generic_name', '').strip()
        try:
            unit_price = Decimal(request.form.get('unit_price', ''))
            stock_quantity = int(request.form.get('stock_quantity', 0))
            min_stock_threshold = int(request.form.get('min_stock_threshold', 20))
        except (InvalidOperation, ValueError, TypeError):
            flash('Price and quantities must be valid numbers.', 'warning')
            return redirect(url_for('pharmacy.inventory'))

        if (
            not name or len(name) > 100 or not generic_name or len(generic_name) > 100
            or not unit_price.is_finite()
            or unit_price.as_tuple().exponent < -2
            or not Decimal('0') <= unit_price <= Decimal('99999999.99')
            or not 0 <= stock_quantity <= 2147483647
            or not 0 <= min_stock_threshold <= 2147483647
        ):
            flash('Enter valid medicine details and non-negative price and quantities.', 'danger')
            return redirect(url_for('pharmacy.inventory'))

        try:
            execute_dml("""
                INSERT INTO medicines (name, generic_name, unit_price, stock_quantity, min_stock_threshold)
                VALUES (%s, %s, %s, %s, %s)
            """, (name, generic_name, unit_price, stock_quantity, min_stock_threshold))

            flash(f'Medicine "{name}" added to pharmacy inventory.', 'success')
            return redirect(url_for('pharmacy.inventory'))
        except Exception:
            current_app.logger.exception('Could not add medicine to inventory.')
            flash('A database error occurred while adding medicine.', 'danger')

    medicines = query_all("SELECT * FROM medicines ORDER BY name ASC")
    return render_template('admin/pharmacy_inventory.html', medicines=medicines)

@pharmacy_bp.route('/medicine/<int:medicine_id>/edit', methods=['GET', 'POST'])
@role_required('admin')
def edit_medicine(medicine_id):
    medicine = query_one(
        'SELECT id, name, generic_name, unit_price, stock_quantity, min_stock_threshold '
        'FROM medicines WHERE id = %s',
        (medicine_id,)
    )
    if not medicine:
        flash('Medicine record not found.', 'danger')
        return redirect(url_for('pharmacy.inventory'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        generic_name = request.form.get('generic_name', '').strip()
        price_text = request.form.get('unit_price', '').strip()
        threshold_text = request.form.get('min_stock_threshold', '').strip()
        try:
            unit_price = Decimal(price_text)
            threshold = int(threshold_text)
        except (InvalidOperation, ValueError, TypeError):
            flash('Price and threshold must be valid numbers.', 'danger')
            return render_template('admin/edit_medicine.html', medicine=medicine)

        if (
            not name or len(name) > 100 or not generic_name or len(generic_name) > 100
            or not unit_price.is_finite()
            or unit_price.as_tuple().exponent < -2
            or not Decimal('0') <= unit_price <= Decimal('99999999.99')
            or not 0 <= threshold <= 2147483647
        ):
            flash(
                'Enter medicine names, a non-negative price, and a non-negative threshold.',
                'danger'
            )
            return render_template('admin/edit_medicine.html', medicine=medicine)

        try:
            with get_db() as (cursor, _):
                cursor.execute("""
                    UPDATE medicines
                    SET name = %s, generic_name = %s, unit_price = %s,
                        min_stock_threshold = %s
                    WHERE id = %s
                """, (name, generic_name, unit_price, threshold, medicine_id))
                if cursor.rowcount != 1 and (
                    name != medicine['name']
                    or generic_name != medicine['generic_name']
                    or unit_price != Decimal(str(medicine['unit_price']))
                    or threshold != medicine['min_stock_threshold']
                ):
                    raise RuntimeError('Medicine details could not be updated.')
            flash(
                f'Medicine details updated. Current stock remains '
                f'{medicine["stock_quantity"]} units.',
                'success'
            )
            return redirect(url_for('pharmacy.inventory'))
        except Exception:
            current_app.logger.exception('Could not update medicine %s.', medicine_id)
            flash('Medicine details could not be updated. Please try again.', 'danger')

    return render_template('admin/edit_medicine.html', medicine=medicine)

@pharmacy_bp.route('/update-stock/<int:medicine_id>', methods=['POST'])
@role_required('admin')
def update_stock(medicine_id):
    try:
        add_qty = int(request.form.get('add_quantity', 0) or 0)
    except (ValueError, TypeError):
        add_qty = 0

    if not 1 <= add_qty <= 2147483647:
        flash('Quantity to add must be a valid number greater than zero.', 'warning')
        return redirect(url_for('pharmacy.inventory'))

    try:
        med = query_one("SELECT stock_quantity, name FROM medicines WHERE id = %s", (medicine_id,))
        if med:
            new_stock = med['stock_quantity'] + add_qty
            if new_stock > 2147483647:
                flash('The requested quantity exceeds the supported stock limit.', 'warning')
                return redirect(url_for('pharmacy.inventory'))
            execute_dml("UPDATE medicines SET stock_quantity = %s WHERE id = %s", (new_stock, medicine_id))
            flash(f'Updated stock for {med["name"]}. New total: {new_stock} units.', 'success')
        else:
            flash('Medicine record not found.', 'danger')
    except Exception as e:
        flash(f'Stock update failed: {str(e)}', 'danger')

    return redirect(url_for('pharmacy.inventory'))

@pharmacy_bp.route('/low-stock')
@role_required('admin')
def low_stock_alerts():
    try:
        low_stock_medicines = query_all("""
            SELECT * FROM medicines 
            WHERE stock_quantity <= min_stock_threshold 
            ORDER BY stock_quantity ASC
        """)
        return render_template('admin/low_stock_alerts.html', medicines=low_stock_medicines)
    except Exception as e:
        flash(f'Error fetching low-stock alerts: {str(e)}', 'danger')
        return redirect(url_for('pharmacy.inventory'))
