"""HAKOYA の MCP サーバー（stdio）。社外の会社の人が、自分の Claude Code から HAKOYA の道具を使う入り口（docs/mcp.md）。

使い方:  python -m app.mcp_server        （exe では HAKOYA_external.exe --mcp。main() を呼ぶ）
- 道具とプロンプトの中身は core/ai/mcp_tools.py。ここは MCP のやりとり（一覧・呼び出し・プロンプト）だけ。
- サーバーの名前は hakoya。Claude Code では道具が mcp__hakoya__<名前>、検品の手順が /mcp__hakoya__inspect になる。
- HAKOYA は Claude を呼ばない（道具を渡すだけ。Claude Code は、その会社の人の認証とプランで動く）。
- **stdout は MCP のやりとりの道**なので、何も print しない。記録は stderr に、ファイルの場所を書かずに出す。
- 模型を変える道具には _meta の anthropic/requiresUserInteraction を付け、Claude Code が毎回人に許可を求めるようにする
  （「いつも許可」の決まりがあっても、許可の画面が出る）。
"""
from __future__ import annotations

import base64
import contextlib
import logging
import re
import sys
import traceback
import warnings
from pathlib import Path

REQUIRES_HUMAN = "anthropic/requiresUserInteraction"
# 記録の文の中の場所らしい所（ドライブ名・\\ で始まる場所・/ で挟まれた名前）。http:// などのアドレスは残す
_PATHISH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s'\"]*|\\\\[^\s'\"]+|(?<![\w.:/])/(?:[^\s'\"/]+/)+[^\s'\"]*")


class SafeFormatter(logging.Formatter):
    """stderr の記録に場所を出さない：文の中の場所らしい所を消し、エラーは種類と HAKOYA のファイルの名前・行だけにする。"""

    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        out = f"[HAKOYA MCP] {record.levelname} {record.name}: {_PATHISH.sub('<場所>', msg)}"
        if record.exc_info and record.exc_info[1] is not None:
            e = record.exc_info[1]
            frames = traceback.extract_tb(e.__traceback__)[-4:]
            where = " ← ".join(f"{Path(f.filename).name}:{f.lineno}" for f in reversed(frames)) or "?"
            out += f"（{type(e).__name__}：{where}）"
        return out


def _setup_logging() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeFormatter())
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("hakoya").setLevel(logging.INFO)
    logging.raiseExceptions = False          # 記録の失敗で、場所の入ったエラーの表示を出さない


def build_server():
    """MCP の低い層のサーバー（mcp 2.x の Server）に、HAKOYA の道具とプロンプトをつなぐ。"""
    import anyio
    import mcp.types as types
    from mcp.server import Server
    from mcp.shared.exceptions import MCPError

    from core.ai import mcp_tools as mt

    tools = []
    for d in mt.TOOLS.values():
        tools.append(types.Tool(
            name=d.name, title=d.title, description=d.description, input_schema=d.schema,
            annotations=types.ToolAnnotations(title=d.title, read_only_hint=d.read_only, destructive_hint=d.destructive,
                                              idempotent_hint=d.idempotent, open_world_hint=False),
            meta={REQUIRES_HUMAN: True} if d.ask_human else None))
    prompts = [types.Prompt(name=p.name, title=p.title, description=p.description,
                            arguments=[types.PromptArgument(name=a.name, description=a.description, required=a.required)
                                       for a in p.arguments]) for p in mt.PROMPTS.values()]

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        # 道具は普通の関数（数秒かかるものもある）なので、別の糸で動かす（やりとりを止めない）
        res = await anyio.to_thread.run_sync(mt.call, params.name, dict(params.arguments or {}))
        content: list = [types.TextContent(text=res.text)]
        content += [types.ImageContent(data=base64.b64encode(png).decode("ascii"), mime_type="image/png")
                    for png in res.images]
        return types.CallToolResult(content=content, is_error=res.is_error)

    async def on_list_prompts(ctx, params) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=prompts)

    async def on_get_prompt(ctx, params) -> types.GetPromptResult:
        from core.privacy import PrivacyError
        try:
            text = await anyio.to_thread.run_sync(mt.get_prompt, params.name, dict(params.arguments or {}))
        except PrivacyError as e:
            raise MCPError(code=types.INVALID_PARAMS, message=str(e)) from None
        except Exception as e:  # noqa: BLE001  文は返さない（場所が入ることがある）
            mt._log_error("プロンプト", e)
            raise MCPError(code=types.INTERNAL_ERROR, message=mt.UNEXPECTED.format(type(e).__name__)) from None
        if text is None:
            raise MCPError(code=types.INVALID_PARAMS, message="その名前のプロンプトはありません（inspect だけです）")
        p = mt.PROMPTS[params.name]
        return types.GetPromptResult(description=p.description,
                                     messages=[types.PromptMessage(role="user", content=types.TextContent(text=text))])

    return Server(mt.SERVER_NAME, version=mt.VERSION, title="HAKOYA（箱家）", instructions=mt.INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool,
                  on_list_prompts=on_list_prompts, on_get_prompt=on_get_prompt)


def main(argv: list[str] | None = None) -> int:
    """stdio で MCP のやりとりを始める（相手の Claude Code が stdin を閉じると終わる）。argv（--mcp など）は使わない。"""
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")     # 日本語の記録で止まらないように
    warnings.simplefilter("ignore")          # 注意の表示には、部品の場所（フォルダ）が入る
    _setup_logging()
    log = logging.getLogger("hakoya.mcp")
    with contextlib.redirect_stdout(sys.stderr):      # 読み込みの間の print も、やりとりの道（stdout）に混ぜない
        server = build_server()
        import anyio
        from mcp.server.stdio import stdio_server

    async def serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    from core.ai.mcp_tools import TOOLS
    log.info("HAKOYA の MCP サーバーを起動しました（道具 %d 個）", len(TOOLS))
    try:
        anyio.run(serve)
    except KeyboardInterrupt:
        pass
    log.info("HAKOYA の MCP サーバーを止めました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
