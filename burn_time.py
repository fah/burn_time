#!/usr/bin/env python3
"""
burn_time.py — Burns the recording date of a photo visibly into the image
(like the old-fashioned date stamp of analog compact cameras).

Date source (in this order):
  1. EXIF metadata (DateTimeOriginal / DateTimeDigitized / DateTime)
  2. --date argument, if specified (manual fallback / override)
  3. File creation date from the operating system (macOS: st_birthtime)

Requires: Pillow (pip3 install Pillow)
Optional for HEIC/HEIF photos from the Photos app: pip3 install pillow-heif
"""

import argparse
import glob
import sys
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageColor, ImageOps
except ImportError:
    print(
        "Error: Pillow is not installed.\n"
        "Please install with:  pip3 install Pillow",
        file=sys.stderr,
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

MANUAL_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
]

# Preferred fonts on macOS (order = priority).
# A monospaced/technical font looks most like a "burned-in"
# camera timestamp.
DEFAULT_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
]

EPILOG_TEXT = """\
Examples:
  %(prog)s *.jpg
      Date/time in German standard format, top left, yellow.

  %(prog)s *.jpg --position tr --color red --format "yyyy/mm/dd hh:mm:ss"
      Top right, red text, format YYYY/MM/DD HH:MM:SS.

  %(prog)s IMG_1234.HEIC --date "24.12.2019 18:00:00"
      Enforces a date if EXIF is unavailable/unreliable.

  %(prog)s *.jpg --in-place --quality 90
      Overwrites the original files directly (Warning: not reversible).

Positions: tl (top left, default) · tr (top right) · bl (bottom left)
           br (bottom right) · c (center)

Format placeholders (case-insensitive, "mm" after "hh" = minutes):
  yyyy=Year(4)  yy=Year(2)  mm=Month  dd=Day  hh=Hour  mm=Minute  ss=Second
  Alternatively: classic strftime pattern with '%%', e.g. "%%d.%%m.%%Y %%H:%%M"
"""


# --------------------------------------------------------------------------
# Date determination
# --------------------------------------------------------------------------

def _parse_exif_datetime(value):
    value = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def read_exif_date(path: Path):
    """Reads DateTimeOriginal/DateTimeDigitized/DateTime from EXIF data."""
    try:
        with Image.open(path) as img:
            # Older, but very reliable API: returns a flat dict,
            # which for JPEGs typically also contains the Exif-SubIFD tags.
            exif_data = None
            try:
                exif_data = img._getexif()  # noqa: SLF001 (intentionally used)
            except Exception:
                exif_data = None

            if exif_data:
                for tag_id in (36867, 36868, 306):  # Original, Digitized, DateTime
                    val = exif_data.get(tag_id)
                    if val:
                        dt = _parse_exif_datetime(val)
                        if dt:
                            return dt

            # Modern API as fallback (e.g. PNG, TIFF, HEIC via plugin)
            exif = img.getexif()
            if exif:
                val = exif.get(306)
                if val:
                    dt = _parse_exif_datetime(val)
                    if dt:
                        return dt
                try:
                    exif_ifd = exif.get_ifd(0x8769)  # Exif-IFD pointer
                    for tag_id in (36867, 36868):
                        val = exif_ifd.get(tag_id)
                        if val:
                            dt = _parse_exif_datetime(val)
                            if dt:
                                return dt
                except Exception:
                    pass
    except Exception:
        pass
    return None


def get_file_creation_date(path: Path):
    """File creation date (macOS: st_birthtime, otherwise fallback to mtime)."""
    try:
        stat = path.stat()
        ts = getattr(stat, "st_birthtime", None)
        if ts is None:
            ts = stat.st_mtime
        return datetime.fromtimestamp(ts)
    except Exception:
        return None


def parse_manual_date(value: str) -> datetime:
    for fmt in MANUAL_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot read date '{value}'. Examples: "
        f"'2023-06-15 14:30:00' or '15.06.2023 14:30:00'"
    )


# --------------------------------------------------------------------------
# Formatting (simple placeholders -> strftime)
# --------------------------------------------------------------------------

