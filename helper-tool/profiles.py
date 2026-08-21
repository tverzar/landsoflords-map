"""Saved server connection profiles — host/username/key path only. Never a
password (SSH or game) — those are typed fresh each session, same policy
as the rest of this project."""
import json
from pathlib import Path

PROFILES_DIR = Path(__file__).parent / "server_profiles"


def list_profiles():
    PROFILES_DIR.mkdir(exist_ok=True)
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load_profile(name):
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        return {"host": "", "username": "root", "key_path": "", "game_username": "", "port": 22}
    return json.loads(path.read_text(encoding="utf-8"))


def save_profile(name, data):
    PROFILES_DIR.mkdir(exist_ok=True)
    path = PROFILES_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def delete_profile(name):
    path = PROFILES_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
