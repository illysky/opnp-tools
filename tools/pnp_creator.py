#!/usr/bin/env python3
"""
pnp_creator.py  -  Altium PnP CSV → OpenPnP strip-feeder packer

Reads an Altium "Pick and Place Locations" CSV, generates inside a
  {board_name}/ output folder:
  - {board_name}.board.top.xml / board.bottom.xml  (OpenPnP placements)
  - {board_name}_labels.pdf                         (Dymo 11353 labels)
  - {board_name}_shopping.csv                       (shopping list)

To import parts/packages into OpenPnP use the
  Scripts → Config Parts from Board
Jython script inside OpenPnP.

Usage:
  pnp_creator.py <pnp.csv> [--boards N] [--attrition 0.10] [--min-strip 34]
                            [--print] [--printer NAME]
  pnp_creator.py --init-rules
  pnp_creator.py --install
"""

import argparse, csv, io, json, math, os, re, shutil
try:
    from PIL import Image, ImageDraw, ImageFont as _ImageFont
    import cairo as _cairo
    _LABELS_OK = True
except ImportError:
    _LABELS_OK = False
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).resolve().parent
TAPE_RULES   = SCRIPT_DIR / "tape_rules.json"
LCSC_PARTS_DB = SCRIPT_DIR / "lcsc_parts.csv"

# ---------------------------------------------------------------------------
# Machine configuration  —  single strip-feeder model
# ---------------------------------------------------------------------------

MACHINE_CONFIG = {
    "feeder_length_mm": 160,
    "feeder_widths":    {8: 12, 12: 16, 16: 20},  # tape_mm → machine footprint mm
    "y_delta":          4.025,             # EIA-481 sprocket hole spacing (mm)
    "min_strip_mm":     34,    # minimum tape length for C/R/L passives only
    "attrition":        0.10,
}

# ---------------------------------------------------------------------------
# Tape rules (default — written to tape_rules.json on first run)
# ---------------------------------------------------------------------------

DEFAULT_RULES = [
    # Skip rules FIRST — highest priority, prevent matching positive rules below
    {"comment": "QFP / QFN very large - skip", "match": r"QFP|LQFP",                              "tape_mm": 0, "pitch_mm": 0, "skip": True},
    {"comment": "nRF5340 / QFN-98 (16mm tape)","match": r"QFN-98|nRF5340",                        "tape_mm": 16, "pitch_mm": 12},
    {"comment": "Through-hole connectors",      "match": r"PinHeader|Molex|PicoBlade",             "tape_mm": 0, "pitch_mm": 0, "skip": True},
    # Specific ICs / connectors that are on tape
    {"comment": "WLCSP small ICs",              "match": r"WLCSP",                                "tape_mm": 8,  "pitch_mm": 4},
    {"comment": "DA7212 audio codec",           "match": r"DA7212",                               "tape_mm": 12, "pitch_mm": 8},
    {"comment": "HC-1.2 SMD connector",         "match": r"HC-1\.2",                              "tape_mm": 12, "pitch_mm": 8},
    {"comment": "USB Type-C SMD connector",     "match": r"TYPE-C",                               "tape_mm": 12, "pitch_mm": 8},
    {"comment": "Fiducials / test points", "match": r"FIDHOLE|FIDUCIAL|Fiducial|^TP1$|^TP-",      "tape_mm": 0, "pitch_mm": 0, "skip": True},
    {"comment": "Through-hole electrolytic","match": r"C_Elec|Inductor.*6\.3",                    "tape_mm": 0, "pitch_mm": 0, "skip": True},
    # 8 mm tape
    {"comment": "0201 passives",           "match": "0201",                                       "tape_mm": 8,  "pitch_mm": 2},
    {"comment": "0402 passives",           "match": "0402",                                       "tape_mm": 8,  "pitch_mm": 2},
    {"comment": "0603 passives / LEDs",    "match": "0603",                                       "tape_mm": 8,  "pitch_mm": 4},
    {"comment": "0805 passives",           "match": "0805",                                       "tape_mm": 8,  "pitch_mm": 4},
    {"comment": "1206 passives",           "match": "1206",                                       "tape_mm": 8,  "pitch_mm": 4},
    {"comment": "SOT-23 variants",         "match": r"SOT-[12345]\d\d?[A-Z]?$|SOT-23",           "tape_mm": 8,  "pitch_mm": 4},
    {"comment": "SOD-123 / SOD-323",       "match": r"SOD-[0-9]",                                "tape_mm": 8,  "pitch_mm": 4},
    {"comment": "Small DFN/SON (<=2x2mm)", "match": r"DFN1010|DFN2020|SON.*2x2",                 "tape_mm": 8,  "pitch_mm": 4},
    {"comment": "SMD 2-pin connectors",    "match": "20854",                                      "tape_mm": 12, "pitch_mm": 8},
    # 12 mm tape
    {"comment": "SOIC-8",                  "match": r"SOIC-8",                                    "tape_mm": 12, "pitch_mm": 8},
    {"comment": "SOT-223",                 "match": "SOT-223",                                    "tape_mm": 12, "pitch_mm": 8},
    {"comment": "WSON/DFN larger",         "match": r"WSON.*[3-9]x|DFN.*[3-9]x",                 "tape_mm": 12, "pitch_mm": 8},
    {"comment": "Crystal SMD 3225/2520",   "match": r"Crystal|XTAL_[0-9]",                       "tape_mm": 12, "pitch_mm": 8},
    {"comment": "QFN/VQFN small (<=32 pin)","match": r"QFN.*[123][0-9]P|VQFN.*[123][0-9]P",     "tape_mm": 12, "pitch_mm": 8},
    {"comment": "RGB LEDs larger package", "match": r"LTST|MHS110|R6GHB",                        "tape_mm": 12, "pitch_mm": 8},
    {"comment": "PCF85063 RTC TSSOP-8",    "match": "PCF85063",                                  "tape_mm": 12, "pitch_mm": 8},
    {"comment": "QFN-32/28 / VQFN medium", "match": r"QFN-3[0-9]|QFN-2[0-9]|VQFN-1[0-9]",      "tape_mm": 12, "pitch_mm": 8},
    # 16 mm tape — larger ICs (SOIC-16+, TSSOP-16+, QFN-48+)
    {"comment": "SOIC-16 and above",       "match": r"SOIC-1[6-9]|SOIC-[2-9]\d",                "tape_mm": 16, "pitch_mm": 12},
    {"comment": "TSSOP-16 and above",      "match": r"TSSOP-1[6-9]|TSSOP-[2-9]\d",              "tape_mm": 16, "pitch_mm": 12},
    {"comment": "QFN-48 and above",        "match": r"QFN-[4-9]\d",                              "tape_mm": 16, "pitch_mm": 12},
    {"comment": "SOP-8 wide body",         "match": r"SOP-8W|SOP8W|SOP.*Wide",                  "tape_mm": 16, "pitch_mm": 12},
]

