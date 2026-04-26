"""
evolution_kit.tool_system — 组合式核心工具原语系统

7 种原语工具 + PermissionEnforcer + ToolRegistry，
灵感来自 claw-code 的 tools/src/lib.rs。

优化说明（2026-04-26）:
- 所有工具继承 BaseTool，统一的 validate/execute 契约
- PermissionEnforcer 支持白名单模式
- ToolRegistry 支持自动发现和注册
"""

import abc
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tools")


# ─── 工具接口 ──────────────────────────────────

class RiskLevel(Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolResult:
    def __init__(self, ok: bool, data: Any = None, error: str = "",
                 meta: Optional[dict] = None):
        self.ok = ok
        self.data = data
        self.error = error
        self.meta = meta or {}
        self.duration_ms = 0.0

    @classmethod
    def ok(cls, data: Any, **meta) -> "ToolResult":
        return cls(True, data=data, meta=meta)

    @classmethod
    def err(cls, error: str, **meta) -> "ToolResult":
        return cls(False, error=error, meta=meta)

    def __repr__(self) -> str:
        status = "ok" if self.ok else "err"
        return f"<ToolResult {status} {self.data or self.error}>"


class BaseTool(abc.ABC):
    """所有工具的基类"""
    name: str = ""
    description: str = ""
    risk: RiskLevel = RiskLevel.LOW
    requires_confirmation: bool = False

    @abc.abstractmethod
    def validate(self, **kwargs) -> Optional[str]:
        """验证参数，返回错误信息或 None"""
        ...

    @abc.abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """执行工具逻辑"""
        ...


# ─── 工具实现 ──────────────────────────────────

class FileReadTool(BaseTool):
    name = "file_read"
    description = "读取文件内容"
    risk = RiskLevel.LOW

    def __init__(self, allowed_prefix: str = "."):
        self.allowed_prefix = Path(allowed_prefix).resolve()

    def validate(self, path: str, **kwargs) -> Optional[str]:
        resolved = (self.allowed_prefix / path).resolve()
        if not str(resolved).startswith(str(self.allowed_prefix)):
            return f"路径越权: {path}"
        if not resolved.exists():
            return f"文件不存在: {path}"
        return None

    def execute(self, path: str, limit: int = 0, binary: bool = False,
                **kwargs) -> ToolResult:
        t0 = time.time()
        resolved = (self.allowed_prefix / path).resolve()
        mode = "rb" if binary else "r"
        try:
            with open(resolved, mode) as f:
                content = f.read()
            if limit > 0 and len(content) > limit:
                content = content[:limit] + f"\n... [截断: {len(content)} > {limit} chars]"
            elapsed = (time.time() - t0) * 1000
            size = len(content)
            return ToolResult.ok(content, bytes=size, duration_ms=elapsed)
        except Exception as e:
            return ToolResult.err(str(e))


class FileWriteTool(BaseTool):
    name = "file_write"
    description = "写入文件（自动备份原文件）"
    risk = RiskLevel.MEDIUM
    requires_confirmation = True

    def __init__(self, allowed_prefix: str = "."):
        self.allowed_prefix = Path(allowed_prefix).resolve()

    def validate(self, path: str, content: str = "", **kwargs) -> Optional[str]:
        resolved = (self.allowed_prefix / path).resolve()
        if not str(resolved).startswith(str(self.allowed_prefix)):
            return f"路径越权: {path}"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"无法创建目录: {e}"
        return None

    def execute(self, path: str, content: str = "", backup: bool = True,
                **kwargs) -> ToolResult:
        t0 = time.time()
        resolved = (self.allowed_prefix / path).resolve()
        if backup and resolved.exists():
            bak = resolved.with_suffix(resolved.suffix + ".bak")
            shutil.copy2(resolved, bak)
        resolved.write_text(content, encoding="utf-8")
        elapsed = (time.time() - t0) * 1000
        return ToolResult.ok(written=len(content), path=str(resolved), duration_ms=elapsed)


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "获取网页内容（只读，安全）"
    risk = RiskLevel.LOW

    def validate(self, url: str, **kwargs) -> Optional[str]:
        if not url.startswith(("http://", "https://")):
            return f"不支持的协议: {url}"
        return None

    def execute(self, url: str, timeout: int = 10, **kwargs) -> ToolResult:
        t0 = time.time()
        try:
            import httpx
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            elapsed = (time.time() - t0) * 1000
            return ToolResult.ok(r.text, status=r.status_code, duration_ms=elapsed)
        except ImportError:
            return ToolResult.err("httpx 未安装，请 pip install httpx")
        except Exception as e:
            return ToolResult.err(str(e))


class ShellExecTool(BaseTool):
    name = "shell_exec"
    description = "执行 shell 命令（参数列表模式，禁止 shell=True）"
    risk = RiskLevel.HIGH
    requires_confirmation = True

    def __init__(self, allowed_commands: Optional[list[str]] = None):
        self.allowed = allowed_commands

    def validate(self, args: list[str], **kwargs) -> Optional[str]:
        if not isinstance(args, list) or not args:
            return "args 必须是非空列表"
        if self.allowed and args[0] not in self.allowed:
            return f"命令不在白名单: {args[0]}"
        return None

    def execute(self, args: list[str], timeout: int = 30,
                capture: bool = True, **kwargs) -> ToolResult:
        t0 = time.time()
        try:
            r = subprocess.run(
                args, capture_output=capture, text=capture, timeout=timeout
            )
            elapsed = (time.time() - t0) * 1000
            return ToolResult.ok({
                "stdout": r.stdout,
                "stderr": r.stderr,
                "code": r.returncode,
            }, duration_ms=elapsed, code=r.returncode)
        except subprocess.TimeoutExpired:
            return ToolResult.err(f"超时 ({timeout}s)")
        except Exception as e:
            return ToolResult.err(str(e))


class MemoryQueryTool(BaseTool):
    name = "memory_query"
    description = "查询记忆系统（关键词/向量搜索）"
    risk = RiskLevel.SAFE

    def __init__(self, memory_dir: str = "memory"):
        self.memory_dir = Path(memory_dir).resolve()

    def validate(self, query: str, **kwargs) -> Optional[str]:
        if not query or not query.strip():
            return "查询不能为空"
        return None

    def execute(self, query: str, top_k: int = 5, **kwargs) -> ToolResult:
        t0 = time.time()
        if not self.memory_dir.exists():
            return ToolResult.err(f"记忆目录不存在: {self.memory_dir}")

        hits = []
        for f in sorted(self.memory_dir.glob("**/*.md")):
            try:
                text = f.read_text(encoding="utf-8")
                if query.lower() in text.lower():
                    # 简单相关性：查询出现次数
                    score = text.lower().count(query.lower())
                    hits.append((score, f.name, text[:500]))
            except Exception:
                continue

        hits.sort(key=lambda x: -x[0])
        hits = hits[:top_k]
        elapsed = (time.time() - t0) * 1000
        return ToolResult.ok({
            "hits": [{"file": f, "score": s, "preview": t} for s, f, t in hits],
            "total_files_scanned": len(list(self.memory_dir.glob("**/*.md"))),
        }, duration_ms=elapsed)


class DiffMergeTool(BaseTool):
    name = "diff_merge"
    description = "计算文件差异或合并变更"
    risk = RiskLevel.LOW

    def validate(self, old_text: str = "", new_text: str = "",
                 **kwargs) -> Optional[str]:
        if not old_text and not new_text:
            return "至少需要 old_text 或 new_text"
        return None

    def execute(self, old_text: str = "", new_text: str = "",
                context_lines: int = 3, **kwargs) -> ToolResult:
        t0 = time.time()
        old_lines = old_text.splitlines(True) if old_text else []
        new_lines = new_text.splitlines(True) if new_text else []

        import difflib
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile="a", tofile="b",
            n=context_lines,
        ))
        elapsed = (time.time() - t0) * 1000
        return ToolResult.ok({
            "diff_lines": len(diff),
            "diff": "".join(diff) if diff else "(identical)",
            "old_size": len(old_text),
            "new_size": len(new_text),
        }, duration_ms=elapsed)


