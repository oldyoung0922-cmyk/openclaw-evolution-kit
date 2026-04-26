#!/usr/bin/env bash
# evolution-kit 安装后钩子
set -e

echo "  evolution-kit 安装后钩子: 开始"

# 确认进化工作室
EVO_DIR="$HOME/.openclaw/evolution_workspace/ai_evolution_chamber"
mkdir -p "$EVO_DIR/rules" "$EVO_DIR/scripts" "$EVO_DIR/plugins"

# 复制规则和脚本
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp -n "$SCRIPT_DIR/rules/"*.md "$EVO_DIR/rules/" 2>/dev/null || true
cp -n "$SCRIPT_DIR/scripts/"*.py "$EVO_DIR/scripts/" 2>/dev/null || true

echo "  evolution-kit 安装后钩子: 完成"
