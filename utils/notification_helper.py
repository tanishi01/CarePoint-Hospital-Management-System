from database import execute_dml

def create_notification(user_id, message, cursor=None):
    """Insert a notification message for a specific user."""
    sql = "INSERT INTO notifications (user_id, message) VALUES (%s, %s)"
    if cursor is not None:
        cursor.execute(sql, (user_id, message))
        return True
    try:
        execute_dml(sql, (user_id, message))
        return True
    except Exception as e:
        print(f"[Notification Helper Error] {e}")
        return False