class MetaPromptTool(BaseTool):
    name = "meta_prompt"
    description = "元提示模板引擎，生成结构化提示"
    risk = RiskLevel.SAFE

    TEMPLATES = {
        "decision": (
            "You need to make a decision about: {topic}\n"
            "Context: {context}\n"
            "Options: {options}\n"
            "Provide your reasoning and final decision."
        ),
        "review": (
            "Review the following work: {content}\n"
            "Criteria: {criteria}\n"
            "Identify issues, strengths, and suggestions."
        ),
        "reflect": (
            "Reflect on this experience: {experience}\n"
            "What went well? What could be improved? What should be remembered?"
        ),
    }

    def validate(self, template: str = "decision", **kwargs) -> Optional[str]:
        if template not in self.TEMPLATES:
            return f"未知模板: {template}，可用: {list(self.TEMPLATES.keys())}"
        return None

    def execute(self, template: str = "decision", **kwargs) -> ToolResult:
        t0 = time.time()
        tpl = self.TEMPLATES[template]
        try:
            filled = tpl.format(**kwargs)
        except KeyError as e:
            return ToolResult.err(f"缺少参数: {e}")
        elapsed = (time.time() - t0) * 1000
        return ToolResult.ok(filled, template=template, duration_ms=elapsed)


# ─── 权限增强器 ────────────────────────────────

