from datetime import datetime, time, timedelta


DOCTOR_SCHEDULE_QUERY = """
    SELECT ds.start_time, ds.end_time, ds.break_start, ds.break_end,
           ds.slot_duration_mins, d.user_id AS doctor_user_id
    FROM doctor_schedules ds
    JOIN doctors d ON ds.doctor_id = d.id
    JOIN users u ON d.user_id = u.id
    WHERE ds.doctor_id = %s AND ds.day_of_week = %s
      AND d.status = 'active' AND u.status = 'active'
"""


def parse_iso_date(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None
    return parsed if parsed.isoformat() == value else None


def parse_schedule_time(value):
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        if total_seconds < 0 or total_seconds >= 24 * 60 * 60:
            return None
        return (datetime.min + value).time()
    if value is None:
        return None

    value = str(value)
    for time_format in ('%H:%M', '%H:%M:%S'):
        try:
            parsed = datetime.strptime(value, time_format).time()
        except ValueError:
            continue
        if parsed.strftime(time_format) == value:
            return parsed
    return None


def _minutes(value):
    return value.hour * 60 + value.minute


def build_slot_availability(schedules, appointments, booking_date, now, leaves=None):
    if not schedules:
        return [], None

    slots = []
    schedule_details = None
    for schedule in schedules:
        start_time = parse_schedule_time(schedule['start_time'])
        end_time = parse_schedule_time(schedule['end_time'])
        break_start = parse_schedule_time(schedule.get('break_start'))
        break_end = parse_schedule_time(schedule.get('break_end'))
        duration = schedule['slot_duration_mins']
        if (
            start_time is None
            or end_time is None
            or not isinstance(duration, int)
            or duration <= 0
            or end_time <= start_time
        ):
            raise ValueError('Invalid doctor schedule.')
        if (break_start is None) != (break_end is None):
            raise ValueError('Incomplete doctor break schedule.')
        if break_start and (
            break_end <= break_start
            or break_start < start_time
            or break_end > end_time
        ):
            raise ValueError('Invalid doctor break schedule.')

        work_start = _minutes(start_time)
        work_end = _minutes(end_time)
        break_start_minute = _minutes(break_start) if break_start else None
        break_end_minute = _minutes(break_end) if break_end else None
        booking_start = datetime.combine(booking_date, time.min)
        day_minutes = 24 * 60
        leave_intervals = []
        for leave in leaves or []:
            if leave['is_full_day']:
                leave_start = start_time
                leave_end = end_time
            else:
                leave_start = parse_schedule_time(leave['start_time'])
                leave_end = parse_schedule_time(leave['end_time'])
                if leave_start is None or leave_end is None or leave_end <= leave_start:
                    raise ValueError('Invalid approved doctor leave interval.')
            leave_intervals.append((_minutes(leave_start), _minutes(leave_end)))

        booked_intervals = []
        for appointment in appointments:
            booked_time = parse_schedule_time(appointment['appointment_time'])
            if booked_time is not None:
                booked_start = _minutes(booked_time)
                booked_intervals.append((booked_start, booked_start + duration))

        schedule_details = {
            'working_hours': {
                'start': start_time.strftime('%H:%M'),
                'end': end_time.strftime('%H:%M')
            },
            'break_times': (
                {
                    'start': break_start.strftime('%H:%M'),
                    'end': break_end.strftime('%H:%M')
                }
                if break_start and break_end else None
            ),
            'duration_minutes': duration
        }

        # Anchor the full-day grid to the registered shift start.
        slot_starts = []
        slot_start_minute = work_start
        while slot_start_minute >= 0:
            slot_end_minute = slot_start_minute + duration
            if slot_end_minute <= day_minutes:
                slot_starts.append(slot_start_minute)
            slot_start_minute -= duration

        slot_start_minute = work_start + duration
        while slot_start_minute < day_minutes:
            slot_end_minute = slot_start_minute + duration
            if slot_end_minute <= day_minutes:
                slot_starts.append(slot_start_minute)
            slot_start_minute += duration

        for slot_start_minute in sorted(slot_starts):
            slot_end_minute = slot_start_minute + duration
            slot_start = booking_start + timedelta(minutes=slot_start_minute)
            in_working_hours = (
                slot_start_minute >= work_start
                and slot_end_minute <= work_end
            )
            overlaps_break = (
                break_start_minute is not None
                and slot_start_minute < break_end_minute
                and slot_end_minute > break_start_minute
            )
            overlaps_leave = any(
                slot_start_minute < leave_end
                and slot_end_minute > leave_start
                for leave_start, leave_end in leave_intervals
            )
            overlaps_appointment = any(
                slot_start_minute < booked_end and slot_end_minute > booked_start
                for booked_start, booked_end in booked_intervals
            )

            if not in_working_hours:
                status = 'outside'
            elif overlaps_break:
                status = 'break'
            elif overlaps_leave:
                status = 'leave'
            elif overlaps_appointment or slot_start <= now:
                status = 'unavailable'
            else:
                status = 'available'

            slots.append({
                'time': slot_start.strftime('%H:%M'),
                'status': status
            })

    slots.sort(key=lambda slot: slot['time'])
    return slots, schedule_details


def is_slot_available(
    schedules, appointments, booking_date, appointment_time, now, leaves=None
):
    slots, _ = build_slot_availability(
        schedules, appointments, booking_date, now, leaves
    )
    return any(
        slot['time'] == appointment_time.strftime('%H:%M')
        and slot['status'] == 'available'
        for slot in slots
    )


def has_patient_time_conflict(appointments, appointment_time, appointment_duration):
    new_start = datetime.combine(datetime.min.date(), appointment_time)
    new_end = new_start + timedelta(minutes=appointment_duration)

    for appointment in appointments:
        if appointment.get('status') == 'cancelled':
            continue

        existing_start_time = parse_schedule_time(appointment['appointment_time'])
        existing_duration = appointment.get('slot_duration_mins')
        if (
            existing_start_time is None
            or not isinstance(existing_duration, int)
            or existing_duration <= 0
        ):
            return True

        existing_start = datetime.combine(datetime.min.date(), existing_start_time)
        existing_end = existing_start + timedelta(minutes=existing_duration)
        if new_start < existing_end and new_end > existing_start:
            return True

    return False


def available_times(schedules, booked_times, booking_date, now, leaves=None):
    appointments = [{'appointment_time': value} for value in booked_times]
    slots, _ = build_slot_availability(
        schedules, appointments, booking_date, now, leaves
    )
    return [
        slot['time']
        for slot in slots
        if slot['status'] == 'available'
    ]