# Passive designator prefix (C, R, L)
_PASSIVE_PREFIX = re.compile(r"^[CRL]\d", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Tape rule helpers
# ---------------------------------------------------------------------------

def init_rules(path: Path):
    path.write_text(json.dumps({"rules": DEFAULT_RULES}, indent=2))
    print(f"Wrote default tape rules to {path}")


def load_rules(path: Path):
    if not path.exists():
        init_rules(path)
    return json.loads(path.read_text())["rules"]


def lookup_tape(package_id: str, rules: list):
    """Return (tape_mm, pitch_mm, skip) for package_id.
    Falls back to 8mm / 4mm pitch if no rule matches."""
    for rule in rules:
        if re.search(rule["match"], package_id, re.IGNORECASE):
            return (rule.get("tape_mm", 0),
                    rule.get("pitch_mm", 4),
                    rule.get("skip", False))
    return (8, 4, False)   # default: 8mm tape


# ---------------------------------------------------------------------------
# Value normalisation helper
# ---------------------------------------------------------------------------

def _normalise_value(comment: str, des_prefix: str) -> str:
    """Capitalise SI prefix/unit chars that follow a digit, then ensure the
    appropriate unit suffix is present for C (F), L (H), R (R/Ω).
    DNP/DNF/DNS values are returned uppercased but never modified."""
    if not comment:
        return comment
    if re.match(r'^DN[PFS]', comment.strip(), re.IGNORECASE):
        return comment.strip().upper()

    c = comment.strip()

    # Capitalise SI prefix and unit chars that appear after a digit
    # e.g. 100nf → 100NF, 10k → 10K, 4r7 → 4R7
    def _cap(m):
        ch = m.group(1)
        return 'U' if ch.lower() in ('u', 'μ') else ch.upper()

    c = re.sub(r'(?<=\d)([pnuμmkrfhPNUMKRFH])', _cap, c)
    # Replace any remaining bare μ
    c = c.replace('μ', 'U').replace('Μ', 'U')

    des = (des_prefix or '').upper()
    cu  = c.upper()

    if des == 'C':
        if not cu.endswith('F'):
            c = c + 'F'
    elif des == 'L':
        if not cu.endswith('H'):
            c = c + 'H'
    elif des == 'R':
        # R present anywhere (handles 4R7 decimal-separator style)
        if not re.search(r'[RΩ]', cu):
            c = c + 'R'

    return c.upper()


# ---------------------------------------------------------------------------
# Altium CSV parser
# ---------------------------------------------------------------------------

def parse_altium_csv(path: Path):
    """Parse all non-DNP placements from an Altium PnP CSV.
    Returns a list of placement dicts; each has a 'layer' field ('top'/'bottom')."""
    text   = path.read_text(encoding="utf-8-sig", errors="replace")
    lines  = text.splitlines()

    header_idx = None
    for i, line in enumerate(lines):
        if "Designator" in line and "Footprint" in line:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"Could not find header row in {path}")

    data_lines = "\n".join(lines[header_idx:])
    reader     = csv.DictReader(io.StringIO(data_lines))

    def norm(s):
        return s.strip().strip('"').strip()

    placements = []
    for raw_row in reader:
        row        = {norm(k): norm(v) for k, v in raw_row.items() if k}
        designator = row.get("Designator", "")
        comment    = row.get("Value", "") or row.get("Comment", "")
        layer      = row.get("Layer", "")
        footprint  = row.get("Footprint", "")
        x_str      = row.get("Center-X(mm)", "0")
        y_str      = row.get("Center-Y(mm)", "0")
        rotation   = row.get("Rotation", "0")

        if not designator:
            continue
        # Drop test points
        if re.match(r'^TP\d', designator.strip(), re.IGNORECASE):
            continue
        # Drop Do-Not-Place/Fit/Solder components entirely
        fitted = row.get("Fitted", row.get("Mount", "")).lower()
        if fitted in ("not fitted", "n", "no", "false", "0", "dnp", "dnf"):
            continue
        if re.match(r'^DN[PFS]', comment.strip(), re.IGNORECASE):
            continue

        x = float(x_str.replace("mm", "").strip())
        y = float(y_str.replace("mm", "").strip())

        placements.append({
            "designator": designator,
            "comment":    comment,
            "layer":      "bottom" if "bottom" in layer.lower() else "top",
            "footprint":  footprint,
            "x":          x,
            "y":          y,
            "rotation":   float(rotation),
        })

    return placements