class PermissionEnforcer:
    """工具权限管理，支持白名单模式（白名单=不会提示确认）。"""

    def __init__(self, allowlist: Optional[list[str]] = None):
        self.allowlist = set(allowlist or [])

    def requires_confirmation(self, tool: BaseTool, **kwargs) -> bool:
        if tool.risk == RiskLevel.SAFE:
            return False
        if tool.name in self.allowlist:
            return False
        return tool.requires_confirmation

    def check(self, tool: BaseTool, **kwargs) -> Optional[str]:
        """返回拒绝原因或 None（允许）"""
        # 白名单放行
        if tool.name in self.allowlist:
            return None
        if tool.risk == RiskLevel.SAFE:
            return None
        if tool.risk == RiskLevel.HIGH and tool.name not in self.allowlist:
            return f"[BLOCKED] {tool.name} 需要白名单授权"
        return None


# ─── 工具注册器 ────────────────────────────────

class ToolRegistry:
    """管理工具注册和发现。"""

    def __init__(self, enforcer: Optional[PermissionEnforcer] = None):
        self._tools: dict[str, BaseTool] = {}
        self.enforcer = enforcer or PermissionEnforcer()

    def register(self, tool: BaseTool) -> None:
        if not tool.name:
            raise ValueError(f"工具缺少 name: {type(tool).__name__}")
        self._tools[tool.name] = tool
        logger.info(f"注册工具: {tool.name} [{tool.risk.value}]")

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list(self, risk_filter: Optional[RiskLevel] = None) -> list[BaseTool]:
        if risk_filter:
            return [t for t in self._tools.values() if t.risk == risk_filter]
        return list(self._tools.values())

    def run(self, name: str, **kwargs) -> ToolResult:
        tool = self.get(name)
        if not tool:
            return ToolResult.err(f"未知工具: {name}")

        # 检查白名单
        blocked = self.enforcer.check(tool, **kwargs)
        if blocked:
            return ToolResult.err(blocked)

        # 验证参数
        err = tool.validate(**kwargs)
        if err:
            return ToolResult.err(err)

        # 确认（实际使用中由外部代理控制）
        # if self.enforcer.requires_confirmation(tool, **kwargs):
        #     print(f"[CONFIRM] {tool.name}({kwargs})? (y/n): ", end="")

        # 执行
        return tool.execute(**kwargs)

    @classmethod
    def auto_register(cls, prefix: str = ".", memory_dir: str = "memory",
                      allowlist: Optional[list[str]] = None) -> "ToolRegistry":
        """自动注册所有内置工具"""
        registry = cls(enforcer=PermissionEnforcer(allowlist))
        allowed = Path(prefix).resolve()

        registry.register(FileReadTool(allowed_prefix=str(allowed)))
        registry.register(FileWriteTool(allowed_prefix=str(allowed)))
        registry.register(WebFetchTool())
        registry.register(ShellExecTool(
            allowed_commands=["ls", "cat", "head", "tail", "wc", "echo", "pwd"]
        ))
        registry.register(MemoryQueryTool(memory_dir=memory_dir))
        registry.register(DiffMergeTool())
        registry.register(MetaPromptTool())
        return registry


def run_example():
    """演示工具系统用法"""
    registry = ToolRegistry.auto_register(prefix=".", memory_dir="memory",
                                          allowlist=["file_read"])

    print("=== 已注册工具 ===")
    for t in registry.list():
        print(f"  {t.name:20s} [{t.risk.value:6s}] {t.description}")

    print("\n=== 执行 file_read (测试) ===")
    result = registry.run("file_read", path="nonexistent.txt")
    print(f"  {result}")

    print("\n=== 执行 meta_prompt ===")
    result = registry.run("meta_prompt", template="reflect",
                          experience="今天完成了 TAOR 引擎设计。")
    print(f"  {result.data}")

    print("\n=== 执行 shell_exec (blocked 测试) ===")
    result = registry.run("shell_exec", args=["rm", "-rf", "/"])
    print(f"  {result}")

    return registry


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_example()