def translate_format(fmt: str) -> str:
    """
    Translates a simple format like 'dd.mm.yyyy hh:mm:ss' into a
    strftime pattern. If the string already contains a '%', it is
    used unmodified as a strftime pattern (expert mode).

    'mm' is ambiguous (month vs. minute): everything BEFORE the first 'hh'
    counts as month, everything AFTER as minute. An explicitly capitalized
    'MM' is always interpreted as month.
    """
    if "%" in fmt:
        return fmt

    tokens = [
        ("yyyy", "%Y"),
        ("yy", "%y"),
        ("dd", "%d"),
        ("hh", "%H"),
        ("ss", "%S"),
    ]

    result = []
    i = 0
    seen_hour = False
    n = len(fmt)
    lower = fmt.lower()

    while i < n:
        if lower[i : i + 2] == "mm":
            orig = fmt[i : i + 2]
            if orig == "MM":
                result.append("%m")
            else:
                result.append("%M" if seen_hour else "%m")
            i += 2
            continue

        matched = False
        for token, repl in tokens:
            length = len(token)
            if lower[i : i + length] == token:
                result.append(repl)
                if token == "hh":
                    seen_hour = True
                i += length
                matched = True
                break

        if not matched:
            result.append(fmt[i])
            i += 1

    return "".join(result)


# --------------------------------------------------------------------------
# Image processing
# --------------------------------------------------------------------------

def load_font(font_path, size):
    candidates = [font_path] if font_path else DEFAULT_FONT_CANDIDATES
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def compute_position(pos: str, img_size, text_size, margin: int):
    W, H = img_size
    tw, th = text_size
    pos = pos.lower()
    if pos == "tl":
        return margin, margin
    if pos == "tr":
        return W - tw - margin, margin
    if pos == "bl":
        return margin, H - th - margin
    if pos == "br":
        return W - tw - margin, H - th - margin
    if pos in ("c", "center"):
        return (W - tw) // 2, (H - th) // 2
    return margin, margin


