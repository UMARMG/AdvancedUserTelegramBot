import sys
import subprocess


def start_user_bot(string_session: str):
    subprocess.Popen([sys.executable, "user_bot/main_user_bot.py", string_session])
