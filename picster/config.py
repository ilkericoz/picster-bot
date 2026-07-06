import asyncio
import json
import os

CONFIG_PATH = "picster_config.json"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


async def config_reloader(entries, interval=10):
    """Poll the config file every `interval` seconds and update entries in-place on change."""
    last_mtime = os.path.getmtime(CONFIG_PATH)
    while True:
        await asyncio.sleep(interval)
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime == last_mtime:
                continue
            new_config = load_config()
            new_by_name = {e["name"]: e for e in new_config.get("urls", [])}
            for entry in entries:
                name = entry.get("name")
                if name in new_by_name:
                    entry.clear()
                    entry.update(new_by_name[name])
            last_mtime = mtime
            print(f"[CONFIG] Reloaded — {', '.join(new_by_name)}")
        except Exception as e:
            print(f"[CONFIG] Reload failed: {e}")
