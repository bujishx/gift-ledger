"""Exercise the maintenance page JavaScript with the candidate runner's Node runtime."""
from pathlib import Path
import subprocess


def test_manual_backup_ui():
    subprocess.run(["node", "--test", Path(__file__).with_name("manual_backup_ui.test.mjs")], check=True)
