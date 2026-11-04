"""テスト用の小さな PDF を、部品を使わずに中身を直接書いて作る（PDF のファイルはコミットしない決まりなので、テストの中で作る）。

content は PDF のページの描画の命令（例：b"1 w 100 100 m 300 100 l S"）。座標は pt で、左下が原点（PDF の決まり）。
F1 は英数字の字（Helvetica）、F2 は日本語の字（平成角ゴシック、UniJIS-UCS2-H で UTF-16 の16進を書く）、
F3 は文字の表（ToUnicode）がない字（読むと (cid:65) のようになる。文字の表のない PDF の代わり）。
"""
from __future__ import annotations

MM = 72.0 / 25.4          # 1mm が何 pt か


def pdf_bytes(content: bytes, w: float = 1190.55, h: float = 841.89, rotate: int = 0, image: bool = False,
              info: dict[str, str] | None = None) -> bytes:
    """info：文書の情報（作成者 Author・題名 Title など）。"""
    res_img = b"/XObject<</Im1 10 0 R>>" if image else b""
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 %.2f %.2f]/Rotate %d/Contents 4 0 R"
            b"/Resources<</Font<</F1 5 0 R/F2 6 0 R/F3 8 0 R>>%s>>>>" % (w, h, rotate, res_img),
            b"<</Length %d>>stream\n%s\nendstream" % (len(content), content),
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
            b"<</Type/Font/Subtype/Type0/BaseFont/HeiseiKakuGo-W5/Encoding/UniJIS-UCS2-H/DescendantFonts[7 0 R]>>",
            b"<</Type/Font/Subtype/CIDFontType0/BaseFont/HeiseiKakuGo-W5"
            b"/CIDSystemInfo<</Registry(Adobe)/Ordering(Japan1)/Supplement 2>>/DW 1000>>",
            b"<</Type/Font/Subtype/Type0/BaseFont/NoMap/Encoding/Identity-H/DescendantFonts[9 0 R]>>",
            b"<</Type/Font/Subtype/CIDFontType2/BaseFont/NoMap"
            b"/CIDSystemInfo<</Registry(Adobe)/Ordering(Identity)/Supplement 0>>/DW 1000>>"]
    if image:   # 1×1 の灰色の画像（スキャンの PDF の代わり。content で用紙いっぱいに描く）
        objs.append(b"<</Type/XObject/Subtype/Image/Width 1/Height 1/ColorSpace/DeviceGray/BitsPerComponent 8/Length 1>>stream\n\x80\nendstream")
    if info:    # 文書の情報（UTF-16 の16進）
        objs.append(b"<<" + b"".join(b"/%s <FEFF%s>" % (k.encode(), v.encode("utf-16-be").hex().encode()) for k, v in info.items()) + b">>")
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    info_ref = b"/Info %d 0 R" % len(objs) if info else b""
    return bytes(out + b"trailer<</Size %d/Root 1 0 R%s>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, info_ref, x))


def jp(text: str) -> bytes:
    """日本語の文字を F2 で書く命令の中身（<16進> Tj）。"""
    return b"<" + text.encode("utf-16-be").hex().encode() + b"> Tj"


def line(x0_mm: float, y0_mm: float, x1_mm: float, y1_mm: float) -> bytes:
    """用紙の左下からの mm で線を1本。"""
    return b"%.3f %.3f m %.3f %.3f l S\n" % (x0_mm * MM, y0_mm * MM, x1_mm * MM, y1_mm * MM)


def text(x_mm: float, y_mm: float, body: bytes, font: bytes = b"F1", size: float = 8.0) -> bytes:
    """用紙の左下からの mm に、字の並びの左下を置いて書く（body は (ABC) Tj か jp(...)）。"""
    return b"BT /%s %.1f Tf %.3f %.3f Td %s ET\n" % (font, size, x_mm * MM, y_mm * MM, body)
