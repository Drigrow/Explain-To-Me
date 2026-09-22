from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import queue
import re
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from unicodeitplus import replace as latex_to_unicode


APP_DIR = Path.home() / ".config" / "etm"
CONFIG_PATH = APP_DIR / "config.json"
KEYCHAIN_SERVICE = "etm-openrouter"
KEYCHAIN_ACCOUNT = "openrouter-api-key"
DEFAULTS: dict[str, Any] = {
    "base_url": "https://openrouter.ai/api/v1",
    "default_model": "gemini",
    "synthesis_model": "gemini",
    "models": {
        "gemini": "~google/gemini-flash-latest",
        "gpt": "~openai/gpt-sol-latest",
        "ds": "~deepseek/deepseek-flash-latest",
        "opus": "~anthropic/claude-opus-latest",
        "sonnet": "~anthropic/claude-sonnet-latest",
    },
}

SHORT_ANSWER_INSTRUCTION = (
    "Answer concisely and directly. Lead with the answer, then give only the most useful "
    "supporting points. Prefer 3–6 short bullets or a compact explanation; avoid unnecessary "
    "background, repetition, and long disclaimers. Reply in the user's language."
)
DETAILED_ANSWER_INSTRUCTION = (
    "Give a thorough, well-structured answer. Lead with the conclusion, then explain key "
    "concepts, reasoning, assumptions, examples, trade-offs, and important caveats where useful. "
    "Use Markdown headings and lists when they improve readability. Reply in the user's language."
)


class EtmError(Exception):
    pass


def load_config() -> dict[str, Any]:
    """Load config and merge it with defaults so new settings stay compatible."""
    config = json.loads(json.dumps(DEFAULTS))
    if not CONFIG_PATH.exists():
        return config
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtmError(f"无法读取配置 {CONFIG_PATH}: {exc}") from exc
    for key in ("base_url", "default_model", "synthesis_model"):
        if isinstance(saved.get(key), str):
            config[key] = saved[key]
    if isinstance(saved.get("models"), dict):
        config["models"].update(
            {str(k): str(v) for k, v in saved["models"].items() if isinstance(v, str)}
        )
    return config


def save_config(config: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


def env_key() -> str | None:
    """Read process env first, then a local .env without requiring python-dotenv."""
    for name in ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"):
        if os.environ.get(name):
            return os.environ[name].strip()
    dotenv = Path.cwd() / ".env"
    if dotenv.is_file():
        try:
            for raw in dotenv.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() in {"OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"}:
                    return value.strip().strip('"').strip("'")
        except OSError:
            pass
    return None


def keychain_key() -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            text=True, capture_output=True, check=False,
        )
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def save_keychain_key(key: str) -> None:
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w", key],
            text=True, capture_output=True, check=False,
        )
    except FileNotFoundError as exc:
        raise EtmError("此系统没有 macOS Keychain（security 命令）。") from exc
    if result.returncode != 0:
        raise EtmError(f"无法保存到 Keychain: {result.stderr.strip()}")


def delete_keychain_key() -> bool:
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            text=True, capture_output=True, check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def get_api_key(interactive: bool = True) -> str:
    key = env_key() or keychain_key()
    if key:
        return key
    if not interactive or not sys.stdin.isatty():
        raise EtmError("未找到 OPENROUTER_API_KEY。请先执行 `etm config set-key`，或设置环境变量。")
    print("未找到 OpenRouter API key。它会安全保存到 macOS Keychain。", file=sys.stderr)
    key = getpass.getpass("OpenRouter API key: ").strip()
    if not key:
        raise EtmError("没有输入 API key。")
    save_keychain_key(key)
    print("已保存到 macOS Keychain。", file=sys.stderr)
    return key


def resolve_model(value: str, config: dict[str, Any]) -> str:
    return config["models"].get(value, value)


def stream_openrouter(
    question: str, model: str, config: dict[str, Any], key: str, timeout: int, detailed: bool = False,
):
    """Yield content deltas from OpenRouter's OpenAI-compatible SSE endpoint."""
    url = config["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": DETAILED_ANSWER_INSTRUCTION if detailed else SHORT_ANSWER_INSTRUCTION},
            {"role": "user", "content": question},
        ],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "etm-openrouter/0.2.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    payload = json.loads(data)
                    delta = payload["choices"][0].get("delta", {}).get("content", "")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                if isinstance(delta, str) and delta:
                    yield delta
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise EtmError(f"OpenRouter 返回 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise EtmError(f"无法连接 OpenRouter: {exc.reason}") from exc
CONSOLE = Console()

# Keep substitutions deliberately limited to math delimiters.  The remaining
# Markdown must pass through untouched so headings, code fences, and tables
# retain their intended formatting.
DISPLAY_DOLLAR_RE = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL)
DISPLAY_BRACKET_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
INLINE_DOLLAR_RE = re.compile(r"(?<!\\)\$(?!\$)([^\n$]+?)(?<!\\)\$(?!\$)")
INLINE_PAREN_RE = re.compile(r"\\\((.+?)\\\)")
FENCED_CODE_RE = re.compile(r"(```.*?```)", re.DOTALL)
INLINE_CODE_RE = re.compile(r"(`[^`\n]*`)")
FRACTION_RE = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")


