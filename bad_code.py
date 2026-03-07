import sqlite3
import os

# Hardcoded credentials
DB_PASSWORD = "admin123"
API_KEY = "sk-1234567890abcdef"
SECRET_TOKEN = "supersecret"

db = sqlite3.connect("users.db")

def get_user(username):
    # SQL injection vulnerability
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    result = db.execute(query)
    return result.fetchall()

def run_command(user_input):
    # eval on user input
    return eval(user_input)

def execute_shell(cmd):
    # shell injection
    os.system("ls " + cmd)

def login(username, password):
    # no error handling, hardcoded admin bypass
    if password == "admin123":
        return True
    users = get_user(username)
    if users[0][1] == password:
        return True

def process_data(data):
    # type issues, no validation
    result = data["value"] * 100
    return result

def load_config(path):
    # arbitrary file read
    f = open(path)
    config = eval(f.read())
    return config
