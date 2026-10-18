from datetime import datetime, timedelta

from utils.appointment_helper import parse_schedule_time


def is_leave_schema_error(error):
    error_args = getattr(error, 'args', ())
    return bool(error_args and error_args[0] in (1054, 1146))


def leave_interval(leave, schedule):
    if leave['is_full_day']:
        return (
            parse_schedule_time(schedule['start_time']),
            parse_schedule_time(schedule['end_time'])
        )
    return (
        parse_schedule_time(leave['start_time']),
        parse_schedule_time(leave['end_time'])
    )


def appointment_overlaps_leave(appointment, leave, schedule):
    appointment_start = parse_schedule_time(appointment['appointment_time'])
    duration = appointment.get('slot_duration_mins') or schedule['slot_duration_mins']
    leave_start, leave_end = leave_interval(leave, schedule)
    if (
        appointment_start is None
        or leave_start is None
        or leave_end is None
        or duration <= 0
    ):
        return False

    appointment_end = (
        datetime.combine(datetime.min, appointment_start)
        + timedelta(minutes=duration)
    ).time()
    return appointment_start < leave_end and appointment_end > leave_start


def is_valid_partial_leave(start_time, end_time, schedule):
    start_time = parse_schedule_time(start_time)
    end_time = parse_schedule_time(end_time)
    schedule_start = parse_schedule_time(schedule['start_time'])
    schedule_end = parse_schedule_time(schedule['end_time'])
    if (
        start_time is None
        or end_time is None
        or schedule_start is None
        or schedule_end is None
        or end_time <= start_time
        or start_time < schedule_start
        or end_time > schedule_end
    ):
        return False

    break_start = parse_schedule_time(schedule.get('break_start'))
    break_end = parse_schedule_time(schedule.get('break_end'))
    return not (
        break_start and break_end
        and start_time < break_end
        and end_time > break_start
    )


def leave_overlaps_existing(leave, existing):
    if leave['is_full_day'] or existing['is_full_day']:
        return True
    start_time = parse_schedule_time(leave['start_time'])
    end_time = parse_schedule_time(leave['end_time'])
    existing_start = parse_schedule_time(existing['start_time'])
    existing_end = parse_schedule_time(existing['end_time'])
    return (
        start_time is not None
        and end_time is not None
        and existing_start is not None
        and existing_end is not None
        and start_time < existing_end
        and end_time > existing_start
    )