def burn_text_into_image(img: Image.Image, text: str, args) -> Image.Image:
    W, H = img.size
    font_size = args.font_size or max(16, int(H * 0.035))
    margin = args.margin if args.margin is not None else max(8, int(H * 0.02))

    font = load_font(args.font, font_size)

    rgb_img = img.convert("RGB")
    tmp_draw = ImageDraw.Draw(rgb_img)
    bbox = tmp_draw.textbbox((0, 0), text, font=font, stroke_width=1)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x, y = compute_position(args.position, (W, H), (tw, th), margin)
    # Compensate for font metrics (negative ascent offsets) so that the
    # visible text starts exactly at the target position.
    x -= bbox[0]
    y -= bbox[1]

    try:
        color_rgb = ImageColor.getrgb(args.color)
    except ValueError:
        print(f"⚠️  Unknown color '{args.color}', using 'yellow'.", file=sys.stderr)
        color_rgb = ImageColor.getrgb("yellow")
    color_rgba = color_rgb + (255,)

    base = rgb_img.convert("RGBA")

    # Soft, slightly offset shadow -> gives the text depth and
    # ensures readability on both light and dark backgrounds.
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    offset = max(2, font_size // 12)
    shadow_draw.text(
        (x + offset, y + offset), text, font=font,
        fill=(0, 0, 0, 190), stroke_width=1, stroke_fill=(0, 0, 0, 190),
    )
    blur_radius = max(1, font_size // 14)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # Sharp, colored text with dark outline -> "burned-in" look.
    text_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    stroke_w = max(1, font_size // 18)
    text_draw.text(
        (x, y), text, font=font,
        fill=color_rgba, stroke_width=stroke_w, stroke_fill=(0, 0, 0, 220),
    )

    combined = Image.alpha_composite(base, shadow_layer)
    combined = Image.alpha_composite(combined, text_layer)
    return combined.convert("RGB")


def determine_output_path(path: Path, args) -> Path:
    if args.in_place:
        return path
    out_dir = Path(args.output_dir) if args.output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{path.stem}{args.suffix}{path.suffix}"


def process_file(path: Path, args) -> bool:
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)  # apply correct orientation
    except Exception as e:
        print(f"⚠️  Could not open '{path.name}': {e}", file=sys.stderr)
        return False

    exif_bytes = img.info.get("exif")

    dt = read_exif_date(path)
    source = "EXIF"
    if dt is None:
        if args.date:
            dt = parse_manual_date(args.date)
            source = "Argument"
        else:
            dt = get_file_creation_date(path)
            source = "Filesystem"
    if dt is None:
        print(f"⚠️  No date found for '{path.name}' – skipped.", file=sys.stderr)
        return False

    strftime_pattern = translate_format(args.format)
    text = dt.strftime(strftime_pattern)

    out_path = determine_output_path(path, args)

    if args.dry_run:
        print(f"[dry-run] {path.name}: '{text}' (Source: {source}) -> {out_path.name} @ {args.position}")
        return True

    final_img = burn_text_into_image(img, text, args)

    save_kwargs = {}
    if out_path.suffix.lower() in (".jpg", ".jpeg"):
        save_kwargs["quality"] = args.quality
        save_kwargs["optimize"] = True
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes

    try:
        final_img.save(out_path, **save_kwargs)
    except Exception as e:
        print(f"⚠️  Could not save '{out_path.name}': {e}", file=sys.stderr)
        return False

    if args.verbose:
        print(f"✅ {path.name}: '{text}' (Source: {source}) -> {out_path}")
    else:
        print(f"✅ {path.name} -> {out_path.name}  ('{text}', Source: {source})")
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def expand_files(patterns):
    files = []
    for p in patterns:
        if any(ch in p for ch in "*?["):
            matches = sorted(glob.glob(p))
            if not matches:
                print(f"⚠️  No matches for pattern: {p}", file=sys.stderr)
            files.extend(matches)
        else:
            files.append(p)
    seen, result = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


def try_register_heif():
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="burn_time.py",
        description=(
            "Burns the recording date of a photo visibly into the image "
            "(like an old-fashioned camera date stamp)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG_TEXT,
    )
    parser.add_argument(
        "files", nargs="+",
        help="One or more image files or glob patterns (e.g. *.jpg)",
    )
    parser.add_argument(
        "--position", "-p", default="tl",
        choices=["tl", "tr", "bl", "br", "c", "center"],
        help="Position of the date stamp (default: tl = top left)",
    )
    parser.add_argument(
        "--color", "-c", default="yellow",
        help="Text color, e.g. 'yellow', 'red', '#FFAA00' (default: yellow)",
    )
    parser.add_argument(
        "--format", "-f", default="dd.mm.yyyy HH:mm:ss",
        help=(
            "Date format. Simple placeholders yyyy/mm/dd/hh/mm/ss OR "
            "strftime pattern with '%%'. Default: 'dd.mm.yyyy HH:mm:ss'"
        ),
    )
    parser.add_argument("--font", help="Path to a custom .ttf/.otf font file")
    parser.add_argument(
        "--font-size", type=int, default=None,
        help="Font size in pixels (default: automatic, ~3.5%% of image height)",
    )
    parser.add_argument(
        "--margin", type=int, default=None,
        help="Distance from image edge in pixels (default: automatic)",
    )
    parser.add_argument(
        "--date", default=None,
        help=(
            "Manual date as fallback or override if no EXIF date is available. "
            "Format: '2023-06-15 14:30:00' or '15.06.2023 14:30:00'"
        ),
    )
    parser.add_argument(
        "--output-dir", "-o", default=None,
        help="Output directory for processed images (default: same directory)",
    )
    parser.add_argument(
        "--suffix", default="_dated",
        help="Suffix for output filenames (default: '_dated'); ignored with --in-place",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Overwrite original file directly (Warning: not reversible!)",
    )
    parser.add_argument(
        "--quality", type=int, default=95,
        help="JPEG quality of output (default: 95)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only show what would happen, without writing files",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose output",
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.date:
        try:
            parse_manual_date(args.date)  # validation only
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))

    try_register_heif()

    paths = [Path(f) for f in expand_files(args.files)]
    if not paths:
        print("No files found.", file=sys.stderr)
        sys.exit(1)

    ok, fail = 0, 0
    for p in paths:
        if not p.exists():
            print(f"⚠️  File not found: {p}", file=sys.stderr)
            fail += 1
            continue
        if process_file(p, args):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} successful, {fail} failed.")
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
