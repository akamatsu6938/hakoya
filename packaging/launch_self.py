"""HAKOYA の exe の起動口。PyInstaller はこのファイルから exe を作る（packaging/hakoya_self.spec・hakoya_external.spec）。

起動のしかた（★11/5）：自分用の exe は --mode sample（見本用）・own（自社用）・other（他社用）で選べる（HAKOYA_MODE と同じ。
省くと HAKOYA_MODE、それもなければ既定）。例：HAKOYA.exe --mode sample
社外用の exe は、exe の中の印（EDITION）で、いつも他社用（--mode を書いても変わらない。core/paths.py）。
--mcp を付けると、画面の代わりに MCP の口（stdio）で起動する（社外の Claude Code とつなぐ。docs/mcp.md）。
起動のしかたは core/paths.py を読み込んだときに決まるので、app.server を読み込む前に HAKOYA_MODE を書く。
"""
import os
import sys

MODES = ("own", "other", "sample")       # core/paths.py の MODES と同じ（ここで core を読み込むと、先に起動のしかたが決まってしまう）


def mode_from_args(argv: list[str]) -> str | None:
    """--mode X か --mode=X の X。書いていなければ None（--mode だけで値がなければ ""）。"""
    for i, a in enumerate(argv):
        if a == "--mode":
            return argv[i + 1] if i + 1 < len(argv) else ""
        if a.startswith("--mode="):
            return a.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    mode = mode_from_args(sys.argv[1:] if argv is None else argv)
    if mode is not None:
        mode = mode.strip().lower()
        if mode not in MODES:
            print("--mode は own（自社用）・other（他社用）・sample（見本用）のどれかにしてください（例：HAKOYA.exe --mode sample）")
            return 2
        os.environ["HAKOYA_MODE"] = mode
    if "--mcp" in (sys.argv[1:] if argv is None else argv):   # 社外の Claude Code とつなぐ（stdout は MCP のやりとりの道）
        from app.mcp_server import main as mcp_main
        return mcp_main()
    from app.server import main as serve
    return serve()


if __name__ == "__main__":
    sys.exit(main())