# ---------------------------------------------------------------------------
# CSV write-back (for cleaned BOM)
# ---------------------------------------------------------------------------

def _write_back_csv(csv_path: Path, placements: list):
    """Write cleaned comment/footprint values back into the original CSV.
    A .csv.bak backup is kept alongside."""
    backup = csv_path.with_suffix(".csv.bak")
    shutil.copy2(csv_path, backup)

    text   = csv_path.read_text(encoding="utf-8-sig", errors="replace")
    lines  = text.splitlines()

    header_idx = None
    for i, line in enumerate(lines):
        if "Designator" in line and "Footprint" in line:
            header_idx = i
            break
    if header_idx is None:
        return

    pl_map = {p["designator"]: p for p in placements}

    # Parse header to find column indices
    hdr_reader = csv.reader(io.StringIO(lines[header_idx]))
    headers    = [h.strip().strip('"') for h in next(hdr_reader)]
    des_col = next((i for i, h in enumerate(headers) if h == "Designator"), None)
    val_col = next((i for i, h in enumerate(headers) if h in ("Value", "Comment")), None)
    fp_col  = next((i for i, h in enumerate(headers) if h == "Footprint"), None)

    new_lines = list(lines[:header_idx + 1])  # pre-header rows + header
    data_text = "\n".join(lines[header_idx + 1:])

    for row in csv.reader(io.StringIO(data_text)):
        if not row:
            new_lines.append("")
            continue
        des = row[des_col].strip().strip('"') if des_col is not None and des_col < len(row) else ""
        if des in pl_map:
            p = pl_map[des]
            if val_col is not None and val_col < len(row):
                row[val_col] = p["comment"]
            if fp_col is not None and fp_col < len(row):
                row[fp_col] = p["footprint"]
        buf = io.StringIO()
        csv.writer(buf).writerow(row)
        new_lines.append(buf.getvalue().rstrip("\r\n"))

    csv_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"BOM      : cleaned CSV written back  (backup → {backup.name})")


# ---------------------------------------------------------------------------
# Interactive BOM cleanup
# ---------------------------------------------------------------------------

