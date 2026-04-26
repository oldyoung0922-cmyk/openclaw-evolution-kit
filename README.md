# openclaw-evolution-kit

Moss 智能进化套件 —— 让 OpenClaw 拥有自我进化的能力。

## 能力总览

| 模块 | 说明 | 文件 |
|------|------|------|
| **TAOR 循环引擎** | Think-Act-Observe-Repeat 自驱动闭环 | `plugins/evolution_kit/taor_engine.py` |
| **工具系统** | 7 种组合式核心工具原语 | `plugins/evolution_kit/tool_system.py` |
| **夜间三省** | 每日 22:00–02:00 自动回顾反思 | `scripts/nightly_check.py` |
| **记忆蓝图** | 三层记忆架构设计（即时/短期/长期） | `docs/memory_blueprint.md` |
| **协调框架** | 多智能体间 DAG 协调与质量门禁 | `docs/coordinator_framework.md` |

## 前置条件

- OpenClaw **2026.4.22+**
- Python 3.9+
- Git
- macOS / Linux

## 快速安装

```bash
# 克隆套件到本地
git clone git@github.com:oldyoung0922-cmyk/openclaw-evolution-kit.git ~/.openclaw/plugins/evolution-kit

# 或使用安装向导（推荐）
python3 experiments/openclaw_evolution_installer.py
```

## 目录结构

```
openclaw-evolution-kit/
├── manifest.json            # 插件声明（安装脚本读取）
├── default_config.json      # 默认配置（合并到 openclaw.json）
├── requirements.txt         # Python 依赖
├── install.sh               # 安装后钩子
├── .gitignore
├── plugins/
│   └── evolution_kit/       # 核心进化插件
│       ├── __init__.py
│       ├── taor_engine.py   # TAOR 循环引擎
│       └── tool_system.py   # 工具原语系统
├── scripts/
│   └── nightly_check.py     # 夜间三省检查脚本
├── rules/
│   ├── nightly_check.md     # 夜间三省规则
│   └── taor_design.md       # TAOR 设计参考
└── docs/
    ├── coordinator_framework.md
    └── memory_blueprint.md
```
