# -*- coding: utf-8 -*-
"""把一组 PNG 打包成 Windows 用的 .ico。

Vista 之后的 ico 允许直接内嵌 PNG，所以不需要任何图像库，
把各尺寸的 PNG 按 ico 的目录结构拼起来就行。尺寸越全，图标在各处越清晰
（16 用于列表和标题栏，32/48 用于桌面和任务栏，256 用于大图标视图）。

用法：
    python make_ico.py 输出.ico 16.png 32.png 48.png 256.png
"""

import struct
import sys


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s 不是 PNG 文件" % path)
    return struct.unpack(">II", head[16:24])


def build_ico(png_paths, out_path):
    entries = []
    images = []
    offset = 6 + 16 * len(png_paths)
    for path in png_paths:
        with open(path, "rb") as f:
            data = f.read()
        width, height = png_size(path)
        # 目录项里用 0 表示 256
        entries.append(struct.pack(
            "<BBBBHHII",
            width if width < 256 else 0,
            height if height < 256 else 0,
            0, 0, 1, 32, len(data), offset,
        ))
        offset += len(data)
        images.append(data)

    with open(out_path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(png_paths)))  # 保留位、类型=图标、数量
        for entry in entries:
            f.write(entry)
        for image in images:
            f.write(image)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    build_ico(sys.argv[2:], sys.argv[1])
    print("已生成 %s（含 %d 个尺寸）" % (sys.argv[1], len(sys.argv) - 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
