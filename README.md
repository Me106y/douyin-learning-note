# douyin-learning-note

把一条公开的抖音学习 / 面试讲解视频整理成 Obsidian 笔记的 agent skill。

- 本地转写，不上传任何音视频到第三方 ASR 服务
- 自动取标题、查重、原子写库、清理临时文件
- 面试题模式（考察点 / 回答框架 / 口述回答 / 补充标准答案 / 追问）与普通学习模式
- 输出 Mermaid `mindmap` 思维导图
- **跨平台**：macOS 用 `mlx-whisper`，Windows / Linux 用 `faster-whisper`

目录结构遵循通用的 skill 约定（`SKILL.md` + `references/` + `scripts/`），可用于 DSH、Codex、Claude Code 等支持 `SKILL.md` 的 agent。

## 安装

把本仓库整个目录放进 agent 的技能目录，目录名保持 `douyin-learning-note`：

| Agent | 技能目录 |
| --- | --- |
| DSH | `~/.dsh/skills/douyin-learning-note` |
| Codex | `~/.codex/skills/douyin-learning-note` |

## 前置依赖

- [`uv`](https://docs.astral.sh/uv/)：worker 依赖由脚本临时拉起
- `ffmpeg`：抽取音频
- 系统浏览器：Chrome 或 Edge（脚本会自动探测）

## 用法

macOS / Linux：

```bash
"<SKILL_DIR>/scripts/run.sh" start --input "<抖音分享文案 + 链接>"
"<SKILL_DIR>/scripts/run.sh" wait --session "<session_id>" --timeout 150 --interval 10
"<SKILL_DIR>/scripts/run.sh" commit --session "<session_id>" --note "<note.md>"
```

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\run.ps1" doctor
```

环境自检（Windows 用 `run.ps1`）：

```bash
"<SKILL_DIR>/scripts/run.sh" doctor
```

## 配置

| 平台 | 配置路径 |
| --- | --- |
| Windows | `%APPDATA%\douyin-learning-note\config.json` |
| macOS / Linux | `~/.config/douyin-learning-note/config.json` |

默认 vault 为 `<用户目录>/Desktop/Note`，子目录 `学习/视频笔记`；可用 `--vault` / `--subdir` 覆盖。完整配置项说明见 [`SKILL.md`](SKILL.md) 和 [`config.example.json`](config.example.json)。
