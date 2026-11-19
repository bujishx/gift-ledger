"""Daily certificate checks for the single-YAML site's maintenance task."""
from datetime import datetime, timedelta, timezone


RENEW_BEFORE = timedelta(days=30)


def renewal_due(not_after, now=None):
    now = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(not_after) <= now + RENEW_BEFORE


def report_daily_result(result):
    """A routine check has no output. A deferred run explains its next retry."""
    if result["status"] == "current":
        return True
    if result["status"] == "deferred":
        print("本站正在执行其他维护任务，本次自动续签检查已跳过。群晖会在下次计划运行时重试。")
        return True
    return False
