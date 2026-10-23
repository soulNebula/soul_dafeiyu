"""GUI 冒烟测试用媒体素材生成器（PNG / GIF / PDF）。

测试直接调用 :func:`make_media` 在临时目录里生成素材，
不依赖仓库里提交的二进制文件，避免跨环境不一致。
"""
from __future__ import annotations

from pathlib import Path


def make_media(directory: str | Path) -> dict[str, Path]:
    """在指定目录生成 sample.png / sample.gif / sample.pdf 并返回路径字典。"""
    from PIL import Image, ImageDraw

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    # 1) 静态 PNG：渐变背景 + 文字
    png_path = target / "sample.png"
    image = Image.new("RGB", (240, 160), "#1e1f22")
    draw = ImageDraw.Draw(image)
    for y in range(160):
        ratio = y / 159
        draw.line(
            [(0, y), (240, y)],
            fill=(int(30 + 40 * ratio), int(60 + 100 * ratio), int(80 + 60 * ratio)),
        )
    draw.rounded_rectangle((16, 16, 224, 144), radius=14, outline="#3eb575", width=2)
    draw.text((120, 80), "WXBOX TEST IMAGE", fill="#eafff3", anchor="mm")
    image.save(png_path, "PNG")

    # 2) GIF 动图：3 帧移动色块（足够验证循环播放与 stop 清理）
    gif_path = target / "sample.gif"
    frames = []
    for index in range(3):
        frame = Image.new("RGB", (64, 64), "#1e1f22")
        frame_draw = ImageDraw.Draw(frame)
        left = 4 + index * 16
        frame_draw.rectangle((left, 18, left + 24, 46), fill="#ffcb6b")
        frame_draw.text((32, 56), "GIF", fill="#a8abb2", anchor="mm")
        frames.append(frame)
    frames[0].save(
        gif_path,
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=60,
        loop=0,
        disposal=2,
    )

    # 3) 最小可用 PDF（只用于文件卡片展示与大小计算）
    pdf_path = target / "sample.pdf"
    pdf_path.write_bytes(_minimal_pdf())

    # 4) 超大 PNG（验证普通图片预览最大 320 宽、等比缩放）
    big_png_path = target / "big.png"
    big_image = Image.new("RGB", (800, 600), "#1e1f22")
    big_draw = ImageDraw.Draw(big_image)
    for y in range(600):
        ratio = y / 599
        big_draw.line(
            [(0, y), (800, y)],
            fill=(int(30 + 60 * ratio), int(50 + 120 * ratio), int(70 + 60 * ratio)),
        )
    big_draw.text((400, 300), "BIG IMAGE 800x600", fill="#eafff3", anchor="mm")
    big_image.save(big_png_path, "PNG")

    # 5) 超大 GIF（验证表情包预览最大 160×160）
    big_gif_path = target / "big.gif"
    big_frames = []
    for index in range(3):
        frame = Image.new("RGB", (300, 300), "#1e1f22")
        frame_draw = ImageDraw.Draw(frame)
        left = 40 + index * 60
        frame_draw.rectangle((left, 80, left + 140, 220), fill="#3eb575")
        big_frames.append(frame)
    big_frames[0].save(
        big_gif_path,
        "GIF",
        save_all=True,
        append_images=big_frames[1:],
        duration=60,
        loop=0,
        disposal=2,
    )

    return {
        "png": png_path,
        "gif": gif_path,
        "pdf": pdf_path,
        "big_png": big_png_path,
        "big_gif": big_gif_path,
    }


def _minimal_pdf() -> bytes:
    """返回一个最小合法 PDF 的字节内容。"""
    content = "BT /F1 18 Tf 72 720 Td (WXBOX GUI smoke test) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = "%PDF-1.4\n"
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body.encode("latin-1")))
        body += f"{index} 0 obj\n{obj}\nendobj\n"
    xref_position = len(body.encode("latin-1"))
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n"
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF\n"
    )
    return body.encode("latin-1")
