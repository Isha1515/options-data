"""
setup_schedule.py
==================
Automatically sets up a daily cron job (Mac/Linux) or prints
Task Scheduler instructions (Windows) to run the collector
every weekday at 4:30pm ET.
"""

import sys
import os
import platform
import subprocess

SCRIPT = os.path.abspath("collect_options_data.py")
PYTHON = sys.executable


def setup_mac_linux():
    cron_line = f"30 16 * * 1-5 {PYTHON} {SCRIPT} >> {os.path.abspath('collector.log')} 2>&1"
    print("\n── Mac / Linux Setup ───────────────────────────────────────")
    print("Adding cron job to run the collector Mon-Fri at 4:30pm...\n")

    # Read existing crontab
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if SCRIPT in existing:
        print("✓ Cron job already exists:")
        for line in existing.splitlines():
            if SCRIPT in line:
                print(f"  {line}")
        return

    new_crontab = existing.rstrip("\n") + "\n" + cron_line + "\n"
    proc = subprocess.run(['crontab', '-'], input=new_crontab, text=True, capture_output=True)

    if proc.returncode == 0:
        print("✓ Cron job added successfully!")
        print(f"\n  Schedule : Mon–Fri at 4:30pm (system time)")
        print(f"  Command  : {PYTHON} {SCRIPT}")
        print(f"  Log file : {os.path.abspath('collector.log')}")
        print("\n⚠️  Make sure your system clock is set to Eastern Time,")
        print("   or adjust the cron time (16 30 = 4:30pm) to match your timezone offset.")
    else:
        print("✗ Failed to add cron job. Add it manually:")
        print(f"\n  Run: crontab -e")
        print(f"  Add: {cron_line}\n")


def setup_windows():
    print("\n── Windows Task Scheduler Setup ────────────────────────────")
    print("Run this command in an Administrator PowerShell:\n")
    cmd = (
        f'$action = New-ScheduledTaskAction -Execute "{PYTHON}" '
        f'-Argument "{SCRIPT}"; '
        f'$trigger = New-ScheduledTaskTrigger -Weekly '
        f'-DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday '
        f'-At 4:30PM; '
        f'Register-ScheduledTask -Action $action -Trigger $trigger '
        f'-TaskName "OptionsDataCollector" -RunLevel Highest'
    )
    print(f"  {cmd}\n")
    print("Or manually in Task Scheduler:")
    print("  1. Open Task Scheduler → Create Basic Task")
    print("  2. Trigger: Daily, recur every 1 day, Mon-Fri")
    print("  3. Action: Start a program")
    print(f"     Program : {PYTHON}")
    print(f"     Arguments: {SCRIPT}")
    print(f"     Start in : {os.path.dirname(SCRIPT)}")


def main():
    system = platform.system()
    print("Options Data Collector — Schedule Setup")
    print(f"Python  : {PYTHON}")
    print(f"Script  : {SCRIPT}")
    print(f"Platform: {system}")

    if system in ('Darwin', 'Linux'):
        setup_mac_linux()
    elif system == 'Windows':
        setup_windows()
    else:
        print(f"\nUnknown platform '{system}'. Set up scheduling manually.")

    print("\n── Quick Test ──────────────────────────────────────────────")
    print("Run this to test the collector right now (just AAPL):\n")
    print(f"  {PYTHON} collect_options_data.py --symbols AAPL\n")


if __name__ == '__main__':
    main()
