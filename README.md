# Claude Usage for Omarchy

Claude Code active session monitor, prompt & token metrics, tool telemetry, interactive session launcher/resumer, and live OAuth quota tracking (`/usage`) in the Omarchy top bar.

Adapted from [jesseburlamaque/antigravity-usage](https://github.com/jesseburlamaque/antigravity-usage) for Claude Code (`~/.claude`).

![Claude Usage Preview](preview.png?v=2)

## Features

### 1. Status Bar Icon & Live Badge
- **Themed Vector Icon**: Claude starburst SVG dynamically colorized to match your active Omarchy theme foreground color via `MultiEffect`.
- **State Pulse Indicator**:
  - 🟢 **Green (Pulsing)**: Claude Code session is actively working (`status: "busy"`).
  - 🔵 **Blue**: Session is open and waiting for user input (`Waiting`).
  - ⚪ **Transparent**: Idle (no active sessions).
- **Configurable Bar Badge Mode**:
  - `active` (Default): Number of active Claude Code sessions (auto-hides when idle).
  - `prompts`: Total typed prompts executed today.
  - `off`: Icon only.
- **Detailed Tooltip**: Hovering shows active session status, today's prompts, today's token count, and active model.

### 2. Interactive Session Management
- **Quick Terminal Resume**: Click any session card or press `1`–`5` to immediately resume that session in its workspace directory (`claude --resume <id>`).
- **Terminal Emulator Override**: Supports `foot`, `ghostty`, `kitty`, `alacritty`, custom commands, or system default `xdg-terminal-exec`.
- **New Session Launcher**: Click `+` or press `n` to launch a fresh `claude` session.
- **Process Termination**: Hover over any active session and click `` to send `SIGTERM` to its verified PID.
- **Recent Sessions List**: Shows custom session titles, workspace tags, step counts, relative timestamps, and expandable view (3–10 default limit).

### 3. Model & Token Breakdown with Timeframe Toggle
- **Timeframe Switcher**: Toggle between **Today**, **Last 7 Days**, and **All time**.
- **Model Family Colors**: Automatically color-coded across Opus, Sonnet, Haiku, and Fable.
- **Token & Prompt Telemetry**: Tracks deduplicated typed prompts, steps, and total tokens (`input + output + cache_read + cache_creation`).

### 4. Live Quota Limits, Burn Rate & Desktop Alerts
- **Live Anthropic OAuth Usage**: Fetches 5-Hour Session, Weekly (7-Day), and model-specific limits (using Omarchy's shared `omarchy-agent-usage-claude` cache when present, with a direct `~/.claude/.credentials.json` OAuth probe fallback).
- **Burn Rate Velocity & Forecasting**: Tracks hourly consumption (`Burn: X%/h`) and predicts remaining quota at reset.
- **Dual Reset Time Display**: Shows both countdown (`Resets in 2h 15m`) and local wall-clock time.
- **Configurable Low Quota Alerts**: Optional desktop alerts via `omarchy-notification-send` with concurrency locking and anti-spam state tracking.

## Requirements

- Python 3 (standard library only)
- Claude Code (`claude` CLI) with local state in `~/.claude`
- Omarchy Shell / Quickshell

## Installation

```sh
omarchy plugin add https://github.com/oguzkarcan/claude-usage.git --enable
omarchy restart shell
```

### Local Install (from this directory)

```sh
ln -sfn "$(pwd)" ~/.config/omarchy/plugins/ozzy.claude-usage
omarchy-shell shell rescanPlugins
omarchy plugin enable ozzy.claude-usage --section centerEnd
```

### Update / Removal

```sh
omarchy plugin update ozzy.claude-usage --yes
omarchy plugin remove ozzy.claude-usage
```

### Configuration & Shortcuts

| Shortcut | Action |
|---|---|
| `1`–`5` | Resume recent session in terminal (`claude --resume <id>`) |
| `n` | Launch a new `claude` session |
| `r` | Force refresh telemetry and quota limits |
| `s` | Toggle Settings / save settings |
| `j` / `k` | Scroll popup content |
| `q` / `Esc` | Close popup |

### IPC Commands

```sh
omarchy-shell shell toggle ozzy.claude-usage
omarchy-shell shell summon ozzy.claude-usage
omarchy-shell shell hide ozzy.claude-usage
omarchy-shell ozzy.claude-usage refresh
omarchy-shell ozzy.claude-usage settings
```

## Development & Testing

```sh
python3 -m unittest discover -s tests
```

## License

MIT © Ozzy & Jesse Burlamaque (upstream `antigravity-usage`)
