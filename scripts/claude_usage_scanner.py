#!/usr/bin/env python3
"""Query Claude Code state, active sessions, history, transcripts, and OAuth quota to emit usage stats."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
TRANSCRIPT_CACHE_VERSION = 1

CLAUDE_CODE_LOCAL_COMMANDS = frozenset((
    "add-dir", "advisor", "agents", "allowed-tools", "android", "app", "artifacts", "autocompact", "autofix-pr",
    "auto-mode-setup", "background", "bashes", "bg", "branch", "break-reminder", "breaks", "brief", "btw", "bug", "cd",
    "checkpoint", "clear", "cloud-plugins", "color", "compact", "config", "context", "copy", "cost", "daemon",
    "design-consent", "design-login", "design-revoke", "desktop", "diff", "downtime", "effort", "exit", "export",
    "extra-usage", "fast", "feedback", "focus", "fork", "goal", "heapdump", "help", "hooks", "ide", "import", "install",
    "install-github-app", "install-slack-app", "ios", "list-agents", "login", "logout", "loops", "marketplace", "mcp",
    "memory", "memory-pause", "mobile", "model", "name", "new", "output-style", "passes", "pause-memory", "peers",
    "permissions", "plan", "plugin", "plugins", "powerup", "privacy-settings", "pro-trial-expired", "quit", "radio",
    "rate-limit-options", "rc", "recap", "reload-plugins", "reload-skills", "remote", "remote-control", "remote-env",
    "rename", "reset", "restart", "resume", "rewind", "scroll-speed", "session", "settings", "setup-bedrock",
    "setup-vertex", "skill-doctor", "skills", "stats", "status", "stickers", "stop", "subtask", "tasks", "teleport",
    "terminal-setup", "theme", "toggle-memory", "tp", "tui", "ultraplan", "ultrareview", "undo", "update", "upgrade",
    "usage", "usage-credits", "version", "voice", "web-setup", "wellbeing", "workflow-launch-exec", "workflows",
))

CLAUDE_CODE_MACHINE_PROMPTS = (
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "[Request interrupted",
)
CLAUDE_CODE_LOCAL_OUTPUT = ("<local-command-stdout>", "<local-command-stderr>")
CLAUDE_CODE_MACHINE_SOURCES = ("system", "task-notification", "peer")

_MS_EPOCH_THRESHOLD = 10_000_000_000


def default_base_dir() -> Path:
    return Path(os.path.expandvars(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))).resolve()


def resolve_cache_dir(base_dir: Path) -> Path:
    """Keep plugin cache isolated in XDG cache for real ~/.claude, and inside base_dir/cache for tests."""
    try:
        if base_dir.resolve() == default_base_dir():
            xdg = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
            return Path(xdg).resolve() / "ozzy.claude-usage"
    except Exception:
        pass
    return base_dir / "cache"


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def date_string(value: dt.date) -> str:
    return value.strftime("%Y-%m-%d")


def sanitize_plain_text(val: Any, max_len: int = 250) -> str:
    """Sanitize arbitrary strings to safe plain-text by stripping control chars and truncating."""
    if val is None:
        return ""
    text = str(val)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def recent_date_strings() -> list[str]:
    today = dt.datetime.now().date()
    return [date_string(today - dt.timedelta(days=offset)) for offset in range(6, -1, -1)]


def normalize_timestamp_seconds(value: Any) -> float:
    """Normalize a timestamp value (epoch seconds, ms, or ISO string) to epoch seconds."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        try:
            v = float(value)
            if not (v >= 0):
                return 0.0
            return v / 1000.0 if v > _MS_EPOCH_THRESHOLD else v
        except (TypeError, ValueError):
            return 0.0
    raw = str(value).strip()
    if not raw:
        return 0.0
    if re.fullmatch(r"[0-9]{1,20}(\.[0-9]+)?", raw):
        return normalize_timestamp_seconds(float(raw))
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except Exception:
        return 0.0


def local_date_from_timestamp(value: Any) -> str:
    if value is None:
        return date_string(dt.datetime.now().date())
    if isinstance(value, (int, float)):
        try:
            seconds = normalize_timestamp_seconds(value)
            if seconds <= 0:
                return date_string(dt.datetime.now().date())
            return date_string(dt.datetime.fromtimestamp(seconds).date())
        except Exception:
            return date_string(dt.datetime.now().date())
    raw = str(value).strip()
    if not raw:
        return date_string(dt.datetime.now().date())
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return date_string(parsed.date())
    except Exception:
        pass
    try:
        clean = raw.split(".")[0]
        parsed = dt.datetime.fromisoformat(clean)
        return date_string(parsed.date())
    except Exception:
        return date_string(dt.datetime.now().date())


