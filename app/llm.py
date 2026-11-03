import json
import logging
import os
import subprocess
import threading
from pathlib import Path

from app import db

log = logging.getLogger(__name__)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude-cli")
LLM_MODEL = os.getenv("LLM_MODEL", "sonnet")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))
WORKDIR = Path(__file__).parent.parent / "data" / "llm-cwd"

# The host has little free memory, so only one CLI process runs at a time.
_cli_lock = threading.Lock()


class LLMError(Exception):
    pass


def complete(system: str, prompt: str) -> tuple[str, str]:
    if os.getenv("LLM_PROVIDER", LLM_PROVIDER) != "claude-cli":
        raise LLMError("Unknown LLM_PROVIDER")
    if db.get_setting("llm_down") == "1":
        raise LLMError("LLM marked unavailable by admin")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    args = [CLAUDE_BIN, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
            "--setting-sources", "", "--strict-mcp-config", "--system-prompt", system, "--model", LLM_MODEL]
    try:
        with _cli_lock:
            proc = subprocess.run(args, input=prompt, capture_output=True, text=True,
                                  timeout=LLM_TIMEOUT, cwd=WORKDIR)
    except FileNotFoundError:
        raise LLMError("LLM command not found")
    except subprocess.TimeoutExpired:
        raise LLMError("LLM timed out")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON output (exit %s): %s", proc.returncode, proc.stderr[:200])
        raise LLMError("LLM returned unreadable output")
    if out.get("is_error") or "result" not in out:
        raise LLMError("LLM reported an error")
    model = next(iter(out.get("modelUsage") or {}), LLM_MODEL)
    return out["result"], model
