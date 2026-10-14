---
name: douyin-learning-note
description: 将抖音学习或面试讲解视频整理为 Obsidian 笔记。用户粘贴抖音链接或分享文案，并要求整理、总结、取标题、记笔记、生成问答或保存到 Obsidian 时使用。Windows 与 macOS 均可运行：本地转写（macOS 用 mlx-whisper，Windows/Linux 用 faster-whisper）、问答整理、Mermaid 思维导图、自动取标题、写库和临时文件清理。
---

# 抖音学习视频笔记

把一条公开抖音学习视频整理成 Obsidian 笔记。默认写入共享配置里的 vault；用户可以在调用时覆盖 vault 或子目录。

## 硬性规则

- 只用本 skill 的脚本抓取和转写，不要调用 `yt-dlp`，不要把视频或音频上传到在线 ASR/第三方解析服务。
- 转写只在本机完成，后端按平台自动选择：macOS 用 `mlx-whisper`（默认模型 `mlx-community/whisper-large-v3-mlx`），Windows/Linux 用 `faster-whisper`（默认模型 `large-v3`）。可以用配置项 `asr_backend` 显式指定 `mlx` 或 `faster`。
- 最终笔记只保留整理后的总结、问答和思维导图；不要把完整逐字稿写进笔记。
- 不要直接写 vault。先生成 `note.md`，再调用 `commit`，让脚本校验、查重、原子写入并清理临时目录。
- 写库成功后临时目录自动删除；失败时保留并返回 `session_dir`。
- 笔记默认使用简体中文，技术名词保留通用英文写法。
- 口述回答只基于视频内容；视频没讲到但标准答案需要的内容，可以写入单独的“补充标准答案”，并明确标记为非视频内容。

## 路径与配置

本 skill 目录是包含当前 `SKILL.md` 的目录。入口脚本按平台二选一，参数完全相同：

```bash
# macOS / Linux
"<SKILL_DIR>/scripts/run.sh" ...
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File "<SKILL_DIR>\scripts\run.ps1" ...
```

下文所有 `"<SKILL_DIR>/scripts/run.sh" ...` 示例，在 Windows 上就是把命令前缀换成上面那行 `powershell ... run.ps1`，其余参数（`start` / `wait` / `commit` / `doctor` 及 `--input`、`--session` 等）一字不差。

共享配置默认在：

```text
macOS / Linux: ~/.config/douyin-learning-note/config.json
Windows:       %APPDATA%\douyin-learning-note\config.json
```

关键配置（留空表示使用平台默认值）：

```json
{
  "vault_path": "",
  "note_subdir": "学习/视频笔记",
  "asr_backend": "auto",
  "model": "mlx-community/whisper-large-v3-mlx",
  "faster_model": "large-v3",
  "faster_device": "auto",
  "faster_compute_type": "auto",
  "browser_channel": "auto",
  "work_root": ""
}
```

平台默认值：

- `vault_path`：`<用户目录>/Desktop/Note`（Windows 即 `C:\Users\<你>\Desktop\Note`，macOS 即 `/Users/<你>/Desktop/Note`）
- `work_root`：Windows `%LOCALAPPDATA%\douyin-learning-note\sessions`；macOS `~/Library/Caches/douyin-learning-note/sessions`；Linux `~/.cache/douyin-learning-note/sessions`
- `asr_backend`：`auto` = macOS 用 `mlx`，Windows/Linux 用 `faster`
- `browser_channel`：`auto` = 自动探测系统浏览器；也可指定 `chrome` / `msedge` / `chromium`（自带内核，需先 `playwright install chromium`）或某个浏览器可执行文件的绝对路径

用户说“保存到某个目录”时，把对应路径作为 `--vault` 或 `--subdir` 传给 `start`。用户说“重新整理并覆盖”时传 `--force`。

## 工作流

1. 用户消息里通常包含分享文案和短链。原样传给 `start`：

```bash
"<SKILL_DIR>/scripts/run.sh" start --input "<用户原始消息>"
```

如果用户指定了目录，追加 `--vault "<vault>"` 或 `--subdir "<子目录>"`；如果用户要覆盖已有笔记，追加 `--force`。

2. 解析 `start` 输出的 JSON：

- `state == "duplicate"`：直接告诉用户已有笔记路径。只有用户明确要求覆盖时才加 `--force` 重跑。
- 否则记录 `session_id`，并告诉用户后台已开始转写。

3. 轮询状态。优先用 `wait`，超时后再调用，不要高频轮询：