def normalise_fractions(markup: str) -> str:
    """unicodeitplus handles symbols and scripts; simplify common TeX fractions."""
    for _ in range(8):  # also resolves nested fractions from the inside out
        updated = FRACTION_RE.sub(lambda m: f"({m.group(1)})/({m.group(2)})", markup)
        if updated == markup:
            return markup
        markup = updated
    return markup


def latex_to_terminal(markup: str) -> str:
    """Convert `$...$`, `$$...$$`, `\\(...\\)` and `\\[...\\]` to Unicode math.

    A terminal cannot typeset TeX glyphs like a browser, but unicodeitplus provides
    a readable Unicode approximation. On an unsupported expression we
    retain the original formula instead of silently losing information.
    """
    def convert(match: re.Match[str], block: bool = False) -> str:
        original = match.group(0)
        try:
            converted = latex_to_unicode(normalise_fractions(match.group(1))).strip()
        except Exception:
            return original
        if not converted or "\\" in converted:
            return original
        return f"\n\n{converted}\n\n" if block else converted

    def transform_text(text: str) -> str:
        text = DISPLAY_DOLLAR_RE.sub(lambda m: convert(m, block=True), text)
        text = DISPLAY_BRACKET_RE.sub(lambda m: convert(m, block=True), text)
        text = INLINE_PAREN_RE.sub(convert, text)
        return INLINE_DOLLAR_RE.sub(
            lambda m: convert(m) if re.search(r"\\[A-Za-z]+|[\^_{}]|[=<>+*/]", m.group(1)) else m.group(0),
            text,
        )

    # Formulas in fenced/inline code are source code, not prose: never alter them.
    fenced_parts = FENCED_CODE_RE.split(markup)
    for index in range(0, len(fenced_parts), 2):
        inline_parts = INLINE_CODE_RE.split(fenced_parts[index])
        inline_parts[::2] = [transform_text(part) for part in inline_parts[::2]]
        fenced_parts[index] = "".join(inline_parts)
    return "".join(fenced_parts)


def markdown_renderable(content: str) -> Markdown:
    return Markdown(latex_to_terminal(clean_terminal_text(content)) or "_没有返回内容_")


def print_section(title: str, content: str) -> None:
    """Render completed model output as Markdown, including tables and code blocks."""
    CONSOLE.rule(title)
    CONSOLE.print(markdown_renderable(content))


ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\a]*(?:\a|\x1b\\))")


def clean_terminal_text(text: str) -> str:
    """A model must not be able to inject terminal control sequences into the dashboard."""
    return ANSI_ESCAPE.sub("", text).replace("\r", "")


class LiveDashboard:
    """One-writer terminal renderer for concurrent model streams.

    Workers only publish events; the main thread owns stdout, so no streams can
    interleave or leave a half-drawn terminal frame.
    """

    def __init__(self, labels: list[str]) -> None:
        self.enabled = CONSOLE.is_terminal
        self.labels = labels
        self.buffers = {label: "" for label in labels}
        self.status = {label: "等待中" for label in labels}
        # Live can only redraw output which still fits above the cursor. Rendering
        # an ever-growing full response causes terminals to scroll each frame.
        # A constant-height tail preview keeps every frame safely replaceable.
        self.panel_height = 14
        self.live: Live | None = None
        if self.enabled:
            self.live = Live(
                self._renderable(), console=CONSOLE, refresh_per_second=12,
                transient=True, vertical_overflow="ellipsis",
            )
            self.live.start(refresh=True)

    def update(self, label: str, delta: str | None = None, status: str | None = None) -> None:
        if delta:
            self.buffers[label] += clean_terminal_text(delta)
        if status:
            self.status[label] = status

    def render(self, force: bool = False) -> None:
        if not self.live:
            return
        self.live.update(self._renderable(), refresh=force)

    def _renderable(self) -> Group:
        panels = [
            Panel(
                # Render the current tail on every refresh. Markdown gracefully
                # tolerates unfinished constructs while a model is still writing;
                # the fixed panel height keeps this from scrolling the terminal.
                markdown_renderable(self._tail_preview(label)),
                title=label,
                subtitle=f"{self.status[label]} · 实时 Markdown 预览",
                border_style="cyan" if self.status[label] not in {"失败"} else "red",
                padding=(0, 1),
                height=self.panel_height,
            )
            for label in self.labels
        ]
        # Rich automatically adapts: several columns on a wide screen, stacked
        # panels on narrow terminals. Live remains the only terminal writer.
        return Group(
            Text("etm · 并发流式回答（Ctrl-C 取消；完成后显示完整 Markdown）", style="bold"),
            Columns(panels, equal=True, expand=True),
        )

    def _tail_preview(self, label: str) -> str:
        content = clean_terminal_text(self.buffers[label])
        if not content:
            return "等待回复…"
        # Keep a bounded character count as a single streaming paragraph can be
        # very long before it emits a newline. Then retain the most recent lines.
        content = content[-1600:]
        lines = content.splitlines()
        preview = "\n".join(lines[-9:])
        return preview or content[-500:]

    def clear(self) -> None:
        if self.live:
            self.live.stop()
            self.live = None


