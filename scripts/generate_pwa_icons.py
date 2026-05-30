"""Generate PWA icons (192 + 512) for Achint ERP. Run once: venv\\Scripts\\python scripts\\generate_pwa_icons.py"""
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError as exc:
    raise SystemExit("Install Pillow first: venv\\Scripts\\pip install Pillow") from exc

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
PRIMARY = (11, 44, 95)  # #0b2c5f
ACCENT = (217, 119, 6)  # #d97706
WHITE = (255, 255, 255)


def draw_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), PRIMARY)
    draw = ImageDraw.Draw(img)
    margin = size // 8
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=size // 10,
        fill=ACCENT,
    )
    inner = size // 4
    draw.rounded_rectangle(
        [inner, inner, size - inner, size - inner],
        radius=size // 16,
        fill=PRIMARY,
    )
    font_size = size // 2
    text = "A"
    try:
        from PIL import ImageFont

        font = ImageFont.truetype("arialbd.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - size * 0.04), text, fill=WHITE, font=font)
    return img


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    for dim in (192, 512):
        path = PUBLIC / f"icon-{dim}.png"
        draw_icon(dim).save(path, format="PNG")
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
