import pymysql
import pymysql.cursors
from contextlib import contextmanager
from flask import current_app, has_app_context
from config import Config

def get_db_connection():
    """Establish connection to MySQL server with fallback to Config if outside Flask app context."""
    if has_app_context():
        host = current_app.config.get('MYSQL_HOST', Config.MYSQL_HOST)
        port = current_app.config.get('MYSQL_PORT', Config.MYSQL_PORT)
        user = current_app.config.get('MYSQL_USER', Config.MYSQL_USER)
        password = current_app.config.get('MYSQL_PASSWORD', Config.MYSQL_PASSWORD)
        database = current_app.config.get('MYSQL_DB', Config.MYSQL_DB)
    else:
        host = Config.MYSQL_HOST
        port = Config.MYSQL_PORT
        user = Config.MYSQL_USER
        password = Config.MYSQL_PASSWORD
        database = Config.MYSQL_DB

    try:
        connection = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False
        )
        return connection
    except pymysql.MySQLError as e:
        print(f"[DB Error] Failed to connect to MySQL ({host}:{port}/{database}): {e}")
        return None

@contextmanager
def get_db(lock_name=None):
    """Manage a transaction, optionally holding a MySQL advisory lock through commit."""
    conn = get_db_connection()
    if conn is None:
        raise Exception("Database connection unavailable. Please ensure MySQL server is running and database configuration is correct.")
    cursor = conn.cursor()
    lock_acquired = False
    try:
        if lock_name:
            cursor.execute('SELECT GET_LOCK(%s, 10) AS acquired', (lock_name,))
            lock_result = cursor.fetchone()
            if not lock_result or lock_result.get('acquired') != 1:
                raise RuntimeError('Could not acquire the requested database lock.')
            lock_acquired = True
        yield cursor, conn
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[DB Transaction Error] Rolled back: {e}")
        raise e
    finally:
        if lock_acquired:
            try:
                cursor.execute('SELECT RELEASE_LOCK(%s) AS released', (lock_name,))
                cursor.fetchone()
            except Exception as lock_error:
                if has_app_context():
                    current_app.logger.exception(
                        'Could not release database advisory lock %s: %s',
                        lock_name,
                        lock_error
                    )
                else:
                    print(f"[DB Lock Error] Could not release {lock_name}: {lock_error}")
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

def query_one(sql, params=None):
    """Execute query and fetch a single record."""
    with get_db() as (cursor, _):
        cursor.execute(sql, params or ())
        return cursor.fetchone()

def query_all(sql, params=None):
    """Execute query and fetch all matching records."""
    with get_db() as (cursor, _):
        cursor.execute(sql, params or ())
        return cursor.fetchall()

def execute_dml(sql, params=None):
    """Execute INSERT, UPDATE, DELETE statements and return last inserted ID."""
    with get_db() as (cursor, _):
        cursor.execute(sql, params or ())
        return cursor.lastrowid
