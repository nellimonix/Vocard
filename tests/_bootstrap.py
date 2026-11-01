import os
from pathlib import Path
from unittest.mock import patch

settings_path = str(Path(__file__).resolve().parent.parent / "settings.json")
exists = os.path.exists

with patch("os.path.exists", side_effect=lambda path: str(path) == settings_path or exists(path)):
    from voicelink import Config, LangHandler
    import function

Config({"client_id": 1, "prefix": "!", "logging": {"file": {"enable": False}}, "ipc_client": {"enable": False}})
LangHandler.init()
