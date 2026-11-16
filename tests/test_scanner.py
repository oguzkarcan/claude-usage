#!/usr/bin/env python3
"""Smoke and unit tests for claude_usage_scanner."""

import datetime as dt
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from claude_usage_scanner import (
    check_and_send_quota_notifications,
    compute_bucket_forecast,
    format_claude_model_name,
    format_hours_duration,
    history_prompt_is_real,
    normalize_timestamp_seconds,
    parse_active_sessions,
    parse_transcripts,
    read_configured_model,
    recent_date_strings,
    sanitize_plain_text,
    scan,
    update_quota_snapshots,
)


class TestClaudeScanner(unittest.TestCase):
    def test_sanitize_plain_text(self):
        self.assertEqual(sanitize_plain_text("hello\x00 world\t"), "hello world")
        self.assertEqual(sanitize_plain_text(None), "")
        self.assertEqual(sanitize_plain_text("   spaced   out   "), "spaced out")

    def test_format_claude_model_name(self):
        self.assertEqual(format_claude_model_name("claude-opus-5-5"), "Claude Opus 5.5")
        self.assertEqual(format_claude_model_name("claude-fable-5-2"), "Claude Fable 5.2")
        self.assertEqual(format_claude_model_name("claude-sonnet-4-6-20250514"), "Claude Sonnet 4.6")
        self.assertEqual(format_claude_model_name("claude-3-5-sonnet-20241022"), "Claude 3.5 Sonnet")
        self.assertEqual(format_claude_model_name("opus[1m]"), "Claude Opus (1M)")
        self.assertEqual(format_claude_model_name("<synthetic>"), "")

    def test_read_configured_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            self.assertEqual(read_configured_model(pdir), "Claude Code")
            (pdir / "settings.json").write_text(json.dumps({"model": "opus"}))
            self.assertEqual(read_configured_model(pdir), "Claude Opus")

    def test_history_prompt_is_real(self):
        self.assertTrue(history_prompt_is_real("fix the build"))
        self.assertTrue(history_prompt_is_real("/init"))
        self.assertFalse(history_prompt_is_real("/clear"))
        self.assertFalse(history_prompt_is_real("/model opus"))

    def test_format_hours_duration(self):
        self.assertEqual(format_hours_duration(0.5), "30m")
        self.assertEqual(format_hours_duration(2.5), "2.5h")
        self.assertEqual(format_hours_duration(26.0), "1d 2h")
        self.assertEqual(format_hours_duration(47.7), "2d")
        self.assertEqual(format_hours_duration(24.0), "1d")
        self.assertEqual(format_hours_duration(0), "0m")

    def test_normalize_timestamp_seconds(self):
        self.assertAlmostEqual(normalize_timestamp_seconds(1725753600), 1725753600.0)
        self.assertAlmostEqual(normalize_timestamp_seconds(1725753600000), 1725753600.0)
        self.assertEqual(normalize_timestamp_seconds(None), 0.0)
        self.assertEqual(normalize_timestamp_seconds("not-a-number"), 0.0)

    def test_compute_bucket_forecast(self):
        now_dt = dt.datetime.now(dt.timezone.utc)
        reset_in_5h = (now_dt + dt.timedelta(hours=5)).isoformat().replace("+00:00", "Z")

        burn_txt, fc_txt, status = compute_bucket_forecast(90.0, 0.0, reset_in_5h)
        self.assertEqual(status, "stable")
        self.assertEqual(fc_txt, "Paced to reset")

        burn_txt, fc_txt, status = compute_bucket_forecast(80.0, 2.0, reset_in_5h)
        self.assertEqual(status, "safe")
        self.assertIn("On pace", fc_txt)
        self.assertEqual(burn_txt, "2.0%/h")

        burn_txt, fc_txt, status = compute_bucket_forecast(20.0, 2.0, reset_in_5h)
        self.assertEqual(status, "warning")
        self.assertIn("Tight pace", fc_txt)

        burn_txt, fc_txt, status = compute_bucket_forecast(20.0, 10.0, reset_in_5h)
        self.assertEqual(status, "critical")
        self.assertIn("Depletes in ~2.0h (before reset)", fc_txt)

    def test_update_quota_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cdir = Path(tmpdir)
            groups = [
                {
                    "name": "Claude Subscription (Pro)",
                    "buckets": [{"id": "session-5-hour", "remaining_fraction": 0.90}],
                }
            ]
            rates = update_quota_snapshots(cdir, groups)
            self.assertIn("session-5-hour", rates)
            self.assertEqual(rates["session-5-hour"], 0.0)
            self.assertTrue((cdir / "quota_snapshots.json").exists())

    def test_parse_active_sessions_pid_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sdir = Path(tmpdir) / "sessions"
            sdir.mkdir()
            (sdir / f"{os.getpid()}.json").write_text(json.dumps({
                "pid": os.getpid(),
                "sessionId": "sess-live",
                "cwd": "/tmp/ws",
                "status": "busy",
                "name": "Live session",
            }))
            (sdir / "99999999.json").write_text(json.dumps({
                "pid": 99999999,
                "sessionId": "sess-dead",
                "cwd": "/tmp/ws",
                "status": "busy",
            }))

            active = parse_active_sessions(sdir)
            self.assertIn("sess-live", active)
            self.assertTrue(active["sess-live"]["isWorking"])
            self.assertNotIn("sess-dead", active)

    def test_transcript_dedup_tools_tokens_and_caching(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdir = Path(tmpdir)
            proj = pdir / "projects" / "-tmp-demo"
            proj.mkdir(parents=True)
            tpath = proj / "sess-1.jsonl"
            today = dt.datetime.now().date().strftime("%Y-%m-%d")
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

            lines = [
                {"type": "custom-title", "customTitle": "Fix widget", "sessionId": "sess-1"},
                {
                    "type": "user",
                    "uuid": "u-1",
                    "sessionId": "sess-1",
                    "cwd": "/tmp/demo",
                    "timestamp": now_iso,
                    "message": {"role": "user", "content": "check tests"},
                },
                # Local command should be excluded once local-command-stdout arrives
                {
                    "type": "user",
                    "uuid": "u-cmd",
                    "sessionId": "sess-1",
                    "timestamp": now_iso,
                    "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
                },
                {
                    "type": "user",
                    "uuid": "u-cmd-out",
                    "parentUuid": "u-cmd",
                    "sessionId": "sess-1",
                    "timestamp": now_iso,
                    "message": {"role": "user", "content": "<local-command-stdout>Cleared</local-command-stdout>"},
                },
                # Streaming assistant placeholder then final snapshot for same msg id
                {
                    "type": "assistant",
                    "sessionId": "sess-1",
                    "requestId": "req-1",
                    "timestamp": now_iso,
                    "message": {
                        "id": "msg-1",
                        "model": "claude-opus-5-5",
                        "content": [{"type": "tool_use", "name": "Read"}],
                        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": "sess-1",
                    "requestId": "req-1",
                    "timestamp": now_iso,
                    "message": {
                        "id": "msg-1",
                        "model": "claude-opus-5-5",
                        "content": [{"type": "tool_use", "name": "Read"}, {"type": "tool_use", "name": "Bash"}],
                        "usage": {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
                    },
                },
            ]
            tpath.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

            cache_dir = pdir / "cache"
            dates = recent_date_strings()
            tools1, models1, list1, latest1, sessions1, d_prompts1, _, total_p1, _, total_tok1 = parse_transcripts(
                pdir / "projects", today, dates, cache_dir=cache_dir
            )
            self.assertEqual(total_p1, 1)
            self.assertEqual(d_prompts1[today], 1)
            self.assertEqual(tools1["Read"], 1)
            self.assertEqual(tools1["Bash"], 1)
            self.assertEqual(total_tok1, 180)
            self.assertEqual(latest1, "Claude Opus 5.5")
            self.assertEqual(sessions1["sess-1"]["firstPrompt"], "Fix widget")
            self.assertTrue((cache_dir / "transcript_stats_cache.json").exists())

            # Second pass reads from cache
            tools2, _, _, _, _, _, _, total_p2, _, total_tok2 = parse_transcripts(
                pdir / "projects", today, dates, cache_dir=cache_dir
            )
            self.assertEqual(total_p2, 1)
            self.assertEqual(total_tok2, 180)
            self.assertEqual(tools2["Bash"], 1)

    def test_scan_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            cache_dir = base_dir / "cache"
            cache_dir.mkdir(parents=True)
            mock_quota = {
                "ok": True,
                "tierLabel": "Pro",
                "groups": [
                    {
                        "name": "Claude Subscription (Pro)",
                        "buckets": [
                            {
                                "id": "session-5-hour",
                                "name": "Session (5-hour)",
                                "label": "5-Hour Session",
                                "window": "5h",
                                "remaining_fraction": 0.65,
                                "reset_time": "2026-11-15T06:50:00Z",
                            }
                        ],
                    }
                ],
                "usageStatusText": "",
                "authHelpText": "",
            }
            (cache_dir / "quota_usage_cache.json").write_text(json.dumps(mock_quota))

            data = scan(base_dir, force=False)
            self.assertEqual(data["schemaVersion"], 1)
            self.assertEqual(data["id"], "claude")
            self.assertEqual(data["tierLabel"], "Pro")
            self.assertIn("activeStatus", data)
            self.assertIn("todayPrompts", data)
            self.assertIn("todayTotalTokens", data)
            self.assertIsInstance(data["recentSessions"], list)
            self.assertTrue(len(data["limits"]) > 0)
            self.assertEqual(data["quotaGroups"][0]["buckets"][0]["remainingPercent"], 65)
            self.assertIn("burnRatePerHour", data["limits"][0])
            self.assertIn("forecastText", data["limits"][0])
            self.assertTrue(data["lastFullRefreshMs"] > 0)

    def test_quota_notifications_cooldown_and_consolidation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cdir = Path(tmpdir)
            low = [
                {
                    "name": "Claude Subscription (Pro)",
                    "buckets": [
                        {"id": "weekly-7-day", "label": "Weekly (7-Day)", "remainingPercent": 0},
                        {"id": "session-5-hour", "label": "5-Hour Session", "remainingPercent": 3},
                    ],
                }
            ]
            healthy = [
                {
                    "name": "Claude Subscription (Pro)",
                    "buckets": [
                        {"id": "weekly-7-day", "label": "Weekly (7-Day)", "remainingPercent": 100},
                        {"id": "session-5-hour", "label": "5-Hour Session", "remainingPercent": 100},
                    ],
                }
            ]

            with patch("subprocess.run") as mock_run:
                check_and_send_quota_notifications(cdir, low, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 1)
                cmd = mock_run.call_args[0][0]
                self.assertEqual(cmd[0], "omarchy-notification-send")
                self.assertIn("0%", cmd[7])
                self.assertIn("Weekly (7-Day) (0%)", cmd[8])
                self.assertIn("5-Hour Session (3%)", cmd[8])

                mock_run.reset_mock()
                check_and_send_quota_notifications(cdir, low, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 0)

                check_and_send_quota_notifications(cdir, healthy, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 0)

                check_and_send_quota_notifications(cdir, low, threshold_pct=15)
                self.assertEqual(mock_run.call_count, 1)

    def test_cache_permissions_under_umask_022(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir) / "claude"
            proj_dir = base_dir / "projects" / "-home-test-repo"
            proj_dir.mkdir(parents=True)
            cache_dir = base_dir / "cache"
            cache_dir.mkdir(parents=True)
            os.chmod(cache_dir, 0o755)

            (cache_dir / "quota_usage_cache.json").write_text(json.dumps({
                "ok": True,
                "tierLabel": "Pro",
                "groups": [{
                    "name": "Claude Subscription (Pro)",
                    "buckets": [{"id": "session-5-hour", "label": "5-Hour Session", "remaining_fraction": 0.8}],
                }],
            }))
            (proj_dir / "sess-perm.jsonl").write_text(json.dumps({
                "type": "assistant",
                "timestamp": "2026-11-16T07:00:00Z",
                "message": {
                    "id": "msg-perm-1",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "content": [{"type": "text", "text": "ok"}],
                },
            }) + "\n")
            (base_dir / "history.jsonl").write_text(json.dumps({
                "display": "secret prompt text",
                "timestamp": 1794812400000,
                "project": "/home/test/repo",
                "sessionId": "sess-perm",
            }) + "\n")

            old_umask = os.umask(0o022)
            try:
                scan(base_dir, force=True)
                check_and_send_quota_notifications(cache_dir, [], threshold_pct=15)
            finally:
                os.umask(old_umask)

            self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
            for fname in (
                "scanner_cache.json",
                "transcript_stats_cache.json",
                "quota_snapshots.json",
                "quota_usage_cache.json",
            ):
                fpath = cache_dir / fname
                self.assertTrue(fpath.exists(), f"Missing cache file {fname}")
                self.assertEqual(stat.S_IMODE(fpath.stat().st_mode), 0o600)

            # Also verify XDG_CACHE_HOME path when scanning default CLAUDE_CONFIG_DIR
            xdg_dir = Path(tmpdir) / "xdg_cache"
            xdg_dir.mkdir(mode=0o755)
            os.chmod(xdg_dir, 0o755)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(base_dir), "XDG_CACHE_HOME": str(xdg_dir)}):
                old_umask = os.umask(0o022)
                try:
                    scan(base_dir, force=True)
                finally:
                    os.umask(old_umask)
                xdg_plugin_cache = xdg_dir / "ozzy.claude-usage"
                self.assertEqual(stat.S_IMODE(xdg_plugin_cache.stat().st_mode), 0o700)
                for fname in ("scanner_cache.json", "transcript_stats_cache.json"):
                    fpath = xdg_plugin_cache / fname
                    self.assertTrue(fpath.exists(), f"Missing XDG cache file {fname}")
                    self.assertEqual(stat.S_IMODE(fpath.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
