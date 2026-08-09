"""Daily Upstox re-authentication prompt, driven by Windows Task Scheduler.

Upstox invalidates every access token at 03:30 IST and issues no refresh token,
so the exchange step needs a human browser login — this cannot be made fully
unattended (Kite behaves the same way). What it CAN do is stop the timing being
something you have to remember: at 03:35 the machine opens the login page for
you, and you complete 2FA when you sit down.

Deliberately quiet. It exits without doing anything when:
  * today is not an NSE trading day (no weekend/holiday popups)
  * the current token is still valid (you already re-authenticated)
  * credentials are missing (there is nothing to log into — it says so and stops)

    python upstox_daily_login.py              # run the check now
    python upstox_daily_login.py --dry-run    # decide, but open nothing
    python upstox_daily_login.py --install    # register the 03:35 task
    python upstox_daily_login.py --uninstall  # remove it
    python upstox_daily_login.py --status     # is the task registered?
"""
from __future__ import annotations

import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from upstox_auth import IST, get_upstox_auth

TASK_NAME = "ApexUpstoxDailyLogin"
RUN_AT = "03:35"          # a few minutes after the 03:30 IST invalidation
HERE = Path(__file__).resolve().parent


def _is_trading_day(now: datetime) -> bool:
    try:
        from market_calendar import is_trading_day
        return is_trading_day(now.date())
    except Exception:
        return now.weekday() < 5


def run_check(dry_run: bool = False) -> int:
    now = datetime.now(IST)
    print(f"[{now:%Y-%m-%d %H:%M:%S IST}] Upstox daily login check")

    if not _is_trading_day(now):
        print("  Not an NSE trading day - nothing to do.")
        return 0

    auth = get_upstox_auth()
    missing = auth.missing_credentials()
    if missing:
        print(f"  Missing credentials in upstox_secrets.env: {', '.join(missing)}")
        print("  Cannot start a login until those are set.")
        return 1

    # Re-probe rather than trusting cached state: this runs long after the
    # process that cached it, and the whole point is to catch the 03:30 death.
    auth.load_cached_session()
    st = auth.status()

    if st["connected"] and not st["expiring_soon"]:
        print(f"  Token still valid ({st['hours_remaining']}h left) - no login needed.")
        return 0

    reason = st["reason"] or ("expiring within the hour" if st["expiring_soon"] else "expired")
    print(f"  Re-authentication needed: {reason}")

    try:
        url = auth.login_url()
    except ValueError as exc:
        print(f"  Could not build the login URL: {exc}")
        return 1

    if dry_run:
        print(f"  [dry run] would open: {url}")
        return 0

    print("  Opening the Upstox login in your default browser ...")
    opened = webbrowser.open(url)
    if not opened:
        print("  Could not launch a browser. Open this manually:")
        print(f"  {url}")
    print("  After logging in, paste the FULL redirect URL into the dashboard")
    print("  (Settings -> Broker Connections -> UPSTOX badge), or run:")
    print("     python upstox_login.py")
    return 0


# ── Windows Task Scheduler registration ──────────────────────────────────────

def _task_command() -> str:
    """The command Task Scheduler runs. Absolute paths: the task has no cwd."""
    python = Path(sys.executable).resolve()
    script = HERE / "upstox_daily_login.py"
    return f'"{python}" "{script}"'


def install() -> int:
    """Register the daily task in the CURRENT USER's scope (no elevation).

    /RL LIMITED and no /RU SYSTEM on purpose: this must run as you, in your
    desktop session, or it cannot open a browser window you would ever see.
    """
    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", _task_command(),
        "/SC", "DAILY",
        "/ST", RUN_AT,
        "/RL", "LIMITED",
        "/F",                       # replace an existing registration
    ]
    print("Registering the daily Upstox login task ...")
    print(f"  name    : {TASK_NAME}")
    print(f"  runs at : {RUN_AT} daily (skips non-trading days at runtime)")
    print(f"  command : {_task_command()}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FAILED: {result.stderr.strip() or result.stdout.strip()}")
        return result.returncode
    print(result.stdout.strip())
    print()
    print("NOTE: Task Scheduler will not wake a sleeping machine by default.")
    print("If the laptop is asleep at 03:35, enable 'Run task as soon as possible")
    print("after a scheduled start is missed' in Task Scheduler, or just open the")
    print("dashboard - the UPSTOX badge shows the countdown either way.")
    return 0


def uninstall() -> int:
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Could not remove the task: {result.stderr.strip() or result.stdout.strip()}")
        return result.returncode
    print(f"Removed the scheduled task {TASK_NAME}.")
    return 0


def task_status() -> int:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Task {TASK_NAME} is NOT registered.")
        print("Register it with:  python upstox_daily_login.py --install")
        return 1
    keep = ("TaskName:", "Status:", "Next Run Time:", "Last Run Time:",
            "Last Result:", "Schedule:", "Start Time:", "Task To Run:")
    for line in result.stdout.splitlines():
        if any(line.strip().startswith(k) for k in keep):
            print("  " + line.strip())
    return 0


def main(argv: list[str]) -> int:
    if "--install" in argv:
        return install()
    if "--uninstall" in argv:
        return uninstall()
    if "--status" in argv:
        return task_status()
    return run_check(dry_run="--dry-run" in argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