```bash
"<SKILL_DIR>/scripts/run.sh" wait --session "<session_id>" --timeout 150 --interval 10
```

如果返回 `timed_out: true` 或 `state == "running"`，给用户一个简短进度，然后继续等待。macOS 上 `mlx-whisper` 的 `large-v3` 在 M1 8GB 上约按音频时长的 3 倍耗时，3–4 分钟的视频通常需要 10 分钟左右；Windows/Linux 走 `faster-whisper`，CPU（int8）通常更慢，有 NVIDIA GPU（`faster_device: "cuda"`）时快很多。首次运行还要算上下载模型的时间。

4. `state == "failed"`：把 `error.message` 和 `session_dir` 告诉用户，停止。不要写笔记。

5. `state == "ready"` 后，读取状态里的：

- `paths.metadata_json`
- `paths.transcript_segments_md`
- `paths.transcript_txt`

先阅读 [references/asr-glossary.md](references/asr-glossary.md)，结合视频标题、标签和上下文纠正明显的 ASR 同音词。无法确定的词保留并标“待核实”。

6. 按 [references/note-template.md](references/note-template.md) 生成笔记：

- 自动判断是面试题模式还是普通学习模式；无法判断时按普通学习模式。
- 面试题模式：每题包含“考察点、回答框架、口述回答、补充标准答案、追问/易错点”。
- 口述回答写成 300–600 字的自然段，不要只写几句概括。
- 普通学习模式：包含“核心要点、详细梳理、可行动项/启发”。
- 两种模式都必须有 Mermaid `mindmap` 和来源链接。
- 保留题目时间戳，不写完整逐字稿。
- tags 至少包含 `视频笔记`、`抖音`，面试题加 `面试题`，普通学习加 `学习`，再加 1–3 个内容主题标签。
- 标题由内容总结生成，面试题默认以 `面试题｜` 开头，普通学习默认以 `学习笔记｜` 开头；不要直接复用抖音点击诱饵标题。
- 把笔记写到 `paths.note_md` 指向的路径。

7. 写库：

```bash
"<SKILL_DIR>/scripts/run.sh" commit --session "<session_id>" --note "<paths.note_md>"
```

如果提交时用户临时改了目录，可以再传 `--vault` 或 `--subdir`。`commit` 会校验结构、检查重复、生成安全文件名、原子写入，并在成功后删除整个临时 session。

8. 返回最终 `note_path` 给用户。如果 `commit` 返回 `duplicate`，返回已有笔记路径。

## 环境与失败处理

- 抓取依赖系统浏览器，脚本会按平台自动探测：Windows 依次找 Chrome（`Program Files` / `Program Files (x86)` / `%LOCALAPPDATA%`）和 Edge；macOS 找 Chrome / Chrome Canary / Edge / Chromium；Linux 找 `google-chrome` / `microsoft-edge` / `chromium`。一个都没有时，把 `browser_channel` 设为 `chromium` 并执行 `uv run --with playwright playwright install chromium`。
- 转写依赖 `ffmpeg` 和 `uv`。没装 `uv` 时先装：Windows `winget install --id astral-sh.uv -e`（或 `python -m pip install --user uv`），macOS/Linux `curl -LsSf https://astral.sh/uv/install.sh | sh`。
- worker 的依赖由脚本按平台临时拉起（macOS：`mlx-whisper`；Windows/Linux：`faster-whisper`），首次运行会从镜像下载模型，之后走缓存。
- 网络必须能访问 `pypi.tuna.tsinghua.edu.cn` 和 `hf-mirror.com`；脚本默认已经设置这两个镜像。
- 抖音必须识别为大陆出口。若 worker 报 `GeoBlockedError`，让用户关闭 VPN 或配置大陆代理，然后重新调用。
- 如果需要常驻大陆代理，在共享配置里设置 `proxy_url`，例如 `http://127.0.0.1:7890`；Codex、Qoder 和 DSH 会读取同一份配置。
- 只支持公开可访问的视频。登录、私密或已删除视频不要绕过权限，直接报告失败。
- 不要清理 session 目录以外的文件。模型缓存保留，不删除。

## 快速自检

需要确认环境时运行（Windows 上把前缀换成 `run.ps1`）：

```bash
"<SKILL_DIR>/scripts/run.sh" doctor
```

`doctor` 会报告当前平台、`asr_backend`、`uv`、`ffmpeg`、探测到的浏览器（`browser.path` / `browser.channel`）、vault 是否存在、模型缓存目录与镜像连通性。`warnings` 非空时先按提示补齐环境再重试。
