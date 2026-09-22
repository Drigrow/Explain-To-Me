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
from .i18n import LANGUAGES, tr


APP_DIR = Path.home() / ".config" / "etm"
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
KEYCHAIN_SERVICE = "etm-openrouter"
KEYCHAIN_ACCOUNT = "openrouter-api-key"
DEFAULTS: dict[str, Any] = {
    "base_url": "https://openrouter.ai/api/v1",
    "default_model": "gemini",
    "synthesis_model": "gemini",
    "language": "en",
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
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT = 12_000


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
    for key in ("base_url", "default_model", "synthesis_model", "language"):
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


def load_last_run() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        last_run = state.get("last_run")
        return last_run if isinstance(last_run, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_last_run(models: list[tuple[str, str]], histories: dict[str, list[dict[str, str]]], detailed: bool, answer_language: str | None) -> None:
    """Persist only the most recent conversation; keys remain in macOS Keychain."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    serialised = {
        key: [{"role": message["role"], "content": message["content"][-MAX_HISTORY_CONTENT:]}
              for message in messages[-MAX_HISTORY_MESSAGES:]
              if message.get("role") in {"user", "assistant"} and isinstance(message.get("content"), str)]
        for key, messages in histories.items()
    }
    STATE_PATH.write_text(json.dumps({"last_run": {
        "models": [{"alias": alias, "model": model} for alias, model in models],
        "histories": serialised, "detailed": detailed, "answer_language": answer_language,
    }}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_PATH.chmod(0o600)


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
    answer_language: str | None = None, history: list[dict[str, str]] | None = None,
):
    """Yield content deltas from OpenRouter's OpenAI-compatible SSE endpoint."""
    url = config["base_url"].rstrip("/") + "/chat/completions"
    instruction = DETAILED_ANSWER_INSTRUCTION if detailed else SHORT_ANSWER_INSTRUCTION
    if answer_language:
        instruction += f" Always answer in {LANGUAGES[answer_language]}, regardless of the question's language."
    body = json.dumps({
        "model": model,
        "stream": True,
        "messages": [{"role": "system", "content": instruction}, *(history or []), {"role": "user", "content": question}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "etm-openrouter/0.4.0",
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
MATH_MACROS = {
    r"\infty": "∞", r"\omega": "ω", r"\Omega": "Ω", r"\pi": "π", r"\Pi": "Π",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\theta": "θ",
    r"\lambda": "λ", r"\mu": "μ", r"\sigma": "σ", r"\sum": "∑", r"\prod": "∏",
    r"\log": "log", r"\ln": "ln", r"\left": "", r"\right": "", r"\,": " ", r"\!": "",
}


def normalise_fractions(markup: str) -> str:
    """unicodeitplus handles symbols and scripts; simplify common TeX fractions."""
    for _ in range(8):  # also resolves nested fractions from the inside out
        updated = FRACTION_RE.sub(lambda m: f"({m.group(1)})/({m.group(2)})", markup)
        if updated == markup:
            return markup
        markup = updated
    return markup


def normalise_math_macros(formula: str) -> str:
    """Make common macros robust inside nested superscripts/subscripts."""
    for macro, replacement in MATH_MACROS.items():
        formula = formula.replace(macro, replacement)
    # unicodeitplus renders scripts more cleanly without single-level grouping
    # braces (for example, `e^{-iωt}` -> `e⁻ⁱωt`). Preserve deeper groups.
    formula = re.sub(r"([_^])\{([^{}]+)\}", r"\1\2", formula)
    return formula


def latex_to_terminal(markup: str) -> str:
    """Convert `$...$`, `$$...$$`, `\\(...\\)` and `\\[...\\]` to Unicode math.

    A terminal cannot typeset TeX glyphs like a browser, but unicodeitplus provides
    a readable Unicode approximation. On an unsupported expression we
    retain the original formula instead of silently losing information.
    """
    def convert(match: re.Match[str], block: bool = False) -> str:
        original = match.group(0)
        try:
            # Some providers escape Markdown math a second time and stream
            # `\\omega` / `\\int` instead of standard `\omega` / `\int`.
            # Normalise only inside a math delimiter, never in ordinary prose.
            formula = normalise_math_macros(match.group(1).replace("\\\\", "\\"))
            converted = latex_to_unicode(normalise_fractions(formula)).strip()
            # The converter occasionally leaves grouping braces around a Unicode
            # superscript/subscript token (notably `^{∞}`). They add no meaning
            # in terminal output, so remove only these remaining math groups.
            converted = re.sub(r"\{([^{}]+)\}", r"\1", converted)
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


def markdown_renderable(content: str, language: str = "en") -> Markdown:
    return Markdown(latex_to_terminal(clean_terminal_text(content)) or tr(language, "no_content"))


def print_section(title: str, content: str, language: str = "en") -> None:
    """Render completed model output as Markdown, including tables and code blocks."""
    CONSOLE.rule(title)
    CONSOLE.print(markdown_renderable(content, language))


ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\a]*(?:\a|\x1b\\))")


def clean_terminal_text(text: str) -> str:
    """A model must not be able to inject terminal control sequences into the dashboard."""
    return ANSI_ESCAPE.sub("", text).replace("\r", "")


class LiveDashboard:
    """One-writer terminal renderer for concurrent model streams.

    Workers only publish events; the main thread owns stdout, so no streams can
    interleave or leave a half-drawn terminal frame.
    """

    def __init__(self, labels: list[str], language: str = "en") -> None:
        self.enabled = CONSOLE.is_terminal
        self.labels = labels
        self.language = language
        self.buffers = {label: "" for label in labels}
        self.status = {label: tr(language, "waiting") for label in labels}
        # Live can only redraw output which still fits above the cursor. Rendering
        # an ever-growing full response causes terminals to scroll each frame.
        # A constant-height tail preview keeps every frame safely replaceable.
        # Fit every selected model into the current terminal. Previously three
        # 14-line panels could exceed its height, and Rich hid the last panel.
        available_rows = max(6, CONSOLE.size.height - 3)
        self.panel_height = max(6, min(14, available_rows // max(1, len(labels))))
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
                markdown_renderable(self._tail_preview(label), self.language),
                title=label,
                subtitle=f"{self.status[label]} · {tr(self.language, 'preview')}",
                border_style="cyan" if self.status[label] != tr(self.language, "failed") else "red",
                padding=(0, 1),
                height=self.panel_height,
            )
            for label in self.labels
        ]
        # Rich automatically adapts: several columns on a wide screen, stacked
        # panels on narrow terminals. Live remains the only terminal writer.
        return Group(
            Text(tr(self.language, "app"), style="bold"),
            Columns(panels, equal=True, expand=True),
        )

    def _tail_preview(self, label: str) -> str:
        content = clean_terminal_text(self.buffers[label])
        if not content:
            return tr(self.language, "waiting")
        # Keep a bounded character count as a single streaming paragraph can be
        # very long before it emits a newline. Then retain the most recent lines.
        content = content[-1600:]
        lines = content.splitlines()
        preview = "\n".join(lines[-max(2, self.panel_height - 4):])
        return preview or content[-500:]

    def clear(self) -> None:
        if self.live:
            self.live.stop()
            self.live = None


def run_question(args: argparse.Namespace) -> int:
    config = load_config()
    language = config["language"] if config["language"] in LANGUAGES else "en"
    tokens = list(args.tokens)
    if not tokens:
        raise EtmError(tr(language, "missing_question"))
    # `detailed` is a convenient positional keyword; --detailed is the
    # unambiguous alternative when the question itself starts with that word.
    continuing = bool(tokens and tokens[0].lower() == "continue")
    if continuing:
        tokens.pop(0)
    detailed = args.detailed
    answer_language = args.lang
    if answer_language:
        answer_language = answer_language.lower().replace("_", "-").split("-", 1)[0]
        if answer_language not in LANGUAGES:
            raise EtmError(tr(language, "language_invalid", code=answer_language, available=", ".join(LANGUAGES)))
    while tokens:
        keyword = tokens[0].lower()
        if keyword == "detailed":
            detailed = True
            tokens.pop(0)
        elif keyword in LANGUAGES:
            answer_language = tokens.pop(0).lower()
        else:
            break
    aliases: list[str] = []
    last_run = load_last_run() if continuing else None
    if continuing:
        if not last_run or not isinstance(last_run.get("models"), list):
            raise EtmError("No previous conversation found. Ask a question before using `etm continue`.")
        detailed = args.detailed or bool(last_run.get("detailed"))
        answer_language = answer_language or last_run.get("answer_language")
    else:
        while tokens and tokens[0] in config["models"]:
            aliases.append(tokens.pop(0))
    if not tokens:
        raise EtmError("请选择模型后仍需要提供问题。")
    question = " ".join(tokens)
    selected = aliases or [config["default_model"]]
    resolved = ([(str(item["alias"]), str(item["model"])) for item in last_run["models"]
                 if isinstance(item, dict) and isinstance(item.get("alias"), str) and isinstance(item.get("model"), str)]
                if continuing else [(alias, resolve_model(alias, config)) for alias in selected])
    if not resolved:
        raise EtmError("The previous conversation has no usable model context.")
    key = get_api_key()
    labels = [f"{alias} · {model}" for alias, model in resolved]
    dashboard = LiveDashboard(labels, language)
    events: queue.Queue[tuple[str, str, str]] = queue.Queue()
    histories = (last_run.get("histories", {}) if continuing and isinstance(last_run.get("histories"), dict) else {})

    def consume(label: str, model: str, prompt: str) -> None:
        events.put((label, "status", tr(language, "connecting")))
        try:
            prior = histories.get(label, [])
            safe_prior = prior if isinstance(prior, list) else []
            for delta in stream_openrouter(prompt, model, config, key, args.timeout, detailed, answer_language, safe_prior):
                events.put((label, "delta", delta))
            events.put((label, "status", tr(language, "complete")))
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
                    dashboard.update(label, delta=value, status=tr(language, "answering"))
                elif kind == "error":
                    dashboard.update(label, delta=f"\n[request failed] {value}", status=tr(language, "failed"))
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
                dashboard.update(label, delta=value, status=tr(language, "answering"))
            elif kind == "error":
                dashboard.update(label, delta=f"\n[request failed] {value}", status=tr(language, "failed"))
            else:
                dashboard.update(label, status=value)
        dashboard.render(force=True)
    answers = [(alias, model, dashboard.buffers[label].strip())
               for label, (alias, model) in zip(labels, resolved)]
    dashboard.clear()
    for alias, model, answer in answers:
        print_section(f"{alias} · {model}", answer, language)
    successful = [(alias, model, text) for alias, model, text in answers if not text.startswith("[request failed]")]
    for alias, model, answer in successful:
        label = f"{alias} · {model}"
        previous = histories.get(label, [])
        histories[label] = [*previous, {"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    save_last_run(resolved, histories, detailed, answer_language)
    if len(successful) > 1 and not args.no_synthesis:
        synthesizer = resolve_model(args.synth or config["synthesis_model"], config)
        source_text = "\n\n".join(f"### {alias} ({model})\n{answer}" for alias, model, answer in successful)
        language_rule = (
            f"Required output language: {LANGUAGES[answer_language]}. Use it regardless of the "
            "original question or source-answer language."
            if answer_language else
            "Write in the same language as the original question and the source answers. Do not translate the answer into a different language."
        )
        prompt = textwrap.dedent(f"""\
            Original user question: {question}

            Below are answers from several models. Produce a reliable synthesis.
            Important language rule: {language_rule}
            1. Lead with the most helpful conclusion;
            2. Identify points shared by the answers;
            3. Explain substantive differences, blind spots, or uncertainties;
            4. Do not treat source answers as facts; correct clear errors.

            {source_text}
        """)
        try:
            # Reuse the same single-writer dashboard for the final streamed synthesis.
            synthesis_label = f"{tr(language, 'synthesis')} · {synthesizer}"
            synthesis_dashboard = LiveDashboard([synthesis_label], language)
            synthesis_dashboard.update(synthesis_label, status=tr(language, "connecting"))
            for delta in stream_openrouter(prompt, synthesizer, config, key, args.timeout, detailed, answer_language):
                synthesis_dashboard.update(synthesis_label, delta=delta, status=tr(language, "synthesizing"))
                synthesis_dashboard.render()
            synthesis_dashboard.update(synthesis_label, status=tr(language, "complete"))
            synthesis_dashboard.render(force=True)
            synthesis = synthesis_dashboard.buffers[synthesis_label].strip()
            synthesis_dashboard.clear()
            print_section(f"{tr(language, 'synthesis')} · {synthesizer}", synthesis, language)
        except Exception as exc:
            print_section(f"{tr(language, 'synthesis')} · {synthesizer}", f"[request failed] {exc}", language)
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
    language_command = sub.add_parser("set-language", help="set the interface language")
    language_command.add_argument("language", help="language code, e.g. en, zh, ja")
    uninstall = sub.add_parser("uninstall", help="删除 etm 配置和 Keychain 中的 key")
    uninstall.add_argument("--yes", action="store_true", help="确认删除")
    args = parser.parse_args(argv)
    config = load_config()
    if args.action == "show":
        print(json.dumps(config, indent=2, ensure_ascii=False))
        language = config["language"] if config["language"] in LANGUAGES else "en"
        key_state = tr(language, "api_key_set") if env_key() or keychain_key() else tr(language, "api_key_missing")
        print("\n" + tr(language, "api_key_status") + ": " + key_state)
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
    elif args.action == "set-language":
        code = args.language.lower().replace("_", "-").split("-", 1)[0]
        if code not in LANGUAGES:
            raise EtmError(tr(config.get("language", "en"), "language_invalid", code=code, available=", ".join(LANGUAGES)))
        config["language"] = code
        save_config(config)
        print(tr(code, "language_updated", name=LANGUAGES[code], code=code))
    elif args.action == "uninstall":
        if not args.yes:
            raise EtmError("这会删除 ~/.config/etm/config.json 和 Keychain 中的 key。请加 --yes 确认。")
        deleted = delete_keychain_key()
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        if STATE_PATH.exists():
            STATE_PATH.unlink()
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
        prog="etm", description="Explain To Me — ask one or more models through OpenRouter.",
        epilog="Examples: etm what is TAM SAM SOM | etm zh what is SWOT | etm continue explain with an example",
    )
    parser.add_argument("--synth", metavar="MODEL", help="多模型综合使用的别名或完整 model ID")
    parser.add_argument("--no-synthesis", action="store_true", help="多模型时不生成综合结论")
    parser.add_argument("--detailed", action="store_true", help="输出详细回答（也可将 detailed 放在命令最前）")
    parser.add_argument("--lang", metavar="CODE", help="force answer language, e.g. zh, ja, en")
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
