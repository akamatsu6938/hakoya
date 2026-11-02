"""HAKOYA（自分用ビルド）の起動口。PyInstaller はこのファイルから exe を作る（packaging/hakoya_self.spec）。"""
import sys

from app.server import main

if __name__ == "__main__":
    sys.exit(main())