def interactive_bom_cleanup(placements: list, csv_path: Path) -> list:
    """Prompt for missing fields, normalise SI units, uppercase comment/footprint,
    then write the cleaned data back to the source CSV."""
    modified = False

    # Step A — prompt for missing data
    for p in placements:
        des = p["designator"]
        if not p["comment"]:
            resp = input(f"[MISSING] {des} — no value. Enter value (or Enter to skip): ").strip()
            if resp:
                p["comment"]  = resp
                modified = True
        if not p["footprint"]:
            resp = input(f"[MISSING] {des} — no footprint. Enter footprint (or Enter to skip): ").strip()
            if resp:
                p["footprint"] = resp
                modified = True

    # Step B — normalise and uppercase all placements
    for p in placements:
        des_prefix = p["designator"][0] if p["designator"] else ""
        orig_c = p["comment"]
        orig_f = p["footprint"]
        p["comment"]   = _normalise_value(p["comment"], des_prefix)
        p["footprint"] = p["footprint"].upper()
        if p["comment"] != orig_c or p["footprint"] != orig_f:
            modified = True

    # Step C — write back to CSV if anything changed
    if modified:
        _write_back_csv(csv_path, placements)

    return placements


# ---------------------------------------------------------------------------
# board.xml generator
# ---------------------------------------------------------------------------

