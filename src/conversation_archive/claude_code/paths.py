from pathlib import Path
import os


def default_input_path() -> str:
    explicit_input = os.environ.get("CLAUDE_CODE_SESSION_INPUT")
    if explicit_input is not None:
        return explicit_input

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return str(Path(config_dir).expanduser() / "projects")
    return str(Path.home() / ".claude/projects")


def default_db_path() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_DB", str(Path.home() / "Library/Application Support/Conversation Archive Tools/claude_code_sessions.sqlite"))
