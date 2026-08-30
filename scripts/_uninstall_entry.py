
import subprocess
import shutil
from pathlib import Path

def is_admin():
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def uninstall():
    print("=" * 50)
    print(" Lamix Uninstall")
    print("=" * 50)

    if not is_admin():
        print("Note: Some operations may require admin rights")
    print()

    # Delete scheduled task
    task_name = "Lamix"
    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", task_name, "/f"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print(f"Deleted task: {task_name}")
        elif "not found" in result.stderr.lower():
            print(f"Task {task_name} not found, skipping")
        else:
            print(f"Failed to delete task: {result.stderr.strip()}")
    except Exception as e:
        print(f"Error deleting task: {e}")

    # Ask about config directory
    print()
    data_dir = Path.home() / ".lamix"
    if data_dir.exists():
        print(f"Config dir: {data_dir}")
        print("  Keep: Next install will reuse config, memory, skills.")
        print("  Delete: Remove all personal data.")
        choice = input("\nDelete config dir? (y/N): ").strip().lower()
        if choice in ("y", "yes"):
            try:
                shutil.rmtree(data_dir)
                print(f"Deleted: {data_dir}")
            except Exception as e:
                print(f"Delete failed: {e}")
                print(f"Please delete manually: {data_dir}")
        else:
            print(f"Kept: {data_dir}")
    else:
        print("Config dir not found, nothing to clean.")

    print()
    print("Uninstall complete. Please delete project code manually.")

if __name__ == "__main__":
    try:
        uninstall()
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to exit...")
