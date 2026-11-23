# create_user.py
import os
import sys
import mysql.connector
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()  # loads .env in current directory

DB_CONFIG = {
    'user': os.getenv('DB_USER', ''),
    'password': os.getenv('DB_PASS', ''),
    'host': os.getenv('DB_HOST', '127.0.0.1'),
    'database': os.getenv('DB_NAME', ''),
}

def create_user(username, password, role='manager'):
    if not DB_CONFIG['user'] or not DB_CONFIG['password'] or not DB_CONFIG['database']:
        print("ERROR: DB credentials missing. Check your .env file.")
        print("Current DB_CONFIG:", DB_CONFIG)
        sys.exit(1)
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur = conn.cursor()
        pw_hash = generate_password_hash(password)
        cur.execute("INSERT INTO users (username, role, password_hash) VALUES (%s,%s,%s)", (username, role, pw_hash))
        conn.commit()
        cur.close()
        conn.close()
        print("Created user:", username)
    except mysql.connector.Error as e:
        print("MySQL error:", e)
        sys.exit(1)

if __name__ == '__main__':
    # default admin / 1234
    create_user('admin', '1234', 'manager')
