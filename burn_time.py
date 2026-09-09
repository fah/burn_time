#!/usr/bin/env python3
"""
burn_time.py — Brennt das Aufnahmedatum eines Fotos sichtbar in das Bild ein
(wie der altmodische Datumsstempel analoger Kompaktkameras).

Datumsquelle (in dieser Reihenfolge):
  1. EXIF-Metadaten (DateTimeOriginal / DateTimeDigitized / DateTime)
  2. --date Argument, falls angegeben (manueller Fallback / Override)
  3. Dateierstellungsdatum des Betriebssystems (macOS: st_birthtime)

Benötigt: Pillow (pip3 install Pillow)
Optional für HEIC/HEIF-Fotos aus der Fotos-App: pip3 install pillow-heif
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
        "Fehler: Pillow ist nicht installiert.\n"
        "Bitte installieren mit:  pip3 install Pillow",
        file=sys.stderr,
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Konstanten
# --------------------------------------------------------------------------

MANUAL_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
]

# Bevorzugte Schriften auf macOS (Reihenfolge = Priorität).
# Eine monospaced/technische Schrift wirkt am ehesten wie ein "eingebrannter"
# Kamera-Zeitstempel.
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
Beispiele:
  %(prog)s *.jpg
      Datum/Uhrzeit im deutschen Standardformat, oben links, gelb.

  %(prog)s *.jpg --position tr --color red --format "yyyy/mm/dd hh:mm:ss"
      Oben rechts, rote Schrift, Format JJJJ/MM/TT HH:MM:SS.

  %(prog)s IMG_1234.HEIC --date "24.12.2019 18:00:00"
      Erzwingt ein Datum, falls EXIF nicht verfügbar/vertrauenswürdig ist.

  %(prog)s *.jpg --in-place --quality 90
      Überschreibt die Originaldateien direkt (Vorsicht: nicht umkehrbar).

Positionen: tl (oben links, Standard) · tr (oben rechts) · bl (unten links)
            br (unten rechts) · c (zentriert)

Format-Platzhalter (Groß-/Kleinschreibung egal, "mm" nach "hh" = Minuten):
  yyyy=Jahr(4)  yy=Jahr(2)  mm=Monat  dd=Tag  hh=Stunde  mm=Minute  ss=Sekunde
  Alternativ: klassisches strftime-Muster mit '%%', z.B. "%%d.%%m.%%Y %%H:%%M"
"""


# --------------------------------------------------------------------------
# Datumsermittlung
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
    """Liest DateTimeOriginal/DateTimeDigitized/DateTime aus den EXIF-Daten."""
    try:
        with Image.open(path) as img:
            # Ältere, aber sehr zuverlässige API: liefert ein flaches Dict,
            # das bei JPEGs i.d.R. auch die Exif-SubIFD-Tags enthält.
            exif_data = None
            try:
                exif_data = img._getexif()  # noqa: SLF001 (bewusst genutzt)
            except Exception:
                exif_data = None

            if exif_data:
                for tag_id in (36867, 36868, 306):  # Original, Digitized, DateTime
                    val = exif_data.get(tag_id)
                    if val:
                        dt = _parse_exif_datetime(val)
                        if dt:
                            return dt

            # Moderne API als Fallback (z.B. PNG, TIFF, HEIC via Plugin)
            exif = img.getexif()
            if exif:
                val = exif.get(306)
                if val:
                    dt = _parse_exif_datetime(val)
                    if dt:
                        return dt
                try:
                    exif_ifd = exif.get_ifd(0x8769)  # Exif-IFD-Pointer
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
    """Dateierstellungsdatum (macOS: st_birthtime, sonst Fallback auf mtime)."""
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
        f"Kann Datum '{value}' nicht lesen. Beispiele: "
        f"'2023-06-15 14:30:00' oder '15.06.2023 14:30:00'"
    )


# --------------------------------------------------------------------------
# Formatierung (einfache Platzhalter -> strftime)
# --------------------------------------------------------------------------