def run_question(args: argparse.Namespace) -> int:
    config = load_config()
    tokens = list(args.tokens)
    if not tokens:
        raise EtmError("请提供问题，例如：etm what is TAM SAM SOM")
    # `detailed` is a convenient positional keyword; --detailed is the
    # unambiguous alternative when the question itself starts with that word.
    detailed = args.detailed
    if tokens and tokens[0].lower() == "detailed":
        detailed = True
        tokens.pop(0)
    aliases: list[str] = []
    while tokens and tokens[0] in config["models"]:
        aliases.append(tokens.pop(0))
    if not tokens:
        raise EtmError("请选择模型后仍需要提供问题。")
    question = " ".join(tokens)
    selected = aliases or [config["default_model"]]
    resolved = [(alias, resolve_model(alias, config)) for alias in selected]
    key = get_api_key()
    labels = [f"{alias} · {model}" for alias, model in resolved]
    dashboard = LiveDashboard(labels)
    events: queue.Queue[tuple[str, str, str]] = queue.Queue()

    def consume(label: str, model: str, prompt: str) -> None:
        events.put((label, "status", "连接中"))
        try:
            for delta in stream_openrouter(prompt, model, config, key, args.timeout, detailed):
                events.put((label, "delta", delta))
            events.put((label, "status", "完成"))
        except Exception as exc:
            events.put((label, "error", str(exc)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(resolved)) as pool:
        futures = [pool.submit(consume, label, model, question) for label, (_, model) in zip(labels, resolved)]
        remaining = len(futures)
        last_draw = 0.0
        while remaining:
            try:
                label, kind, value = events.get(timeout=0.05)
                if kind == "delta":
                    dashboard.update(label, delta=value, status="回答中")
                elif kind == "error":
                    dashboard.update(label, delta=f"\n[请求失败] {value}", status="失败")
                else:
                    dashboard.update(label, status=value)
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last_draw >= 0.08:
                dashboard.render()
                last_draw = now
            remaining = sum(not future.done() for future in futures)
        # Drain events queued immediately before a worker finished.
        while True:
            try:
                label, kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == "delta":
                dashboard.update(label, delta=value, status="回答中")
            elif kind == "error":
                dashboard.update(label, delta=f"\n[请求失败] {value}", status="失败")
            else:
                dashboard.update(label, status=value)
        dashboard.render(force=True)
    answers = [(alias, model, dashboard.buffers[label].strip())
               for label, (alias, model) in zip(labels, resolved)]
    dashboard.clear()
    for alias, model, answer in answers:
        print_section(f"{alias} · {model}", answer)
    successful = [(alias, model, text) for alias, model, text in answers if not text.startswith("[请求失败]")]
    if len(successful) > 1 and not args.no_synthesis:
        synthesizer = resolve_model(args.synth or config["synthesis_model"], config)
        source_text = "\n\n".join(f"### {alias} ({model})\n{answer}" for alias, model, answer in successful)
        prompt = textwrap.dedent(f"""\
            用户的问题是：{question}

            以下是不同模型对该问题的回答。请用与用户相同的语言做一个可靠的综合：
            1. 先直接给出你认为最有帮助的结论；
            2. 列出回答的共同点；
            3. 说明实质性不同点、各自可能的盲点或不确定性；
            4. 不要把来源回答当作事实，纠正明显错误。

            {source_text}
        """)
        try:
            # Reuse the same single-writer dashboard for the final streamed synthesis.
            synthesis_label = f"综合 · {synthesizer}"
            synthesis_dashboard = LiveDashboard([synthesis_label])
            synthesis_dashboard.update(synthesis_label, status="连接中")
            for delta in stream_openrouter(prompt, synthesizer, config, key, args.timeout, detailed):
                synthesis_dashboard.update(synthesis_label, delta=delta, status="综合中")
                synthesis_dashboard.render()
            synthesis_dashboard.update(synthesis_label, status="完成")
            synthesis_dashboard.render(force=True)
            synthesis = synthesis_dashboard.buffers[synthesis_label].strip()
            synthesis_dashboard.clear()
            print_section(f"综合 · {synthesizer}", synthesis)
        except Exception as exc:
            print_section(f"综合 · {synthesizer}", f"[请求失败] {exc}")
    return 0


def config_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="etm config")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="显示当前配置（不显示 API key）")
    sub.add_parser("set-key", help="输入并保存 API key 到 macOS Keychain")
    sub.add_parser("delete-key", help="从 macOS Keychain 删除 API key")
    base = sub.add_parser("set-base-url", help="修改 OpenRouter-compatible base URL")
    base.add_argument("url")
    for action, help_text in (("set-default", "设置默认回答模型"), ("set-synth", "设置多模型综合模型")):
        command = sub.add_parser(action, help=help_text)
        command.add_argument("model")
    model = sub.add_parser("set-model", help="新增或覆盖模型别名")
    model.add_argument("alias")
    model.add_argument("model_id")
    remove = sub.add_parser("delete-model", help="删除模型别名")
    remove.add_argument("alias")
    uninstall = sub.add_parser("uninstall", help="删除 etm 配置和 Keychain 中的 key")
    uninstall.add_argument("--yes", action="store_true", help="确认删除")
    args = parser.parse_args(argv)
    config = load_config()
    if args.action == "show":
        print(json.dumps(config, indent=2, ensure_ascii=False))
        print("\nAPI key: " + ("已在环境变量或 Keychain 中找到" if env_key() or keychain_key() else "未设置"))
    elif args.action == "set-key":
        key = getpass.getpass("OpenRouter API key: ").strip()
        if not key:
            raise EtmError("没有输入 API key。")
        save_keychain_key(key)
        print("已保存到 macOS Keychain。")
    elif args.action == "delete-key":
        print("已删除 Keychain 中的 API key。" if delete_keychain_key() else "Keychain 中没有 etm 的 API key。")
    elif args.action == "set-base-url":
        config["base_url"] = args.url.rstrip("/")
        save_config(config)
        print(f"Base URL 已设为 {config['base_url']}")
    elif args.action in {"set-default", "set-synth"}:
        config["default_model" if args.action == "set-default" else "synthesis_model"] = args.model
        save_config(config)
        print(f"已更新 {args.action}。")
    elif args.action == "set-model":
        if not args.alias.replace("-", "").replace("_", "").isalnum():
            raise EtmError("别名只能使用字母、数字、- 和 _。")
        config["models"][args.alias] = args.model_id
        save_config(config)
        print(f"{args.alias} → {args.model_id}")
    elif args.action == "delete-model":
        if args.alias not in config["models"]:
            raise EtmError(f"没有模型别名 {args.alias!r}。")
        if args.alias in {config["default_model"], config["synthesis_model"]}:
            raise EtmError("此别名正在被默认/综合模型使用；请先修改对应设置。")
        del config["models"][args.alias]
        save_config(config)
        print(f"已删除别名 {args.alias}。")
    elif args.action == "uninstall":
        if not args.yes:
            raise EtmError("这会删除 ~/.config/etm/config.json 和 Keychain 中的 key。请加 --yes 确认。")
        deleted = delete_keychain_key()
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        try:
            APP_DIR.rmdir()
        except OSError:
            pass
        print("已删除 etm 配置" + ("及 Keychain API key。" if deleted else "。Keychain 中没有对应 key。"))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        try:
            return config_command(sys.argv[2:])
        except EtmError as exc:
            print(f"etm: {exc}", file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(
        prog="etm", description="Explain To Me — 通过 OpenRouter 提问一个或多个模型。",
        epilog="例：etm what is TAM SAM SOM   |   etm detailed gpt what is SWOT   |   etm gpt opus ds what is BMC",
    )
    parser.add_argument("--synth", metavar="MODEL", help="多模型综合使用的别名或完整 model ID")
    parser.add_argument("--no-synthesis", action="store_true", help="多模型时不生成综合结论")
    parser.add_argument("--detailed", action="store_true", help="输出详细回答（也可将 detailed 放在命令最前）")
    parser.add_argument("--timeout", type=int, default=90, help="每个请求的超时秒数（默认 90）")
    parser.add_argument("tokens", nargs=argparse.REMAINDER, help="可选模型别名，后接问题")
    args = parser.parse_args()
    try:
        return run_question(args)
    except EtmError as exc:
        print(f"etm: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\netm: 已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
