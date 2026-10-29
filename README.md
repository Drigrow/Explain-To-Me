# etm — Explain To Me

<p align="center"><strong>Ask several AI models from your terminal. Compare their answers. Keep the useful context.</strong></p>

<p align="center">
  <a href="https://github.com/Drigrow/Explain-To-Me/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-0b7f5b.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB.svg" alt="Python 3.10 or later">
  <img src="https://img.shields.io/badge/models-via%20OpenRouter-7B61FF.svg" alt="OpenRouter">
</p>

`etm` is a small macOS terminal assistant for asking one or more OpenRouter models at once. It streams answers live, renders Markdown and readable LaTeX, compares multiple perspectives, and lets you continue the latest conversation without losing context.

```zsh
etm gpt opus ds what is a business model canvas
```

## Why etm?

| Instead of… | etm gives you… |
| --- | --- |
| Opening several model tabs | Parallel answers from the models you choose |
| Manually comparing long responses | A final synthesis of agreement, differences, and uncertainty |
| Losing a useful thread | `etm continue` with the previous model-specific context |
| Plain, interleaved terminal output | A stable live Markdown interface that does not tear or scroll repeatedly |

## Quick start

Requires Python 3.10 or later.

```zsh
git clone git@github.com:Drigrow/Explain-To-Me.git
cd Explain-To-Me
python3.11 -m pip install --user .

# Your key is entered without echo and stored in the macOS Keychain on first use.
etm what are TAM, SAM, and SOM?
```

`etm` first checks `OPENROUTER_API_KEY` (including a local `.env` file), then the macOS Keychain. To enter or replace a key explicitly:

```zsh
etm config set-key
```

## Everyday usage

```zsh
# Default model: Gemini Flash
etm what is TAM SAM SOM

# Pick one model
etm gpt what is SWOT

# Compare models in parallel, then synthesize the answers
etm gpt opus ds what is BMC

# Ask for a more thorough answer
etm detailed gpt what is a business model canvas

# Force the answer language, independent of the question language
etm zh gpt opus what is SWOT

# Continue the latest conversation with its saved context
etm continue give me a concrete example
```

## Features

| Feature | What it does |
| --- | --- |
| **Multi-model comparison** | Runs selected models concurrently and synthesizes multiple answers automatically. |
| **Live streaming UI** | Uses a single Rich live renderer, so concurrent output stays readable. |
| **Markdown and LaTeX** | Renders headings, lists, tables, code blocks, and common LaTeX math as terminal-friendly Unicode. |
| **Short or detailed answers** | Default answers are concise. Add `detailed` or `--detailed` for richer explanations. |
| **Response language control** | Prefix a request with `en`, `zh`, `ja`, `ko`, `es`, `fr`, or `de`; use `--lang CODE` when preferred. |
| **Interface language control** | Set the CLI UI independently with `etm config set-language CODE`. |
| **Conversation continuation** | `etm continue` reuses the models, answer settings, and recent context from the last conversation. |
| **Secure key storage** | API keys live in macOS Keychain rather than the project configuration file. |
| **Local file analysis** | Detects exact file references, asks permission for each file, then sends approved content with your question. |

## Analyze local files

Mention an absolute path or an exact filename in your question:

```zsh
etm /Users/me/Documents/report.pdf summarize the main findings
etm zh gpt opus compare revenue.csv costs.csv
etm "/Users/me/Documents/my report.txt" identify inconsistencies
etm screenshot.png explain this error
etm continue /Users/me/Documents/report.pdf check the source for an example
```

For a bare filename, etm searches the current directory and its subdirectories for **exact** matches. If several files have the same name, it lists every full path and asks about each one. Hidden directories, common build/dependency folders, and symlink directories are skipped. Use an absolute path to target one file directly. Quote paths with spaces; use `./name` to disambiguate filenames that look like model or language keywords.

Before reading file content or making a model request, etm shows the resolved paths, sizes, destination API URL, and selected models. Answer `y` to approve each file (the default is no). Rejected files are excluded; rejecting all files cancels the request. File attachments require an interactive terminal. Approved content is included directly in the request, without a separate public upload URL.

| Format | How it is sent |
| --- | --- |
| UTF-8 text, Markdown, code, CSV/TSV, JSON, YAML, logs, subtitles, etc. | Text content with a filename label; no extra parser dependency |
| PDF | Base64 file input via OpenRouter's file-parser plugin |
| PNG, JPEG, WebP, GIF | Base64 image input; requires a vision-capable model/provider |

PDF parsing defaults to `cloudflare-ai`. To use a model's native PDF support or OCR for scanned documents:

```zsh
etm --pdf-engine native report.pdf analyze the diagrams
etm --pdf-engine mistral-ocr scan.pdf extract the key facts
```

Native PDF mode requires a compatible model. OCR may incur separate OpenRouter parsing charges; model inference and context limits still apply. Image and PDF support can also vary when using a custom base URL. See the official [PDF input](https://openrouter.ai/docs/guides/overview/multimodal/pdfs) and [image input](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding) documentation.

Local limits: **10 files**, **8 MiB per file**, **20 MiB total**, and **200,000 bytes per text file**. Oversized files are rejected rather than silently truncated. Office documents, archives, audio, and video are not supported in this version; export documents to PDF/text first.

Each selected model receives the approved attachments; the synthesis model receives their analyses. File bytes, base64, and extracted text are not separately saved in conversation state. `continue` retains the previous answers and filenames, so reattach a source file when a follow-up requires direct inspection. Model answers themselves may quote source content and remain in the usual conversation history.

## Included model aliases

| Alias | Model ID |
| --- | --- |
| `gemini` | `~google/gemini-flash-latest` |
| `gpt` | `~openai/gpt-sol-latest` |
| `ds` | `~deepseek/deepseek-flash-latest` |
| `opus` | `~anthropic/claude-opus-latest` |
| `sonnet` | `~anthropic/claude-sonnet-latest` |

Add, replace, or remove aliases at any time:

```zsh
etm config set-model fast '~google/gemini-flash-latest'
etm config set-default fast
etm config set-synth sonnet
etm config set-default gemini
etm config delete-model fast
```

> Quote model IDs beginning with `~` in zsh so the shell does not treat them as a path.

## Configuration

```zsh
etm config show
etm config set-base-url https://openrouter.ai/api/v1
etm config set-language en
etm config set-key
etm config delete-key
```

Configuration is stored at `~/.config/etm/config.json` with owner-only permissions. The latest conversation is stored separately at `~/.config/etm/state.json`; it contains no API key and retains a bounded amount of recent context per model.

To remove the configuration, conversation state, and Keychain item:

```zsh
etm config uninstall --yes
```

## Development

Install an editable development version:

```zsh
python3.11 -m pip install --user --editable .
```

The repository intentionally ignores API keys, `.env` files, virtual environments, caches, and build artifacts.

Run the offline file-discovery, permission, and request-format tests:

```zsh
PYTHONPATH=src python3.11 -m unittest discover -s tests -v
```

## License

Released under the [MIT License](LICENSE).