def translate_format(fmt: str) -> str:
    """
    Übersetzt ein einfaches Format wie 'dd.mm.yyyy hh:mm:ss' in ein
    strftime-Muster. Enthält der String bereits ein '%', wird er
    unverändert als strftime-Muster verwendet (Profi-Modus).

    'mm' ist zweideutig (Monat vs. Minute): alles VOR dem ersten 'hh'
    gilt als Monat, alles DANACH als Minute. Ein explizit großgeschriebenes
    'MM' wird immer als Monat interpretiert.
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
# Bildbearbeitung
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
    # Font-Metriken (negative Ascent-Offsets) ausgleichen, damit der
    # sichtbare Text exakt an der Zielposition beginnt.
    x -= bbox[0]
    y -= bbox[1]

    try:
        color_rgb = ImageColor.getrgb(args.color)
    except ValueError:
        print(f"⚠️  Unbekannte Farbe '{args.color}', verwende 'yellow'.", file=sys.stderr)
        color_rgb = ImageColor.getrgb("yellow")
    color_rgba = color_rgb + (255,)

    base = rgb_img.convert("RGBA")

    # Weicher, leicht verschobener Schatten -> gibt dem Text Tiefe und
    # sorgt für Lesbarkeit auf hellem wie dunklem Untergrund.
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    offset = max(2, font_size // 12)
    shadow_draw.text(
        (x + offset, y + offset), text, font=font,
        fill=(0, 0, 0, 190), stroke_width=1, stroke_fill=(0, 0, 0, 190),
    )
    blur_radius = max(1, font_size // 14)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # Scharfer, farbiger Text mit dunkler Kontur -> "eingebrannter" Look.
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
        img = ImageOps.exif_transpose(img)  # korrekte Ausrichtung anwenden
    except Exception as e:
        print(f"⚠️  Konnte '{path.name}' nicht öffnen: {e}", file=sys.stderr)
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
            source = "Dateisystem"
    if dt is None:
        print(f"⚠️  Kein Datum für '{path.name}' ermittelbar – übersprungen.", file=sys.stderr)
        return False

    strftime_pattern = translate_format(args.format)
    text = dt.strftime(strftime_pattern)

    out_path = determine_output_path(path, args)

    if args.dry_run:
        print(f"[dry-run] {path.name}: '{text}' (Quelle: {source}) -> {out_path.name} @ {args.position}")
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
        print(f"⚠️  Konnte '{out_path.name}' nicht speichern: {e}", file=sys.stderr)
        return False

    if args.verbose:
        print(f"✅ {path.name}: '{text}' (Quelle: {source}) -> {out_path}")
    else:
        print(f"✅ {path.name} -> {out_path.name}  ('{text}', Quelle: {source})")
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
                print(f"⚠️  Kein Treffer für Muster: {p}", file=sys.stderr)
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
            "Brennt das Aufnahmedatum eines Fotos sichtbar in das Bild ein "
            "(wie ein altmodischer Kamera-Datumsstempel)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG_TEXT,
    )
    parser.add_argument(
        "files", nargs="+",
        help="Ein oder mehrere Bilddateien bzw. Glob-Muster (z.B. *.jpg)",
    )
    parser.add_argument(
        "--position", "-p", default="tl",
        choices=["tl", "tr", "bl", "br", "c", "center"],
        help="Position des Datumsstempels (Standard: tl = oben links)",
    )
    parser.add_argument(
        "--color", "-c", default="yellow",
        help="Textfarbe, z.B. 'yellow', 'red', '#FFAA00' (Standard: yellow)",
    )
    parser.add_argument(
        "--format", "-f", default="dd.mm.yyyy HH:mm:ss",
        help=(
            "Datumsformat. Einfache Platzhalter yyyy/mm/dd/hh/mm/ss ODER "
            "strftime-Muster mit '%%'. Standard: 'dd.mm.yyyy HH:mm:ss'"
        ),
    )
    parser.add_argument("--font", help="Pfad zu einer eigenen .ttf/.otf Schriftdatei")
    parser.add_argument(
        "--font-size", type=int, default=None,
        help="Schriftgröße in Pixel (Standard: automatisch, ~3.5%% der Bildhöhe)",
    )
    parser.add_argument(
        "--margin", type=int, default=None,
        help="Abstand vom Bildrand in Pixel (Standard: automatisch)",
    )
    parser.add_argument(
        "--date", default=None,
        help=(
            "Manuelles Datum als Fallback bzw. Override, falls kein EXIF-Datum "
            "vorhanden ist. Format: '2023-06-15 14:30:00' oder '15.06.2023 14:30:00'"
        ),
    )
    parser.add_argument(
        "--output-dir", "-o", default=None,
        help="Zielverzeichnis für bearbeitete Bilder (Standard: gleiches Verzeichnis)",
    )
    parser.add_argument(
        "--suffix", default="_dated",
        help="Suffix für Ausgabedateinamen (Standard: '_dated'); ignoriert bei --in-place",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Originaldatei direkt überschreiben (Achtung: nicht umkehrbar!)",
    )
    parser.add_argument(
        "--quality", type=int, default=95,
        help="JPEG-Qualität der Ausgabe (Standard: 95)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Nur anzeigen, was passieren würde, ohne Dateien zu schreiben",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Ausführliche Ausgabe",
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.date:
        try:
            parse_manual_date(args.date)  # nur zur Validierung
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))

    try_register_heif()

    paths = [Path(f) for f in expand_files(args.files)]
    if not paths:
        print("Keine Dateien gefunden.", file=sys.stderr)
        sys.exit(1)

    ok, fail = 0, 0
    for p in paths:
        if not p.exists():
            print(f"⚠️  Datei nicht gefunden: {p}", file=sys.stderr)
            fail += 1
            continue
        if process_file(p, args):
            ok += 1
        else:
            fail += 1

    print(f"\nFertig: {ok} erfolgreich, {fail} fehlgeschlagen.")
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()