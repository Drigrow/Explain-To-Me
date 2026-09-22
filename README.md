# etm — Explain To Me

一个小型 macOS Terminal 工具，通过 OpenRouter 向一个或多个模型提问。它使用 [`rich`](https://github.com/Textualize/rich) 渲染 Markdown；API key 保存在 macOS Keychain，不写入配置文件。

## 安装

需要 Python 3.10+。此机器的 `python3` 是系统自带 3.9，请使用已安装的 Python 3.11：

```zsh
python3.11 -m pip install --user .
```

若安装后找不到 `etm`，将 Python user bin 加入 `PATH`（macOS 常见路径是 `~/Library/Python/3.x/bin`），或使用：

```zsh
python3.11 -m pip install --user --editable .
```

第一次运行时会隐藏输入 API key，并保存到 macOS Keychain；也可先运行 `etm config set-key`。已有的 `OPENROUTER_API_KEY` 或当前目录 `.env` 中的同名键会优先使用，且不会重复保存。

更新已安装版本：

```zsh
python3.11 -m pip install --user --upgrade .
```

## 使用

```zsh
etm what is tam sam som
etm gpt what is swot
etm gpt opus ds what is bmc
```

- 没有模型前缀时，使用默认 `gemini`（`~google/gemini-flash-latest`）。
- 单个模型前缀只显示它的回答。
- 两个以上模型并行回答，再用默认 `gemini` 生成“共同点 / 不同点 / 结论”综合。
- 所有回答都是流式请求。交互式 Terminal 中会以单一、固定高度的 Rich `Live` 面板更新各模型的最新内容，工作线程不会直接写终端，因此并发 token 不会交错、重复或撕裂；完成后才一次性渲染完整结果。终端够宽时模型会分栏显示，不够宽则自动纵向排列。
- 回答和综合结论支持 Markdown 渲染，包括标题、列表、表格、引用和代码块。`$...$`、`$$...$$`、`\\(...\\)` 与 `\\[...\\]` 中的 LaTeX 数学式会转换为终端可读的 Unicode 数学符号；无法转换时保留原始公式。重定向到文件/管道时仍输出可读的纯文本结果。

预置别名：

| 别名 | OpenRouter model ID |
| --- | --- |
| `gemini` | `~google/gemini-flash-latest` |
| `gpt` | `~openai/gpt-sol-latest` |
| `ds` | `~deepseek/deepseek-flash-latest` |
| `opus` | `~anthropic/claude-opus-latest` |
| `sonnet` | `~anthropic/claude-sonnet-latest` |

完整 model ID 也可以用于配置值；在 zsh 中请用单引号包住开头的 `~`，例如 `etm --synth '~anthropic/claude-sonnet-latest' gpt opus 问题`。

## 配置

```zsh
etm config show
etm config set-key
etm config delete-key
etm config set-model fast '~google/gemini-flash-latest'
etm config set-model gpt '~openai/gpt-sol-latest'
etm config set-default fast
etm config set-synth sonnet
etm config set-base-url https://openrouter.ai/api/v1
etm config delete-model fast
```

配置文件为 `~/.config/etm/config.json`（权限为仅当前用户可读写）；其中不含 API key。彻底移除配置与 Keychain 密钥：

```zsh
etm config uninstall --yes
```

## 开发与发布

本仓库不会跟踪 API key、`.env`、虚拟环境或构建产物。安装开发版本：

```zsh
python3.11 -m pip install --user --editable .
```

采用 [MIT License](LICENSE)。发布到 GitHub 后，可按需将实际仓库地址添加到 `pyproject.toml` 的 `[project.urls]`。
