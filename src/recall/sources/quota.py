"""知乎请求的本机日预算：先记账再请求，失败和重启都不会返还额度。"""
from datetime import timedelta

from ..catalog import transaction
from ..clock import now
from ..errors import RecallError


class DailyQuotaClient:
    def __init__(self, conn, client, limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise RecallError("INVALID_ARGUMENT", "daily_request_limit 必须是非负整数。")
        self.conn, self.client, self.limit = conn, client, limit

    def remaining(self):
        key = "zhihu_requests:" + now().date().isoformat()
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        remaining = self.limit - (int(row[0]) if row else 0)
        if remaining <= 0:
            current = now()
            tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            raise RecallError("RATE_LIMITED", "本地知乎每日请求预算已用尽，UTC 次日恢复。",
                              details={"retry_after": int((tomorrow - current).total_seconds()) + 1})
        return remaining

    def request(self, *args, **kwargs):
        with transaction(self.conn):
            current = now()
            key = "zhihu_requests:" + current.date().isoformat()
            self.remaining()
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1", (key,),
            )
            self.conn.execute("DELETE FROM meta WHERE key LIKE 'zhihu_requests:%' AND key < ?", (key,))
        return self.client.request(*args, **kwargs)