def format_claude_model_name(raw: Any) -> str:
    """Turn raw Claude model identifiers into concise human-readable display names."""
    text = sanitize_plain_text(raw, 100)
    if not text or text == "<synthetic>":
        return ""

    context_suffix = ""
    if re.search(r"\[1m\]$", text, re.IGNORECASE):
        text = re.sub(r"\[1m\]$", "", text, flags=re.IGNORECASE).strip()
        context_suffix = " (1M)"

    low = text.lower()
    aliases = {
        "opus": "Claude Opus",
        "sonnet": "Claude Sonnet",
        "haiku": "Claude Haiku",
        "fable": "Claude Fable",
        "default": "Claude Code",
    }
    if low in aliases:
        return aliases[low] + context_suffix

    # Strip trailing YYYYMMDD date stamp if present
    cleaned = re.sub(r"-\d{8}$", "", low)

    # Match claude-<family>-<major>-<minor> (e.g. claude-opus-5-5, claude-fable-5-2)
    m = re.match(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?$", cleaned)
    if m:
        family = m.group(1).capitalize()
        ver = f"{m.group(2)}.{m.group(3)}" if m.group(3) else m.group(2)
        return f"Claude {family} {ver}{context_suffix}"

    # Match legacy claude-<major>-<minor>-<family> (e.g. claude-3-5-sonnet, claude-3-7-sonnet)
    m_legacy = re.match(r"^claude-(\d+)(?:-(\d+))?-([a-z]+)$", cleaned)
    if m_legacy:
        ver = f"{m_legacy.group(1)}.{m_legacy.group(2)}" if m_legacy.group(2) else m_legacy.group(1)
        family = m_legacy.group(3).capitalize()
        return f"Claude {ver} {family}{context_suffix}"

    return text + context_suffix


def model_family_color(name: str) -> str:
    low = name.lower()
    if "opus" in low:
        return "#D97757"
    if "sonnet" in low:
        return "#38BDF8"
    if "haiku" in low:
        return "#10B981"
    if "fable" in low:
        return "#A855F7"
    return "#D97757"


def read_json_safe(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_configured_model(base_dir: Path | None = None) -> str:
    """Read the user's configured default model from settings.json if available."""
    if base_dir:
        data = read_json_safe(base_dir / "settings.json")
        m = data.get("model")
        if isinstance(m, str) and m.strip():
            formatted = format_claude_model_name(m.strip())
            if formatted:
                return formatted
    return "Claude Code"


def profile_path(home: Path) -> Path:
    legacy = home / ".config.json"
    if legacy.exists():
        return legacy
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if home == expand_path("~/.claude") and not (configured and expand_path(configured) == home):
        return Path.home() / ".claude.json"
    return home / ".claude.json"


def plan_label(tier: str, subscription: str) -> str:
    plan = subscription[0].upper() + subscription[1:] if subscription else ""
    match = re.search(r"max_(\d+x)", tier, re.IGNORECASE) if tier else None
    if not match:
        return sanitize_plain_text(plan, 40)
    return sanitize_plain_text((plan or "Max") + " " + match.group(1), 40)


def read_oauth_info(base_dir: Path) -> tuple[str, int, int, str]:
    """Read access token, expiries, and display-safe plan label. Tokens never leave the probe."""
    creds = read_json_safe(base_dir / ".credentials.json")
    login = creds.get("claudeAiOauth")
    if not isinstance(login, dict):
        return "", 0, 0, ""

    account = read_json_safe(profile_path(base_dir)).get("oauthAccount")
    tier = ""
    if isinstance(account, dict):
        for key in ("userRateLimitTier", "organizationRateLimitTier"):
            val = str(account.get(key) or "")
            if val:
                tier = val
                break
    if not re.search(r"max_(\d+x)", tier, re.IGNORECASE):
        tier = str(login.get("rateLimitTier") or "")

    plan = plan_label(tier, str(login.get("subscriptionType") or ""))
    try:
        exp = int(login.get("expiresAt") or 0)
    except Exception:
        exp = 0
    try:
        rexp = int(login.get("refreshTokenExpiresAt") or 0)
    except Exception:
        rexp = 0
    return str(login.get("accessToken") or ""), exp, rexp, plan


def is_pid_alive(pid: int, proc_start: str | None = None) -> bool:
    """Check whether a Claude session PID is still alive (and not a recycled unrelated PID)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False

    if proc_start:
        try:
            stat_text = (proc_dir / "stat").read_text(encoding="utf-8", errors="replace")
            rparen = stat_text.rfind(")")
            if rparen != -1:
                fields = stat_text[rparen + 2:].split()
                # Field 22 in /proc/<pid>/stat (0-indexed 19 after comm) is starttime
                if len(fields) > 19 and fields[19] != str(proc_start):
                    return False
        except Exception:
            pass
    return True


def parse_active_sessions(sessions_dir: Path) -> dict[str, dict[str, Any]]:
    """Read live Claude Code sessions from ~/.claude/sessions/*.json, keyed by sessionId."""
    active: dict[str, dict[str, Any]] = {}
    if not sessions_dir.exists():
        return active

    for p in sessions_dir.glob("*.json"):
        data = read_json_safe(p)
        if not data:
            continue
        pid = data.get("pid")
        sid = sanitize_plain_text(data.get("sessionId") or "", 100)
        if not sid or not isinstance(pid, int):
            continue
        proc_start = str(data.get("procStart") or "") or None
        if not is_pid_alive(pid, proc_start):
            continue

        raw_status = str(data.get("status") or "").lower().strip()
        is_working = raw_status in ("busy", "working", "running")
        updated_sec = normalize_timestamp_seconds(data.get("statusUpdatedAt") or data.get("updatedAt") or data.get("startedAt"))
        existing = active.get(sid)
        if existing:
            existing["pids"].append(pid)
            existing["isWorking"] = existing["isWorking"] or is_working
            existing["updatedAt"] = max(existing["updatedAt"], updated_sec)
            if not existing["name"] and data.get("name"):
                existing["name"] = sanitize_plain_text(data.get("name"), 150)
            if not existing["cwd"] and data.get("cwd"):
                existing["cwd"] = sanitize_plain_text(data.get("cwd"), 300)
        else:
            active[sid] = {
                "sessionId": sid,
                "pid": pid,
                "pids": [pid],
                "isWorking": is_working,
                "status": raw_status or "idle",
                "cwd": sanitize_plain_text(data.get("cwd") or "", 300),
                "name": sanitize_plain_text(data.get("name") or "", 150),
                "entrypoint": sanitize_plain_text(data.get("entrypoint") or "", 60),
                "updatedAt": updated_sec,
            }
    return active


def kill_session(session_id: str, base_dir: Path) -> bool:
    """Find alive PIDs for sessionId in ~/.claude/sessions/*.json and terminate them cleanly."""
    sessions_dir = base_dir / "sessions"
    if not sessions_dir.exists():
        return False

    pids: set[int] = set()
    for p in sessions_dir.glob("*.json"):
        data = read_json_safe(p)
        if sanitize_plain_text(data.get("sessionId") or "", 100) != session_id:
            continue
        pid = data.get("pid")
        proc_start = str(data.get("procStart") or "") or None
        if isinstance(pid, int) and pid != os.getpid() and is_pid_alive(pid, proc_start):
            pids.add(pid)

    killed_any = False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed_any = True
        except Exception:
            pass
    return killed_any


def _extract_user_text(line: dict[str, Any]) -> str | None:
    message = line.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if not blocks or any(b.get("type") == "tool_result" for b in blocks):
            return None
        return next((str(b.get("text") or "") for b in blocks if b.get("type") == "text"), "")
    return None


def _is_typed_prompt(line: dict[str, Any]) -> tuple[bool, str]:
    """Return (is_prompt, display_text) for a Claude Code transcript line."""
    if line.get("isMeta") or line.get("isSidechain") or line.get("isCompactSummary"):
        return False, ""
    if line.get("type") == "attachment":
        queued = line.get("attachment")
        if not isinstance(queued, dict) or queued.get("type") != "queued_command" or queued.get("commandMode") != "prompt" or queued.get("isMeta"):
            return False, ""
        origin = queued.get("origin")
        if isinstance(origin, dict) and origin.get("kind") in CLAUDE_CODE_MACHINE_SOURCES:
            return False, ""
        return True, sanitize_plain_text(queued.get("prompt") or queued.get("text") or "", 250)

    if line.get("type") != "user" or line.get("promptSource") in CLAUDE_CODE_MACHINE_SOURCES:
        return False, ""
    text = _extract_user_text(line)
    if text is None or text.lstrip().startswith(CLAUDE_CODE_MACHINE_PROMPTS):
        return False, ""
    return True, sanitize_plain_text(text, 250)


def _local_command_parent(line: dict[str, Any]) -> str | None:
    if line.get("type") == "system":
        text = line.get("content") if isinstance(line.get("content"), str) else None
    elif line.get("type") == "user":
        text = _extract_user_text(line)
    else:
        return None
    parent = line.get("parentUuid")
    if text is not None and isinstance(parent, str) and text.lstrip().startswith(CLAUDE_CODE_LOCAL_OUTPUT):
        return parent
    return None


def history_prompt_is_real(display: Any) -> bool:
    text = str(display or "").strip()
    if not text.startswith("/"):
        return True
    name = text[1:].split(None, 1)[0].lower() if len(text) > 1 else ""
    return name not in CLAUDE_CODE_LOCAL_COMMANDS


def parse_history_file(history_path: Path) -> tuple[dict[str, dict[str, Any]], Counter]:
    """Read history.jsonl to enrich session prompts/workspaces and compute workspace frequencies."""
    session_meta: dict[str, dict[str, Any]] = {}
    workspace_counter: Counter = Counter()

    if not history_path.exists():
        return session_meta, workspace_counter

    try:
        with open(history_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                display = sanitize_plain_text(entry.get("display") or "", 250)
                if not history_prompt_is_real(display):
                    continue
                ws = sanitize_plain_text(entry.get("project") or entry.get("workspace") or "", 300)
                if ws:
                    workspace_counter[ws] += 1
                sid = sanitize_plain_text(entry.get("sessionId") or "", 100)
                ts_sec = normalize_timestamp_seconds(entry.get("timestamp"))
                if sid:
                    meta = session_meta.setdefault(sid, {
                        "firstPrompt": display,
                        "lastPrompt": display,
                        "workspace": ws,
                        "timestamp": ts_sec,
                        "prompts": 0,
                    })
                    meta["prompts"] += 1
                    if display:
                        meta["lastPrompt"] = display
                        if not meta["firstPrompt"]:
                            meta["firstPrompt"] = display
                    if ws:
                        meta["workspace"] = ws
                    meta["timestamp"] = max(meta["timestamp"], ts_sec)
    except Exception:
        pass

    return session_meta, workspace_counter


def _parse_single_transcript(path: Path, is_top_level: bool) -> dict[str, Any]:
    """Parse one Claude Code transcript JSONL file with streaming reply deduplication."""
    prompts: list[list[Any]] = []  # [uuid, day, text, ts_sec, model]
    prompted: set[str] = set()
    unanswered: list[list[Any]] = []
    replies: dict[str, dict[str, Any]] = {}
    tools: Counter = Counter()

    session_id = sanitize_plain_text(path.stem, 100)
    custom_title = ""
    last_prompt_meta = ""
    cwd = ""
    first_prompt = ""
    last_prompt = ""
    last_ts = 0.0
    latest_model = ""

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue

            if parsed.get("sessionId"):
                session_id = sanitize_plain_text(parsed["sessionId"], 100) or session_id
            if parsed.get("cwd") and not cwd:
                cwd = sanitize_plain_text(parsed["cwd"], 300)

            ltype = parsed.get("type")
            if ltype == "custom-title" and parsed.get("customTitle"):
                custom_title = sanitize_plain_text(parsed.get("customTitle"), 150)
                continue
            if ltype == "last-prompt" and parsed.get("lastPrompt"):
                last_prompt_meta = sanitize_plain_text(parsed.get("lastPrompt"), 250)
                continue

            message = parsed.get("message") if isinstance(parsed.get("message"), dict) else {}
            ts_raw = parsed.get("timestamp") if parsed.get("timestamp") is not None else message.get("timestamp")
            ts_sec = normalize_timestamp_seconds(ts_raw)
            if ts_sec > last_ts:
                last_ts = ts_sec
            day = local_date_from_timestamp(ts_raw) if ts_raw is not None else date_string(dt.datetime.now().date())

            cmd_parent = _local_command_parent(parsed)
            if cmd_parent is not None:
                prompts = [p for p in prompts if p[0] != cmd_parent]
                unanswered = [p for p in unanswered if p[0] != cmd_parent]
                continue

            if ltype in ("user", "attachment"):
                ok, ptext = _is_typed_prompt(parsed)
                if ok:
                    puuid = str(parsed.get("uuid") or f"{path}:{line_no}")
                    if puuid not in prompted:
                        prompted.add(puuid)
                        entry = [puuid, day, ptext, ts_sec, ""]
                        prompts.append(entry)
                        unanswered.append(entry)
                        if ptext:
                            if not first_prompt:
                                first_prompt = ptext
                            last_prompt = ptext
                continue

            if ltype != "assistant" and message.get("role") != "assistant":
                continue

            raw_model = str(message.get("model") or parsed.get("model") or "")
            if not raw_model or raw_model == "<synthetic>":
                continue
            model_name = format_claude_model_name(raw_model)
            if not model_name:
                continue

            usage = message.get("usage") or parsed.get("usage")
            in_tok = 0
            out_tok = 0
            cache_read = 0
            cache_write = 0
            if isinstance(usage, dict):
                in_tok = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
                out_tok = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
                cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens") or 0)
                cache_write = int(usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens") or 0)

            msg_tools: list[str] = []
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tname = sanitize_plain_text(block.get("name") or "", 80)
                        if tname:
                            msg_tools.append(tname)

            msg_id = str(message.get("id") or parsed.get("messageId") or "")
            req_id = str(parsed.get("requestId") or "")
            if msg_id:
                rkey = f"{msg_id}:{req_id}" if req_id else msg_id
            else:
                rkey = f"{path}:{parsed.get('uuid') or req_id or line_no}"

            sidechain = parsed.get("isSidechain") is True
            if not sidechain and (in_tok + out_tok + cache_read + cache_write > 0):
                for p in unanswered:
                    p[4] = model_name
                unanswered.clear()
                latest_model = model_name

            prev = replies.get(rkey)
            if prev is not None and out_tok < prev["out"]:
                continue

            replies[rkey] = {
                "key": rkey,
                "model": model_name,
                "day": day,
                "in": in_tok,
                "out": out_tok,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "tools": msg_tools,
            }

    for rep in replies.values():
        for tname in rep["tools"]:
            tools[tname] += 1

    if not latest_model and replies:
        latest_model = list(replies.values())[-1]["model"]

    return {
        "sessionId": session_id,
        "isTopLevel": is_top_level,
        "customTitle": custom_title,
        "lastPromptMeta": last_prompt_meta,
        "cwd": cwd,
        "firstPrompt": first_prompt,
        "lastPrompt": last_prompt,
        "lastTs": last_ts,
        "latestModel": latest_model,
        "prompts": prompts,
        "replies": list(replies.values()),
        "tools": dict(tools),
    }


def parse_transcripts(
    projects_dir: Path,
    today_str: str,
    recent_dates: list[str],
    default_model: str = "Claude Code",
    cache_dir: Path | None = None,
) -> tuple[Counter, dict[str, dict[str, Any]], list[dict[str, Any]], str, dict[str, dict[str, Any]], dict[str, int], dict[str, int], int, int, int]:
    """Parse all Claude Code project transcripts with mtime/size caching and cross-file deduplication."""
    tool_counter: Counter = Counter()
    models_stats: dict[str, dict[str, Any]] = {}
    session_records: dict[str, dict[str, Any]] = {}
    daily_prompts: dict[str, int] = {d: 0 for d in recent_dates}
    daily_steps: dict[str, int] = {d: 0 for d in recent_dates}
    recent_dates_set = set(recent_dates)
    latest_model = default_model

    if not projects_dir.exists():
        return tool_counter, {}, [], latest_model, session_records, daily_prompts, daily_steps, 0, 0, 0

    cache_file = (cache_dir / "transcript_stats_cache.json") if cache_dir else None
    tz_sig = [os.environ.get("TZ"), list(time.tzname), time.timezone, time.altzone]
    cache_files: dict[str, Any] = {}
    if cache_file and cache_file.exists():
        loaded = read_json_safe(cache_file)
        if loaded.get("version") == TRANSCRIPT_CACHE_VERSION and loaded.get("zone") == tz_sig and isinstance(loaded.get("files"), dict):
            cache_files = loaded["files"]

    cache_dirty = False
    new_cache_files: dict[str, Any] = {}
    transcript_files = sorted(projects_dir.rglob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)

    dedup_prompts: dict[str, tuple[str, str, str]] = {}  # uuid -> (day, session_id, model)
    dedup_replies: dict[str, dict[str, Any]] = {}  # reply_key -> reply
    found_latest_model = False

    for p in transcript_files:
        try:
            st = p.stat()
            mtime = st.st_mtime_ns
            size = st.st_size
        except Exception:
            continue

        fkey = str(p)
        is_top_level = p.parent.parent == projects_dir
        cached = cache_files.get(fkey)
        if cached and cached.get("mtime") == mtime and cached.get("size") == size:
            parsed = cached["data"]
        else:
            try:
                parsed = _parse_single_transcript(p, is_top_level)
                cache_dirty = True
            except Exception:
                continue

        new_cache_files[fkey] = {"mtime": mtime, "size": size, "data": parsed}

        if not found_latest_model and parsed.get("latestModel"):
            latest_model = parsed["latestModel"]
            found_latest_model = True

        sid = parsed.get("sessionId") or p.stem
        # Track session card info from top-level transcripts
        if parsed.get("isTopLevel"):
            ts = float(parsed.get("lastTs") or 0) or (mtime / 1e9)
            step_cnt = len(parsed.get("prompts") or []) + len(parsed.get("replies") or [])
            prev_s = session_records.get(sid)
            if not prev_s or ts > prev_s["timestamp"]:
                session_records[sid] = {
                    "conversationId": sid,
                    "firstPrompt": parsed.get("customTitle") or parsed.get("firstPrompt") or "",
                    "lastPrompt": parsed.get("lastPromptMeta") or parsed.get("lastPrompt") or parsed.get("firstPrompt") or "",
                    "workspace": parsed.get("cwd") or "",
                    "timestamp": ts,
                    "stepCount": step_cnt,
                    "model": parsed.get("latestModel") or default_model,
                }

        # Deduplicate prompts & replies across forks/resumes
        for pr in parsed.get("prompts") or []:
            puuid, pday, _, _, pmodel = pr
            if puuid not in dedup_prompts or (pmodel and not dedup_prompts[puuid][2]):
                dedup_prompts[puuid] = (pday, sid, pmodel or default_model)

        for rep in parsed.get("replies") or []:
            rkey = rep["key"]
            held = dedup_replies.get(rkey)
            if held is not None and rep["out"] < held["out"]:
                continue
            dedup_replies[rkey] = {**rep, "sessionId": sid}

    if len(new_cache_files) != len(cache_files):
        cache_dirty = True

    if cache_dirty and cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_c = cache_file.with_suffix(".tmp")
            with open(tmp_c, "w", encoding="utf-8") as f:
                json.dump({"version": TRANSCRIPT_CACHE_VERSION, "zone": tz_sig, "files": new_cache_files}, f)
            tmp_c.replace(cache_file)
        except Exception:
            pass

    def ensure_model(mname: str) -> dict[str, Any]:
        if mname not in models_stats:
            models_stats[mname] = {
                "name": mname,
                "prompts": 0,
                "steps": 0,
                "todayPrompts": 0,
                "todaySteps": 0,
                "weekPrompts": 0,
                "weekSteps": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "totalTokens": 0,
                "todayTokens": 0,
                "weekTokens": 0,
                "sessions": set(),
                "todaySessions": set(),
                "weekSessions": set(),
            }
        return models_stats[mname]

    total_prompts = 0
    total_steps = 0
    total_tokens = 0
    today_steps = 0
    today_tokens_by_model: dict[str, int] = {}

    for pday, sid, mname in dedup_prompts.values():
        total_prompts += 1
        total_steps += 1
        if pday in daily_prompts:
            daily_prompts[pday] += 1
        if pday in daily_steps:
            daily_steps[pday] += 1
        if pday == today_str:
            today_steps += 1

        ms = ensure_model(mname or default_model)
        ms["prompts"] += 1
        ms["steps"] += 1
        ms["sessions"].add(sid)
        if pday == today_str:
            ms["todayPrompts"] += 1
            ms["todaySteps"] += 1
            ms["todaySessions"].add(sid)
        if pday in recent_dates_set:
            ms["weekPrompts"] += 1
            ms["weekSteps"] += 1
            ms["weekSessions"].add(sid)

    for rep in dedup_replies.values():
        mname = rep["model"] or default_model
        rday = rep["day"]
        sid = rep["sessionId"]
        tok_sum = rep["in"] + rep["out"] + rep["cacheRead"] + rep["cacheWrite"]
        total_steps += 1
        total_tokens += tok_sum
        if rday in daily_steps:
            daily_steps[rday] += 1
        if rday == today_str:
            today_steps += 1
            if tok_sum > 0:
                today_tokens_by_model[mname] = today_tokens_by_model.get(mname, 0) + tok_sum

        for tname in rep.get("tools") or []:
            tool_counter[tname] += 1

        ms = ensure_model(mname)
        ms["steps"] += 1
        ms["inputTokens"] += rep["in"]
        ms["outputTokens"] += rep["out"]
        ms["cacheReadTokens"] += rep["cacheRead"]
        ms["cacheWriteTokens"] += rep["cacheWrite"]
        ms["totalTokens"] += tok_sum
        ms["sessions"].add(sid)
        if rday == today_str:
            ms["todaySteps"] += 1
            ms["todayTokens"] += tok_sum
            ms["todaySessions"].add(sid)
        if rday in recent_dates_set:
            ms["weekSteps"] += 1
            ms["weekTokens"] += tok_sum
            ms["weekSessions"].add(sid)

    formatted_models: dict[str, dict[str, Any]] = {}
    model_list: list[dict[str, Any]] = []
    total_model_prompts = sum(d["prompts"] for d in models_stats.values()) or 1

    for m, data in sorted(models_stats.items(), key=lambda item: (item[1]["prompts"], item[1]["steps"], item[1]["totalTokens"]), reverse=True):
        clean_name = sanitize_plain_text(m, 80)
        p_count = data["prompts"]
        share_frac = round(p_count / max(1, total_model_prompts), 4)
        share_pct = round(share_frac * 100, 1)

        entry = {
            "name": clean_name,
            "prompts": p_count,
            "steps": data["steps"],
            "todayPrompts": data["todayPrompts"],
            "todaySteps": data["todaySteps"],
            "todaySessions": len(data["todaySessions"]),
            "weekPrompts": data["weekPrompts"],
            "weekSteps": data["weekSteps"],
            "weekSessions": len(data["weekSessions"]),
            "sessions": len(data["sessions"]),
            "shareFraction": share_frac,
            "sharePercent": share_pct,
            "color": model_family_color(clean_name),
            "inputTokens": data["inputTokens"],
            "outputTokens": data["outputTokens"],
            "cacheReadTokens": data["cacheReadTokens"],
            "cacheWriteTokens": data["cacheWriteTokens"],
            "totalTokens": data["totalTokens"],
            "todayTokens": data["todayTokens"],
            "weekTokens": data["weekTokens"],
        }
        formatted_models[clean_name] = entry
        model_list.append(entry)

    return (
        tool_counter,
        formatted_models,
        model_list,
        latest_model,
        session_records,
        daily_prompts,
        daily_steps,
        total_prompts,
        total_steps,
        total_tokens,
    )


def format_hours_duration(hours: float) -> str:
    """Format duration in hours to compact human readable string (e.g. 45m, 2.5h, 1d 4h)."""
    if hours <= 0:
        return "0m"
    if hours < 1.0:
        return f"{max(1, round(hours * 60))}m"
    if hours < 24.0:
        return f"{hours:.1f}h"
    days = int(hours // 24)
    rem_h = int(round(hours % 24))
    if rem_h >= 24:
        days += 1
        rem_h = 0
    return f"{days}d {rem_h}h" if rem_h > 0 else f"{days}d"


def compute_bucket_forecast(rem_pct: float, burn_rate: float, reset_time_str: str) -> tuple[str, str, str]:
    """Compute (burn_rate_text, forecast_text, forecast_status) for a quota bucket."""
    burn_text = f"{burn_rate:.1f}%/h" if burn_rate >= 0.1 else ""

    hours_until_reset = None
    if reset_time_str:
        try:
            reset_dt = dt.datetime.fromisoformat(reset_time_str.replace("Z", "+00:00"))
            now_dt = dt.datetime.now(dt.timezone.utc)
            hours_until_reset = max(0.0, (reset_dt - now_dt).total_seconds() / 3600.0)
        except Exception:
            hours_until_reset = None

    if burn_rate < 0.1:
        return "", "Paced to reset", "stable"

    hours_to_depletion = rem_pct / max(0.01, burn_rate)

    if hours_until_reset is not None:
        projected_at_reset = rem_pct - (burn_rate * hours_until_reset)
        if projected_at_reset >= 15.0:
            forecast_text = f"On pace · ~{round(projected_at_reset)}% at reset"
            status = "safe"
        elif projected_at_reset > 0.0:
            forecast_text = f"Tight pace · ~{round(projected_at_reset)}% at reset"
            status = "warning"
        else:
            forecast_text = f"Depletes in ~{format_hours_duration(hours_to_depletion)} (before reset)"
            status = "critical"
    else:
        if hours_to_depletion > 12.0:
            forecast_text = f"~{format_hours_duration(hours_to_depletion)} quota left"
            status = "safe"
        elif hours_to_depletion > 3.0:
            forecast_text = f"~{format_hours_duration(hours_to_depletion)} quota left"
            status = "warning"
        else:
            forecast_text = f"Depletes in ~{format_hours_duration(hours_to_depletion)}"
            status = "critical"

    return burn_text, forecast_text, status


def update_quota_snapshots(cache_dir: Path, raw_groups: list[dict[str, Any]]) -> dict[str, float]:
    """Record timestamped snapshot of quota fractions and calculate hourly burn rates per bucket."""
    now = time.time()
    snapshots_path = cache_dir / "quota_snapshots.json"
    snapshots_path.parent.mkdir(parents=True, exist_ok=True)

    current_snapshot: dict[str, float] = {}
    for g in raw_groups:
        for b in g.get("buckets", []):
            b_id = sanitize_plain_text(b.get("id", ""), 50)
            if b_id:
                rem_frac = float(b.get("remaining_fraction", 1.0))
                current_snapshot[b_id] = round(min(1.0, max(0.0, rem_frac)), 4)

    if not current_snapshot:
        return {}

    snapshots: list[dict[str, Any]] = []
    if snapshots_path.exists():
        try:
            with open(snapshots_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    cutoff = now - 48 * 3600
                    snapshots = [s for s in data if isinstance(s, dict) and s.get("timestamp", 0) > cutoff]
        except Exception:
            snapshots = []

    if snapshots and (now - snapshots[-1].get("timestamp", 0) < 60):
        snapshots[-1]["buckets"] = current_snapshot
    else:
        snapshots.append({"timestamp": now, "buckets": current_snapshot})

    try:
        tmp_snap = snapshots_path.with_suffix(".tmp")
        with open(tmp_snap, "w", encoding="utf-8") as f:
            json.dump(snapshots[-120:], f)
        tmp_snap.replace(snapshots_path)
    except Exception:
        pass

    burn_rates: dict[str, float] = {}
    for b_id, current_frac in current_snapshot.items():
        baseline_snap = None
        best_delta_t = 0.0

        for s in reversed(snapshots[:-1]):
            t_diff = now - s.get("timestamp", 0)
            if t_diff < 180:
                continue
            prev_frac = s.get("buckets", {}).get(b_id)
            if prev_frac is None:
                continue
            if prev_frac < current_frac - 0.05:
                break
            baseline_snap = s
            best_delta_t = t_diff
            if 3600 <= t_diff <= 7200:
                break

        burn_rate = 0.0
        if baseline_snap and best_delta_t >= 300:
            prev_frac = baseline_snap.get("buckets", {}).get(b_id, current_frac)
            frac_used = max(0.0, prev_frac - current_frac)
            hours = best_delta_t / 3600.0
            if hours > 0:
                burn_rate = round((frac_used * 100.0) / hours, 2)

        burn_rates[b_id] = burn_rate

    return burn_rates


def _normalize_utilization(value: Any) -> float:
    try:
        n = float(str(value).strip().replace("%", ""))
    except Exception:
        return -1.0
    if not (n >= 0):
        return -1.0
    return min(1.0, n / 100.0)


def _normalize_reset_at(value: Any) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if raw.isdigit():
        ts = int(raw)
        if ts < 1e12:
            ts *= 1000
        try:
            return dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).isoformat()
        except Exception:
            return raw
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except Exception:
        return raw


def _limits_to_groups(limits: list[dict[str, Any]], tier_label: str = "") -> list[dict[str, Any]]:
    """Convert flat Claude usage limits (used fraction 0..1) into grouped buckets (remaining fraction)."""
    if not limits:
        return []

    core_buckets: list[dict[str, Any]] = []
    scoped_buckets: list[dict[str, Any]] = []

    for idx, lim in enumerate(limits):
        if not isinstance(lim, dict):
            continue
        label = sanitize_plain_text(lim.get("title") or lim.get("label") or f"Limit {idx + 1}", 80)
        used_frac = min(1.0, max(0.0, float(lim.get("percent") or 0.0)))
        rem_frac = round(max(0.0, 1.0 - used_frac), 4)
        reset_at = sanitize_plain_text(lim.get("resetsAt") or "", 60)

        low = label.lower()
        is_session = "5-hour" in low or "5h" in low or "session" in low
        is_weekly = "7-day" in low or "weekly" in low
        bid = re.sub(r"[^a-z0-9_-]+", "-", low).strip("-") or f"limit-{idx}"
        window = "5h" if is_session else ("weekly" if is_weekly else "scoped")

        bucket = {
            "id": bid,
            "name": label,
            "label": "5-Hour Session" if is_session else ("Weekly (7-Day)" if low.startswith("weekly") else label),
            "window": window,
            "remaining_fraction": rem_frac,
            "reset_time": reset_at,
            "description": "",
        }
        if is_session or low.startswith("weekly"):
            core_buckets.append(bucket)
        else:
            scoped_buckets.append(bucket)

    groups: list[dict[str, Any]] = []
    if core_buckets:
        sub_name = f"Claude Subscription ({tier_label})" if tier_label and tier_label != "Anthropic" else "Claude Subscription"
        groups.append({
            "name": sub_name,
            "description": "Account-wide rolling usage limits",
            "buckets": core_buckets,
        })
    if scoped_buckets:
        groups.append({
            "name": "Model-Specific Limits",
            "description": "Per-model rolling allowance",
            "buckets": scoped_buckets,
        })
    return groups


def _probe_anthropic_oauth(base_dir: Path) -> dict[str, Any]:
    """Direct OAuth usage probe fallback when omarchy-agent-usage-claude is unavailable."""
    access_token, exp_ms, rexp_ms, plan = read_oauth_info(base_dir)
    if not access_token:
        return {"ok": False, "tierLabel": plan, "usageStatusText": "Waiting for auth", "authHelpText": "Run `claude auth login` to restore live usage."}
    now_ms = time.time() * 1000
    if exp_ms > 0 and exp_ms <= now_ms:
        if rexp_ms > now_ms:
            return {"ok": False, "tierLabel": plan, "usageStatusText": "Limits paused", "authHelpText": "Claude access token lapsed. Start `claude` to refresh it."}
        return {"ok": False, "tierLabel": plan, "usageStatusText": "Sign-in expired", "authHelpText": "Claude sign-in expired. Run `claude auth login`."}

    req = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={
            "Authorization": "Bearer " + access_token,
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as err:
        if err.code == 429:
            return {"ok": False, "tierLabel": plan, "rateLimited": True, "usageStatusText": "", "authHelpText": "Anthropic usage endpoint rate limited. Showing cached/local stats."}
        if err.code in (401, 403):
            return {"ok": False, "tierLabel": plan, "usageStatusText": "Sign-in expired", "authHelpText": "Anthropic refused Claude Code's sign-in. Run `claude auth login`."}
        return {"ok": False, "tierLabel": plan, "usageStatusText": "", "authHelpText": f"Anthropic usage endpoint returned HTTP {err.code}."}
    except Exception:
        return {"ok": False, "tierLabel": plan, "usageStatusText": "", "authHelpText": "Couldn't reach Anthropic usage endpoint."}

    if not isinstance(payload, dict):
        return {"ok": False, "tierLabel": plan, "usageStatusText": "", "authHelpText": "Invalid usage response."}

    limits: list[dict[str, Any]] = []
    five_h = payload.get("five_hour")
    if isinstance(five_h, dict):
        p = _normalize_utilization(five_h.get("utilization"))
        if p >= 0:
            limits.append({"label": "Session (5-hour)", "percent": p, "resetsAt": _normalize_reset_at(five_h.get("resets_at"))})
    seven_d = payload.get("seven_day")
    if isinstance(seven_d, dict):
        p = _normalize_utilization(seven_d.get("utilization"))
        if p >= 0:
            limits.append({"label": "Weekly (7-day)", "percent": p, "resetsAt": _normalize_reset_at(seven_d.get("resets_at"))})

    for entry in payload.get("limits") if isinstance(payload.get("limits"), list) else []:
        if not isinstance(entry, dict):
            continue
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if not isinstance(model, dict):
            continue
        name = sanitize_plain_text(model.get("display_name") or model.get("id") or "", 60)
        p = _normalize_utilization(entry.get("percent"))
        if name and p >= 0:
            kind = str(entry.get("kind") or "").lower()
            win = "Monthly" if "month" in kind else ("Weekly" if ("week" in kind or "day" in kind) else ("Session" if ("hour" in kind or "session" in kind) else ""))
            title = f"{name} {win}".strip()
            limits.append({"label": title, "title": title, "percent": p, "resetsAt": _normalize_reset_at(entry.get("resets_at"))})

    if not limits:
        return {"ok": False, "tierLabel": plan, "usageStatusText": "", "authHelpText": "Anthropic usage endpoint returned no limits."}

    return {"ok": True, "tierLabel": plan, "groups": _limits_to_groups(limits, plan), "usageStatusText": "", "authHelpText": ""}


def _query_live_quota(base_dir: Path, force: bool = False) -> dict[str, Any]:
    """Query live Claude quota via omarchy-agent-usage-claude (shared LimitsCache) or direct OAuth fallback."""
    omarchy_bin = shutil.which("omarchy-agent-usage-claude")
    use_omarchy = bool(omarchy_bin) and base_dir.resolve() == default_base_dir()
    if use_omarchy and omarchy_bin:
        try:
            cmd = [omarchy_bin, "--force"] if force else [omarchy_bin, "--limits-only"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=14)
            if res.returncode == 0 and res.stdout.strip():
                payload = json.loads(res.stdout)
                if isinstance(payload, dict):
                    limits = payload.get("limits") or []
                    tier = sanitize_plain_text(payload.get("tierLabel") or "", 40)
                    groups = _limits_to_groups(limits, tier)
                    return {
                        "ok": len(groups) > 0,
                        "tierLabel": tier,
                        "groups": groups,
                        "usageStatusText": sanitize_plain_text(payload.get("usageStatusText") or "", 100),
                        "authHelpText": sanitize_plain_text(payload.get("authHelpText") or "", 220),
                        "limitsFetchedAt": int(payload.get("limitsFetchedAt") or 0),
                    }
        except Exception:
            pass

    return _probe_anthropic_oauth(base_dir)


def trigger_bg_quota_refresh(base_dir: Path, cache_dir: Path) -> None:
    flag_file = cache_dir / "quota_refresh.flag"
    now = time.time()
    if flag_file.exists():
        try:
            if now - flag_file.stat().st_mtime < 30:
                return
        except Exception:
            pass
    try:
        flag_file.parent.mkdir(parents=True, exist_ok=True)
        flag_file.touch()
        script_path = str(Path(__file__).resolve())
        subprocess.Popen(
            [sys.executable, script_path, "--refresh-quota-bg", str(base_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def bg_refresh_quota(base_dir: Path) -> None:
    cache_dir = resolve_cache_dir(base_dir)
    cache_path = cache_dir / "quota_usage_cache.json"
    flag_file = cache_dir / "quota_refresh.flag"
    try:
        res = _query_live_quota(base_dir, force=False)
        if res.get("groups"):
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(res, f)
            tmp.replace(cache_path)
            update_quota_snapshots(cache_dir, res["groups"])
    except Exception:
        pass
    finally:
        try:
            flag_file.unlink(missing_ok=True)
        except Exception:
            pass


def fetch_claude_usage_quota(base_dir: Path, cache_dir: Path, force: bool = False) -> dict[str, Any]:
    cache_path = cache_dir / "quota_usage_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and cache_path.exists():
        try:
            age = time.time() - cache_path.stat().st_mtime
            data = read_json_safe(cache_path)
            if isinstance(data, dict) and data.get("groups"):
                if age > 180:
                    trigger_bg_quota_refresh(base_dir, cache_dir)
                return data
        except Exception:
            pass

    live = _query_live_quota(base_dir, force=force)
    if live.get("groups"):
        try:
            tmp = cache_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(live, f)
            tmp.replace(cache_path)
        except Exception:
            pass
        return live

    if cache_path.exists():
        stale = read_json_safe(cache_path)
        if stale.get("groups"):
            if live.get("authHelpText"):
                stale["authHelpText"] = live["authHelpText"]
            if live.get("usageStatusText"):
                stale["usageStatusText"] = live["usageStatusText"]
            return stale

    return live


def format_quota_groups(raw_data: dict[str, Any], cache_dir: Path | None = None) -> list[dict[str, Any]]:
    groups = raw_data.get("groups") or []
    burn_rates = update_quota_snapshots(cache_dir, groups) if (cache_dir and groups) else {}

    if not groups:
        return []

    formatted: list[dict[str, Any]] = []
    for g in groups:
        g_name = sanitize_plain_text(g.get("name", "Claude Subscription"), 80)
        is_scoped = "model" in g_name.lower()
        g_color = "#A855F7" if is_scoped else "#D97757"

        buckets: list[dict[str, Any]] = []
        for b in g.get("buckets", []):
            b_id = sanitize_plain_text(b.get("id", ""), 50)
            b_name = sanitize_plain_text(b.get("name", "Limit"), 100)
            b_win = sanitize_plain_text(b.get("window", ""), 20)
            rem_frac = min(1.0, max(0.0, float(b.get("remaining_fraction", 1.0))))
            rem_pct = min(100, max(0, round(rem_frac * 100)))
            used_pct = 100 - rem_pct
            reset_time = sanitize_plain_text(b.get("reset_time", ""), 60)
            label = sanitize_plain_text(b.get("label") or b_name, 80)

            burn_rate = burn_rates.get(b_id, 0.0)
            burn_text, forecast_text, forecast_status = compute_bucket_forecast(rem_pct, burn_rate, reset_time)

            buckets.append({
                "id": b_id,
                "name": b_name,
                "label": label,
                "window": b_win,
                "remainingFraction": round(rem_frac, 4),
                "remainingPercent": rem_pct,
                "usedPercent": used_pct,
                "resetTime": reset_time,
                "description": sanitize_plain_text(b.get("description", ""), 250),
                "color": g_color,
                "burnRatePerHour": burn_rate,
                "burnRateText": burn_text,
                "forecastText": forecast_text,
                "forecastStatus": forecast_status,
            })

        formatted.append({
            "name": g_name,
            "description": sanitize_plain_text(g.get("description", ""), 250),
            "color": g_color,
            "buckets": buckets,
        })
    return formatted


def check_and_send_quota_notifications(
    cache_dir: Path,
    quota_groups: list[dict[str, Any]],
    threshold_pct: int = 15,
) -> None:
    """Send unified desktop notification when Claude quota drops below threshold (concurrency-safe)."""
    if not quota_groups:
        return

    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / "quota_notification.lock"
    state_path = cache_dir / "quota_notification_state.json"

    lock_fd = None
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        if lock_fd:
            try:
                lock_fd.close()
            except Exception:
                pass
        return

    try:
        now = time.time()
        state_data = read_json_safe(state_path)
        modified = False

        for group in quota_groups:
            g_name = group.get("name") or "Claude Subscription"
            buckets = group.get("buckets") or []
            if not buckets:
                continue

            low_buckets = []
            min_pct = 100
            for b in buckets:
                rem_pct = b.get("remainingPercent")
                if rem_pct is None:
                    rem_pct = round(b.get("remainingFraction", 1.0) * 100)
                if rem_pct <= threshold_pct:
                    label = b.get("label") or b.get("name") or "Limit"
                    low_buckets.append(f"{label} ({rem_pct}%)")
                    min_pct = min(min_pct, rem_pct)

            group_state = state_data.get(g_name, {}) if isinstance(state_data.get(g_name), dict) else {}
            was_low = bool(group_state.get("alert_active", False))
            last_notified_pct = group_state.get("last_notified_pct", 100)
            last_notified_time = float(group_state.get("last_notified_time", 0.0))

            if not low_buckets:
                if was_low:
                    group_state["alert_active"] = False
                    group_state["last_notified_pct"] = 100
                    state_data[g_name] = group_state
                    modified = True
                continue

            should_notify = False
            if not was_low:
                should_notify = True
            elif min_pct <= 5 and last_notified_pct > 5 and (now - last_notified_time > 3600):
                should_notify = True

            if should_notify:
                headline = f"Claude Quota Low ({min_pct}% remaining)"
                details = f"{g_name}: {', '.join(low_buckets)} remaining."
                urgency = "critical" if min_pct <= 5 else "normal"
                cmd = [
                    "omarchy-notification-send",
                    "--app-name", "claude-usage",
                    "-u", urgency,
                    "-g", "󰚩",
                    headline,
                    details,
                ]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=4)
                    group_state["alert_active"] = True
                    group_state["last_notified_pct"] = min_pct
                    group_state["last_notified_time"] = now
                    state_data[g_name] = group_state
                    modified = True
                except Exception:
                    pass
            elif not was_low:
                group_state["alert_active"] = True
                state_data[g_name] = group_state
                modified = True

        if modified:
            try:
                tmp_state = state_path.with_suffix(".tmp")
                with open(tmp_state, "w", encoding="utf-8") as f:
                    json.dump(state_data, f, indent=2)
                tmp_state.replace(state_path)
            except Exception:
                pass
    finally:
        try:
            if lock_fd:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:
            pass


def empty_result(base_dir: Path | None = None) -> dict[str, Any]:
    recent_dates = recent_date_strings()
    current_model = read_configured_model(base_dir)
    return {
        "schemaVersion": 1,
        "id": "claude",
        "name": "Claude Code",
        "ready": False,
        "active": False,
        "activeStatus": "Idle",
        "hasActiveSession": False,
        "hasLocalStats": False,
        "tierLabel": "Anthropic",
        "currentModel": current_model,
        "todayPrompts": 0,
        "todaySessions": 0,
        "todaySteps": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [{"date": day, "messageCount": 0, "prompts": 0, "steps": 0} for day in recent_dates],
        "totalPrompts": 0,
        "totalSessions": 0,
        "totalSteps": 0,
        "totalTokens": 0,
        "activeSessions": [],
        "recentSessions": [],
        "toolUsage": {},
        "modelUsage": {},
        "modelList": [],
        "quotaGroups": [],
        "limits": [],
        "recentWorkspaces": [],
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "quotaUpdatedAt": "",
        "quotaUpdatedMs": 0,
        "lastFullRefreshMs": 0,
        "usageStatusText": "No Claude Code data found",
        "authHelpText": "Run `claude` to start a session.",
    }


def scan(base_dir: Path, force: bool = False, alert_threshold: int | None = None) -> dict[str, Any]:
    if not base_dir.exists():
        return empty_result(base_dir)

    cache_dir = resolve_cache_dir(base_dir)
    scan_cache_path = cache_dir / "scanner_cache.json"
    if not force and scan_cache_path.exists():
        try:
            if time.time() - scan_cache_path.stat().st_mtime < 1.5:
                cached = read_json_safe(scan_cache_path)
                if cached.get("ready"):
                    if alert_threshold is not None:
                        check_and_send_quota_notifications(cache_dir, cached.get("quotaGroups", []), threshold_pct=alert_threshold)
                    return cached
        except Exception:
            pass

    today_date = dt.datetime.now().date()
    today_str = date_string(today_date)
    recent_dates = recent_date_strings()

    configured_model = read_configured_model(base_dir)
    _, _, _, oauth_plan = read_oauth_info(base_dir)
    active_map = parse_active_sessions(base_dir / "sessions")
    history_meta, ws_counter = parse_history_file(base_dir / "history.jsonl")

    (
        tool_counter,
        model_usage_dict,
        model_list,
        latest_model,
        session_records,
        daily_prompts,
        daily_steps,
        total_prompts,
        total_steps,
        total_tokens,
    ) = parse_transcripts(
        base_dir / "projects",
        today_str,
        recent_dates,
        default_model=configured_model,
        cache_dir=cache_dir,
    )

    raw_quota = fetch_claude_usage_quota(base_dir, cache_dir, force=force)
    quota_groups = format_quota_groups(raw_quota, cache_dir=cache_dir)

    if alert_threshold is not None:
        check_and_send_quota_notifications(cache_dir, quota_groups, threshold_pct=alert_threshold)

    # Merge session records from transcripts, history.jsonl, and live sessions/*.json
    conv_map: dict[str, dict[str, Any]] = {}
    for sid, rec in session_records.items():
        conv_map[sid] = dict(rec)
        if rec.get("workspace"):
            ws_counter[rec["workspace"]] = max(ws_counter[rec["workspace"]], 1)

    for sid, hmeta in history_meta.items():
        if sid not in conv_map:
            conv_map[sid] = {
                "conversationId": sid,
                "firstPrompt": hmeta.get("firstPrompt", ""),
                "lastPrompt": hmeta.get("lastPrompt", ""),
                "workspace": hmeta.get("workspace", ""),
                "timestamp": hmeta.get("timestamp", 0),
                "stepCount": hmeta.get("prompts", 0),
                "model": configured_model,
            }
        else:
            if not conv_map[sid].get("workspace") and hmeta.get("workspace"):
                conv_map[sid]["workspace"] = hmeta["workspace"]
            if not conv_map[sid].get("firstPrompt") and hmeta.get("firstPrompt"):
                conv_map[sid]["firstPrompt"] = hmeta["firstPrompt"]
            conv_map[sid]["timestamp"] = max(conv_map[sid]["timestamp"], hmeta.get("timestamp", 0))

    for sid, act in active_map.items():
        if sid not in conv_map:
            conv_map[sid] = {
                "conversationId": sid,
                "firstPrompt": act.get("name") or f"Session {sid[:8]}",
                "lastPrompt": act.get("name") or "Active Session",
                "workspace": act.get("cwd") or "",
                "timestamp": act.get("updatedAt") or time.time(),
                "stepCount": 0,
                "model": latest_model,
            }
        else:
            if not conv_map[sid].get("workspace") and act.get("cwd"):
                conv_map[sid]["workspace"] = act["cwd"]
            if not conv_map[sid].get("firstPrompt") and act.get("name"):
                conv_map[sid]["firstPrompt"] = act["name"]
            conv_map[sid]["timestamp"] = max(conv_map[sid]["timestamp"], act.get("updatedAt") or 0)

    def session_sort_key(c: dict[str, Any]) -> tuple[int, float]:
        sid = c["conversationId"]
        return (1 if sid in active_map else 0, float(c.get("timestamp") or 0))

    sorted_convs = sorted(conv_map.values(), key=session_sort_key, reverse=True)

    all_sessions: list[dict[str, Any]] = []
    active_sessions: list[dict[str, Any]] = []
    any_session_working = False

    for item in sorted_convs:
        sid = item["conversationId"]
        act_info = active_map.get(sid)
        is_active = act_info is not None
        is_working = bool(act_info and act_info.get("isWorking"))
        if is_working:
            any_session_working = True

        clean_ws = sanitize_plain_text(item.get("workspace", ""), 300)
        ws_name = Path(clean_ws).name if clean_ws else "Workspace"
        ts_sec = float(item.get("timestamp") or 0)
        date_str = local_date_from_timestamp(ts_sec) if ts_sec else today_str
        iso_mod = dt.datetime.fromtimestamp(ts_sec, tz=dt.timezone.utc).isoformat() if ts_sec else ""

        first_p = str(item.get("firstPrompt") or "").strip()
        last_p = str(item.get("lastPrompt") or "").strip()
        title = first_p or last_p or f"Session {sid[:8]}"
        preview = first_p or last_p or title
        if len(preview) < 12 and len(last_p) > len(preview):
            preview = last_p

        s_item = {
            "conversationId": sid,
            "title": sanitize_plain_text(title, 150),
            "preview": sanitize_plain_text(preview, 250),
            "stepCount": int(item.get("stepCount") or 0),
            "lastModified": iso_mod,
            "date": date_str,
            "workspace": clean_ws,
            "workspaceName": sanitize_plain_text(ws_name, 80),
            "status": "working" if is_working else ("active" if is_active else "idle"),
            "agentName": sanitize_plain_text(item.get("model") or "Claude Code", 80),
            "notFullyIdle": is_working,
            "killed": False,
            "isActive": is_active,
        }
        all_sessions.append(s_item)
        if is_active:
            active_sessions.append(s_item)

    has_active_session = len(active_map) > 0
    if has_active_session:
        active_status = "Working" if any_session_working else "Waiting"
    else:
        active_status = "Idle"

    today_prompts = daily_prompts.get(today_str, 0)
    today_steps = daily_steps.get(today_str, 0)
    today_total_tokens = sum(m.get("todayTokens", 0) for m in model_list)
    today_tokens_by_model = {m["name"]: m["todayTokens"] for m in model_list if m.get("todayTokens", 0) > 0}
    today_sessions = len({s["conversationId"] for s in all_sessions if s["date"] == today_str}) or (1 if has_active_session else 0)

    recent_days_data = [
        {
            "date": day,
            "messageCount": daily_prompts.get(day, 0),
            "prompts": daily_prompts.get(day, 0),
            "steps": daily_steps.get(day, 0),
        }
        for day in recent_dates
    ]

    limits = []
    for g in quota_groups:
        for b in g.get("buckets", []):
            limits.append({
                "group": b.get("id", ""),
                "groupName": g.get("name", ""),
                "title": f"{g.get('name', '')} {b.get('label', '')}",
                "icon": "",
                "color": b.get("color", "#D97757"),
                "used": b.get("usedPercent", 0),
                "allowance": 100,
                "percent": round(1.0 - b.get("remainingFraction", 1.0), 3),
                "resetsAt": b.get("resetTime", ""),
                "burnRatePerHour": b.get("burnRatePerHour", 0.0),
                "burnRateText": b.get("burnRateText", ""),
                "forecastText": b.get("forecastText", ""),
                "forecastStatus": b.get("forecastStatus", "stable"),
            })

    recent_workspaces = [
        {"path": sanitize_plain_text(ws, 300), "name": sanitize_plain_text(Path(ws).name, 100), "count": count}
        for ws, count in ws_counter.most_common(5)
    ]
    tools_dict = {sanitize_plain_text(k, 80): v for k, v in tool_counter.most_common(10)}
    clean_latest_model = sanitize_plain_text(latest_model, 80)

    quota_cache_path = cache_dir / "quota_usage_cache.json"
    quota_updated_at = ""
    quota_updated_ms = int(raw_quota.get("limitsFetchedAt") or 0)
    if quota_cache_path.exists():
        try:
            q_mtime = quota_cache_path.stat().st_mtime
            quota_updated_at = dt.datetime.fromtimestamp(q_mtime, tz=dt.timezone.utc).isoformat()
            if not quota_updated_ms:
                quota_updated_ms = int(q_mtime * 1000)
        except Exception:
            pass

    tier_label = sanitize_plain_text(raw_quota.get("tierLabel") or oauth_plan or "Anthropic", 40)
    has_local = len(all_sessions) > 0 or total_prompts > 0 or len(quota_groups) > 0

    result = {
        "schemaVersion": 1,
        "id": "claude",
        "name": "Claude Code",
        "ready": True,
        "active": has_active_session,
        "activeStatus": active_status,
        "hasActiveSession": has_active_session,
        "hasLocalStats": has_local,
        "tierLabel": tier_label,
        "currentModel": clean_latest_model,
        "todayPrompts": today_prompts,
        "todaySessions": today_sessions,
        "todaySteps": today_steps,
        "todayTotalTokens": today_total_tokens,
        "todayTokensByModel": today_tokens_by_model,
        "recentDays": recent_days_data,
        "totalPrompts": total_prompts,
        "totalSessions": len(all_sessions),
        "totalSteps": total_steps,
        "totalTokens": total_tokens,
        "activeSessions": active_sessions,
        "recentSessions": all_sessions[:10],
        "toolUsage": tools_dict,
        "modelUsage": model_usage_dict,
        "modelList": model_list,
        "quotaGroups": quota_groups,
        "limits": limits,
        "recentWorkspaces": recent_workspaces,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "quotaUpdatedAt": quota_updated_at,
        "quotaUpdatedMs": quota_updated_ms,
        "lastFullRefreshMs": quota_updated_ms,
        "usageStatusText": sanitize_plain_text(raw_quota.get("usageStatusText") or f"{active_status} • {clean_latest_model}", 100),
        "authHelpText": sanitize_plain_text(raw_quota.get("authHelpText") or "", 220),
    }

    try:
        scan_cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_cache = scan_cache_path.with_suffix(".tmp")
        with open(tmp_cache, "w", encoding="utf-8") as f:
            json.dump(result, f)
        tmp_cache.replace(scan_cache_path)
    except Exception:
        pass

    return result


scan_claude = scan


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code Usage Scanner")
    parser.add_argument("path", nargs="?", default=None, help="Path to ~/.claude")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--force", action="store_true", help="Bypass cache and force refresh from Anthropic OAuth /usage")
    parser.add_argument("--kill", type=str, default=None, help="Kill the running session by session ID")
    parser.add_argument("--refresh-quota-bg", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--notify-low-quota", type=int, nargs="?", const=15, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    base_dir = expand_path(args.path) if args.path else default_base_dir()

    if args.refresh_quota_bg:
        bg_refresh_quota(base_dir)
        return

    if args.kill:
        success = kill_session(args.kill, base_dir)
        print(json.dumps({"success": success, "conversationId": args.kill}))
        return

    result = scan(base_dir, force=args.force, alert_threshold=args.notify_low_quota)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