def make_board_xml(placements: list, board_name: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<board xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        f' xsi:noNamespaceSchemaLocation="http://openpnp.org/xsd/board-1.0.xsd"'
        f' name="{board_name}">',
        "   <placements>",
    ]
    for p in placements:
        part_id = f"{p['comment'][:10]}-{p['footprint'][:10]}"
        lines.append(
            f'      <placement id="{p["designator"]}" type="Placement"'
            f' part-id="{part_id}" name="{p["designator"]}" enabled="true"'
            f' error-handling="Alert" comments="">'
        )
        lines.append(
            f'         <location units="Millimeters"'
            f' x="{p["x"]:.4f}" y="{p["y"]:.4f}" z="0.0" rotation="{p["rotation"]:.1f}"/>'
        )
        lines.append("      </placement>")
    lines += ["   </placements>", "</board>"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BOM builder
# ---------------------------------------------------------------------------

def build_bom_from_csv(placements, rules, n_boards, attrition, min_strip_mm):
    """
    Returns (packable, skipped, defaulted).

    packable  : list of dicts — {part_id, package_id, tape_mm, pitch_mm,
                                  qty_per_board, qty_required, qty_total,
                                  tape_length, designators}
    skipped   : list of (part_id, package_id) — matched skip=True rule
    defaulted : list of (part_id, package_id) — no rule matched; packed as 8mm
    """
    counts    = defaultdict(int)
    pkg_map   = {}
    desig_map = defaultdict(list)

    for p in placements:
        part_id  = f"{p['comment'][:10]}-{p['footprint'][:10]}"
        pkg_map[part_id]   = p["footprint"]
        desig_map[part_id].append(p["designator"])
        counts[part_id]   += 1

    packable, skipped, defaulted = [], [], []

    def _has_explicit_rule(pkg_id):
        for rule in rules:
            if re.search(rule["match"], pkg_id, re.IGNORECASE):
                return True
        return False

    for part_id in sorted(counts):
        qty_per_board = counts[part_id]
        package_id    = pkg_map[part_id]
        tape_mm, pitch_mm, skip = lookup_tape(package_id, rules)

        if skip or tape_mm == 0:
            skipped.append((part_id, package_id))
            continue

        if not _has_explicit_rule(package_id):
            defaulted.append((part_id, package_id))

        is_passive   = bool(_PASSIVE_PREFIX.match(desig_map[part_id][0]))
        att          = attrition if is_passive else 0.0
        qty_required = qty_per_board * n_boards
        qty_total    = math.ceil(qty_required * (1 + att))
        raw_length   = qty_total * pitch_mm
        tape_length  = max(raw_length, min_strip_mm) if is_passive else raw_length

        packable.append({
            "part_id":       part_id,
            "package_id":    package_id,
            "tape_mm":       tape_mm,
            "pitch_mm":      pitch_mm,
            "qty_per_board": qty_per_board,
            "qty_required":  qty_required,
            "qty_total":     qty_total,
            "tape_length":   tape_length,
            "designators":   desig_map[part_id],
        })

    return packable, skipped, defaulted


# ---------------------------------------------------------------------------
# Feeder packer — one component type per feeder
# ---------------------------------------------------------------------------

_LABEL_STUB_MM = 12.5   # tape reserved at the front of each passive feeder for a label


def _passive_cut_mm(chunk_mm: float) -> int:
    """Round passive cut length to the nearest 10mm."""
    raw = chunk_mm + _LABEL_STUB_MM
    return int(round(raw / 10.0) * 10)


class _Feeder:
    """One physical strip feeder (160 mm long). Holds exactly one tape section."""

    __slots__ = ("idx", "tape_mm", "part", "chunk_mm")

    def __init__(self, idx: int, tape_mm: int):
        self.idx      = idx
        self.tape_mm  = tape_mm
        self.part     = None   # part dict
        self.chunk_mm = 0.0    # component tape length (excluding label stub)

    def label(self) -> str:
        return f"{self.idx + 1:02d}"


def pack_components(packable, config):
    """
    One component type per feeder.  Passives (C/R/L) reserve _LABEL_STUB_MM at
    the front of each feeder for a label.  Components whose tape exceeds a
    single feeder's usable length are split across consecutive feeders.

    Returns (feeders, overflow).
    overflow is always empty — kept for API compatibility.
    """
    feeder_length = float(config["feeder_length_mm"])
    feeders       = []

    parts_sorted = sorted(packable, key=lambda p: p["tape_length"], reverse=True)

    for part in parts_sorted:
        tape_mm    = part["tape_mm"]
        is_passive = bool(_PASSIVE_PREFIX.match(
            part.get("designators", ["X"])[0]))

        usable    = feeder_length - (_LABEL_STUB_MM if is_passive else 0.0)
        remaining = part["tape_length"]

        while remaining > 0:
            chunk       = min(remaining, usable)
            fd          = _Feeder(len(feeders), tape_mm)
            fd.part     = part
            fd.chunk_mm = chunk
            feeders.append(fd)
            remaining  -= chunk

    return feeders, []




# ---------------------------------------------------------------------------
# Console cutting list
# ---------------------------------------------------------------------------

def print_cutting_list(feeders, overflow, skipped, unknown,
                       board_name, n_boards, attrition):
    print(f"\n{'='*78}")
    print(f"  TAPE CUTTING LIST — {board_name}  x{n_boards} boards")
    print(f"{'='*78}")
    print(f"  {'FEEDER':<8} {'PART ID':<38} {'QTY':>5}  {'PITCH':>6}  {'CUT':<10}")
    print(f"  {'-'*74}")

    for fd in feeders:
        part       = fd.part
        name       = fd.label()
        is_passive = bool(_PASSIVE_PREFIX.match(part.get("designators", ["X"])[0]))
        cut_mm     = _passive_cut_mm(fd.chunk_mm) if is_passive else int(fd.chunk_mm)
        qty_on_strip = int(cut_mm / part["pitch_mm"])
        print(f"  {name:<8} {part['part_id'][:38]:<38} {qty_on_strip:>5}"
              f"  {part['pitch_mm']:>4}mm  {cut_mm}mm")

    print(f"\n  Feeders used: {len(feeders)}")

    if overflow:
        print(f"\n  ** OVERFLOW — {len(overflow)} part(s) could not be packed:")
        for p in overflow:
            print(f"     {p['part_id']}  ({p['tape_length']:.0f}mm tape needed)")

    if skipped:
        print(f"\n  Hand-place: {len(skipped)} part(s)")
        for pid, pkg in skipped:
            print(f"     {pid}  [{pkg}]")

    if unknown:
        print(f"\n  Defaulted to 8mm ({len(unknown)} part(s)) — consider adding tape_rules.json entries:")
        for pid, pkg in unknown:
            print(f"     {pid}  [{pkg}]")

    print(f"{'='*78}\n")


# ---------------------------------------------------------------------------
# Component labels PDF  (PIL + pycairo)
# ---------------------------------------------------------------------------

# Label: exactly 1 inch × 1 inch printable area
# At 300 DPI: 300 × 300 px image → 72 × 72 pt PDF page
_LBL_HALF_W  = 300    # px
_LBL_HALF_H  = 300    # px
_LBL_FULL_H  = 300    # px
_LBL_PAGE_W  = 72.0   # pt  (1 inch)
_LBL_PAGE_H  = 72.0   # pt  (1 inch)
_LBL_MEDIA   = "custom_25.4x25.4mm_25.4x25.4mm"

_LBL_SLOT_W  = _LBL_HALF_W // 2   # 150px — width of each left/right label slot
_LBL_FONT_SZ = 24                  # px


def _lbl_font():
    try:
        return _ImageFont.truetype("DejaVuSansMono-Bold.ttf", _LBL_FONT_SZ)
    except Exception:
        return _ImageFont.load_default()


def _make_label_page(left: tuple | None, right: tuple | None):
    """
    Render one Dymo 11353 page (2-in-1) with labels side by side.
    left / right are (line1, line2, line3) strings, or None for blank.
    """
    font  = _lbl_font()
    label = Image.new("RGB", (_LBL_HALF_W, _LBL_HALF_H), "white")
    draw  = ImageDraw.Draw(label)

    draw.line([(_LBL_SLOT_W, 0), (_LBL_SLOT_W, _LBL_HALF_H)],
              fill=(180, 180, 180), width=1)

    line_h = _LBL_FONT_SZ + 4

    for entry, x0 in [(left, 20), (right, _LBL_SLOT_W + 20)]:
        if entry is None:
            continue
        line1, line2, line3 = entry
        draw.text((x0, 25),            line1, font=font, fill="black")
        draw.text((x0, 25 + line_h),   line2, font=font, fill="black")
        draw.text((x0, 25 + line_h*2), line3, font=font, fill="black")

    return label.rotate(270, expand=True)


def generate_component_labels_pdf(feeders: list, out_path: Path) -> Path | None:
    """
    Generate a Dymo 11353 labels PDF.  Two labels per PDF page.
    Labels appear in feeder order (grouped by tape_mm, then feeder index).
    """
    if not _LABELS_OK:
        print("WARNING: PIL or pycairo not available — skipping component labels PDF")
        return None

    by_tape: dict[int, list] = defaultdict(list)
    for fd in feeders:
        by_tape[fd.tape_mm].append(fd)

    entries = []
    for tape_mm in sorted(by_tape.keys()):
        for fd in by_tape[tape_mm]:
            entries.append((fd.label(), fd.part, fd.chunk_mm))

    if not entries:
        return None

    surface = _cairo.PDFSurface(str(out_path), _LBL_PAGE_W, _LBL_PAGE_H)
    ctx     = _cairo.Context(surface)
    sx = sy = _LBL_PAGE_W / _LBL_HALF_W

    def _entry(feeder_name, part, chunk_mm):
        pkg        = part["package_id"]
        pid        = part["part_id"]
        # part_id is comment[:10]-footprint[:10]; value is the first segment
        value      = pid.split("-")[0] if "-" in pid else pid
        is_passive = bool(_PASSIVE_PREFIX.match(part.get("designators", ["X"])[0]))
        cut_mm     = _passive_cut_mm(chunk_mm) if is_passive else int(chunk_mm)
        line1 = value[:8]
        line2 = pkg[:8]
        qty_on_strip = int(cut_mm / part["pitch_mm"])
        line3 = f"{qty_on_strip}|{cut_mm}mm"[:8]
        return (line1, line2, line3)

    pairs = list(zip(entries[0::2], entries[1::2]))
    if len(entries) % 2:
        pairs.append((entries[-1], None))

    for (fn_a, pa, ch_a), pair_b in pairs:
        left  = _entry(fn_a, pa, ch_a)
        right = _entry(*pair_b) if pair_b else None
        img   = _make_label_page(left, right)
        buf   = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        img_surf = _cairo.ImageSurface.create_from_png(buf)
        ctx.save()
        ctx.scale(sx, sy)
        ctx.set_source_surface(img_surf, 0, 0)
        ctx.paint()
        ctx.restore()
        surface.show_page()

    surface.finish()
    print(f"Labels   : {out_path}  ({len(entries)} labels)")
    return out_path


def _print_labels(labels_path: Path, printer: str):
    """Send the component labels PDF to the Dymo via CUPS."""
    import subprocess
    print(f"Printing : {labels_path.name} → {printer}")
    try:
        subprocess.run(
            [
                "lp",
                "-d", printer,
                "-o", f"media={_LBL_MEDIA}",
                "-o", "orientation-requested=4",
                "-o", "page-left=0",
                "-o", "page-right=0",
                "-o", "page-top=0",
                "-o", "page-bottom=0",
                str(labels_path),
            ],
            check=True,
        )
        print("Print job submitted.")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: print failed — {e}")


# ---------------------------------------------------------------------------
# LCSC parts database — lazy-loaded, indexed by lowercase package name
# ---------------------------------------------------------------------------

_lcsc_index: dict | None = None   # None = not loaded yet; {} = loaded but empty/missing


def _load_lcsc_db() -> dict:
    global _lcsc_index
    if _lcsc_index is not None:
        return _lcsc_index

    if not LCSC_PARTS_DB.exists():
        print(f"\nLCSC parts DB not found at: {LCSC_PARTS_DB}")
        print(f"Download from: https://yaqwsx.github.io/jlcpcb-cache/")
        print(f"               (click 'Download CSV')")
        print(f"Place as     : {LCSC_PARTS_DB}\n")
        _lcsc_index = {}
        return _lcsc_index

    size_mb = LCSC_PARTS_DB.stat().st_size // (1024 * 1024)
    print(f"Loading LCSC parts DB ({size_mb} MB)...", end="", flush=True)

    index: dict[str, list] = defaultdict(list)
    try:
        with open(LCSC_PARTS_DB, encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                _lcsc_index = {}
                return _lcsc_index

            fl = {h.lower().strip(): h for h in reader.fieldnames}

            lcsc_col  = next((h for k, h in fl.items() if "lcsc" in k), None)
            pkg_col   = next((h for k, h in fl.items() if "package" in k or "footprint" in k), None)
            desc_col  = next((h for k, h in fl.items() if "description" in k or "desc" in k), None)
            stock_col = next((h for k, h in fl.items() if "stock" in k), None)

            if not (lcsc_col and pkg_col):
                print(" WARNING: could not identify LCSC/Package columns")
                _lcsc_index = {}
                return _lcsc_index

            for row in reader:
                pkg = row.get(pkg_col, "").strip().lower()
                if not pkg:
                    continue
                try:
                    stock = int(row.get(stock_col, "0").replace(",", ""))
                except (ValueError, AttributeError):
                    stock = 0
                index[pkg].append({
                    "lcsc":  row.get(lcsc_col, "").strip(),
                    "desc":  row.get(desc_col, "").strip() if desc_col else "",
                    "stock": stock,
                })
    except Exception as e:
        print(f" ERROR: {e}")
        _lcsc_index = {}
        return _lcsc_index

    total = sum(len(v) for v in index.values())
    print(f" {total:,} parts indexed across {len(index)} packages")
    _lcsc_index = dict(index)
    return _lcsc_index


def _lcsc_suggest(value: str, footprint: str) -> list:
    """Return up to 3 (lcsc_num, description) tuples for a C/R component."""
    db = _load_lcsc_db()
    if not db:
        return []

    fp_lower  = footprint.lower()
    # Strip unit suffix for matching (e.g. '100NF' → '100N', '10KR' → '10K')
    val_clean = re.sub(r'[RΩFH]$', '', value.upper()).lower()

    # Exact package match first, then partial
    rows = db.get(fp_lower, [])
    if not rows:
        for key in db:
            if fp_lower in key or key in fp_lower:
                rows = db[key]
                break

    if not rows:
        return []

    scored = []
    for r in rows:
        desc_lower = r["desc"].lower()
        score = 0
        if val_clean and val_clean in desc_lower:
            score = 3
        elif val_clean and len(val_clean) >= 3 and val_clean[:3] in desc_lower:
            score = 1
        if score > 0:
            scored.append((score, r["stock"], r["lcsc"], r["desc"]))

    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [(s[2], s[3][:80]) for s in scored[:3]]


# ---------------------------------------------------------------------------
# Shopping list generator
# ---------------------------------------------------------------------------

def generate_shopping_list(packable: list, skipped: list, placements: list,
                           n_boards: int, out_path: Path):
    """Write a shopping list CSV.  C/R components get up to 3 LCSC suggestions."""
    # Build raw count for skipped parts (all layers in placements list)
    counts: dict[str, int] = defaultdict(int)
    for p in placements:
        pid = f"{p['comment'][:10]}-{p['footprint'][:10]}"
        counts[pid] += 1

    rows = []

    for part in packable:
        pid        = part["part_id"]
        pkg        = part["package_id"]
        # part_id is comment[:10]-footprint[:10]; value is the first segment
        value      = pid.split("-")[0] if "-" in pid else pid
        qty        = part["qty_total"]
        des_prefix = part["designators"][0][0].upper() if part["designators"] else ""

        suggestions = []
        if des_prefix in ("C", "R"):
            suggestions = _lcsc_suggest(value, pkg)

        row = [pid, value, pkg, qty]
        for i in range(3):
            if i < len(suggestions):
                row += [suggestions[i][0], suggestions[i][1]]
            else:
                row += ["", ""]
        rows.append(row)

    for pid, pkg in skipped:
        qty = math.ceil(counts.get(pid, 0) * n_boards)
        value = pid.split("-")[0] if "-" in pid else pid
        rows.append([pid, value, pkg, qty, "(hand place)", "", "", "", "", ""])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Part ID", "Value", "Footprint", "Qty Needed",
                    "LCSC#1", "Desc#1", "LCSC#2", "Desc#2", "LCSC#3", "Desc#3"])
        w.writerows(rows)

    lcsc_count = sum(1 for r in rows if r[4] and r[4] != "(hand place)")
    print(f"Shopping : {out_path}  ({len(rows)} parts, {lcsc_count} with LCSC suggestions)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Altium PnP CSV → OpenPnP strip-feeder packer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("pnp_csv",       nargs="?",  help="Path to Altium PnP CSV file")
    parser.add_argument("--boards",      type=int,   default=1,    help="Number of boards")
    parser.add_argument("--attrition",   type=float, default=None,  help="Pick-loss fraction e.g. 0.10")
    parser.add_argument("--min-strip",   type=float, default=None,  help="Minimum tape strip length (mm)")
    parser.add_argument("--init-rules",  action="store_true",       help="Write/reset tape_rules.json and exit")
    parser.add_argument("--print",       action="store_true",       help="Print component labels to Dymo after generating")
    parser.add_argument("--printer",     type=str, default="DYMO_LabelWriter_4XL",
                                                                    help="CUPS printer name (default: DYMO_LabelWriter_4XL)")
    parser.add_argument("--install",     action="store_true",
                                                                    help="Symlink this script to /usr/local/bin/pnp_creator")
    args = parser.parse_args()

    # --install: create a symlink on PATH
    if args.install:
        script  = Path(__file__).resolve()
        target  = Path("/usr/local/bin/pnp_creator")
        try:
            if target.is_symlink() or target.exists():
                target.unlink()
            target.symlink_to(script)
            script.chmod(0o755)
            print(f"Installed: {target} → {script}")
        except PermissionError:
            print(f"ERROR: permission denied — try:  sudo python3 {script} --install")
        return

    cfg         = {**MACHINE_CONFIG}
    attrition   = args.attrition if args.attrition is not None else cfg["attrition"]
    min_strip   = args.min_strip  if args.min_strip  is not None else cfg["min_strip_mm"]

    if args.init_rules:
        init_rules(TAPE_RULES)
        return

    if not args.pnp_csv:
        parser.print_help()
        return

    csv_path = Path(args.pnp_csv).expanduser().resolve()
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}")
        return

    print(f"CSV      : {csv_path.name}")
    print(f"Boards   : {args.boards}")
    print(f"Attrition: {int(attrition*100)}% (passives only)")
    print(f"Min strip: {min_strip}mm (passives, +{_LABEL_STUB_MM}mm label stub per feeder)")

    # Parse CSV (all layers, DNP already filtered)
    all_placements = parse_altium_csv(csv_path)

    # Interactive BOM cleanup (normalise values, prompt for missing fields)
    all_placements = interactive_bom_cleanup(all_placements, csv_path)

    top_placements    = [p for p in all_placements if p["layer"] == "top"]
    bottom_placements = [p for p in all_placements if p["layer"] == "bottom"]
    print(f"Top      : {len(top_placements)} placements")
    if bottom_placements:
        print(f"Bottom   : {len(bottom_placements)} placements  "
              f"({', '.join(p['designator'] for p in bottom_placements)})")

    # Output folder — all generated files go here (except OpenPnP configs)
    board_name = csv_path.stem.replace("-pnp", "").replace("_pnp", "")
    out_dir    = csv_path.parent / board_name
    out_dir.mkdir(exist_ok=True)
    print(f"Out dir  : {out_dir}")

    # board.top.xml + board.bottom.xml
    (out_dir / f"{board_name}.board.top.xml").write_text(
        make_board_xml(top_placements, board_name))
    print(f"board.xml: {board_name}.board.top.xml ({len(top_placements)} placements)")
    if bottom_placements:
        (out_dir / f"{board_name}.board.bottom.xml").write_text(
            make_board_xml(bottom_placements, board_name))
        print(f"board.xml: {board_name}.board.bottom.xml ({len(bottom_placements)} placements)")

    # BOM, packing, labels and shopping are top-side only
    placements = top_placements

    # BOM
    rules = load_rules(TAPE_RULES)
    packable, skipped, unknown = build_bom_from_csv(
        placements, rules, args.boards, attrition, min_strip
    )

    # Pack
    feeders, overflow = pack_components(packable, cfg)

    # Console cutting list
    print_cutting_list(feeders, overflow, skipped, unknown,
                       csv_path.name, args.boards, attrition)

    # Component labels PDF (Dymo 11353)
    labels_path = generate_component_labels_pdf(
        feeders, out_dir / f"{board_name}_labels.pdf"
    )

    if args.print and labels_path:
        _print_labels(labels_path, args.printer)

    # Shopping list
    generate_shopping_list(
        packable, skipped, placements,
        args.boards,
        out_dir / f"{board_name}_shopping.csv",
    )

    print("\nDone.\n")


if __name__ == "__main__":
    main()
