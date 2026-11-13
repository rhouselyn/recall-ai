"""在长任务运行期间续租；进程崩溃后由租约到期恢复。"""
from contextlib import contextmanager
import sqlite3
import threading

from .db import open_readwrite


@contextmanager
def heartbeat(db_path, renew, *, interval=30):
    stopped = threading.Event()

    def run():
        conn = open_readwrite(db_path)
        try:
            while not stopped.wait(interval):
                try:
                    if not renew(conn):
                        return
                except sqlite3.OperationalError:
                    # 短暂写锁不应放弃续租；提交方仍须复核所有权。
                    continue
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
