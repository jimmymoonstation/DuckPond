#!/bin/bash
# Runs the unattended Claude Code health-check agent. Invoked by cron every 1-2 hours.
set -uo pipefail

LOG_DIR="/opt/job-hunt-partner/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/health_check.log"
PROMPT_FILE="/opt/job-hunt-partner/scripts/health_check_prompt.txt"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) starting health check ==="
  cd /opt/job-hunt-partner
  claude -p "$(cat "$PROMPT_FILE")" --permission-mode dontAsk --output-format text
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) done ==="
} >> "$LOG_FILE" 2>&1
