import csv
import re
import io
import subprocess
import argparse
from PIL import Image, ImageDraw, ImageFont
import cairo


# ============================================================================
# PART 1: EXTRACT CAPACITORS AND RESISTORS
# ============================================================================

def _str(v):
    """Return v as a clean string, or 'N/A' if empty/None."""
    s = str(v).strip() if v is not None else ''
    return s if s and s.lower() not in ('nan', 'none', '') else 'N/A'


def parse_capacitor_value(description):
    """Extract capacitor value from description."""
    desc = description.lower()
    patterns = [
        r'(\d+\.?\d*)\s*uf',
        r'(\d+\.?\d*)\s*nf',
        r'(\d+\.?\d*)\s*pf',
    ]
    for pattern in patterns:
        match = re.search(pattern, desc)
        if match:
            value = match.group(1)
            unit  = re.search(r'[upn]f', desc[match.start():match.end()+2]).group(0)
            unit  = unit[0].lower() + unit[1].upper()
            return f"{value}{unit}"
    return "N/A"


def parse_resistor_value(description):
    """Extract resistor value from description."""
    patterns = [
        r'(\d+\.?\d*)\s*[MΩ]Ω',
        r'(\d+\.?\d*)\s*kΩ',
        r'(\d+\.?\d*)\s*Ω',
    ]
    for pattern in patterns:
        match = re.search(pattern, description)
        if match:
            return description[match.start():match.end()].replace('ΩΩ', 'Ω')
    return "N/A"


def parse_voltage(description):
    """Extract voltage rating from description."""
    match = re.search(r'(\d+)\s*V\s', description)
    return f"{match.group(1)}V" if match else "N/A"


def normalize_package(package):
    """Convert package to standard 0402/0603/0805, or None if not matched."""
    pkg = str(package)
    if '402' in pkg:
        return '0402'
    if '603' in pkg:
        return '0603'
    if '805' in pkg:
        return '0805'
    return None


def extract_components(input_csv):
    """Extract capacitors and resistors from BOM CSV. Returns list of dicts."""
    results = []
    with open(input_csv, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            description = row.get('Description', '')
            desc_lower  = description.lower()

            is_capacitor = 'capacitor' in desc_lower or 'mlcc' in desc_lower
            is_resistor  = 'resistor'  in desc_lower

            if not (is_capacitor or is_resistor):
                continue

            package = normalize_package(row.get('Package', ''))
            if not package:
                continue

            comp_type = 'C' if is_capacitor else 'R'
            value     = parse_capacitor_value(description) if is_capacitor else parse_resistor_value(description)
            voltage   = parse_voltage(description)

            results.append({
                'Type':      comp_type,
                'Value':     value,
                'Voltage':   voltage,
                'Footprint': package,
            })
    return results


def save_components_csv(rows, output_csv):
    """Write extracted component rows to CSV."""
    fieldnames = ['Type', 'Value', 'Voltage', 'Footprint']
    with open(output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_components_csv(input_csv):
    """Load a pre-formatted Type,Value,Voltage,Footprint CSV. Returns list of dicts."""
    with open(input_csv, newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def print_components_table(rows):
    """Print a simple table of extracted components."""
    print(f"  {'Type':<6} {'Value':<14} {'Voltage':<8} Footprint")
    print(f"  {'-'*50}")
    for row in rows:
        print(f"  {row['Type']:<6} {row['Value']:<14} {row.get('Voltage','N/A'):<8} {row.get('Footprint','N/A')}")


# ============================================================================
# PART 2: GENERATE LABELS TO PDF
# ============================================================================

# Dymo 11353: 25mm wide × 13mm tall per label (2-in-1 strip = 25mm × 26mm).
# Rendered at 300 DPI: 25mm → 295px, 13mm → 154px, 26mm → 308px.
INDIVIDUAL_SIZE = (304, 178)
LABEL_SIZE      = (INDIVIDUAL_SIZE[0], INDIVIDUAL_SIZE[1] * 2)

# Physical PDF page size in points — matches printer media: 25.74mm × 30.14mm
_PAGE_W_PT = 25.74 / 25.4 * 72   # ≈ 72.96 pt
_PAGE_H_PT = 30.14 / 25.4 * 72   # ≈ 85.37 pt
_LBL_MEDIA = "custom_25.74x30.14mm_25.74x30.14mm"


def _load_fonts():
    """Load DejaVu mono fonts with fallback."""
    try:
        return (
            ImageFont.truetype("DejaVuSansMono.ttf",      70),
            ImageFont.truetype("DejaVuSansMono-Bold.ttf", 40),
            ImageFont.truetype("DejaVuSansMono-Bold.ttf", 26),
            ImageFont.truetype("DejaVuSansMono.ttf",      26),
        )
    except Exception:
        print("Warning: DejaVuSansMono font not found, using default")
        f = ImageFont.load_default()
        return f, f, f, f


def create_component_label(comp_type, value, voltage, footprint):
    """Create a Dymo 11353 label image (295×308 px) with info repeated in each half."""
    font_type, font_value, font_value_small, font_specs = _load_fonts()

    section = Image.new("RGB", INDIVIDUAL_SIZE, "white")
    draw    = ImageDraw.Draw(section)

    # Large component-type letter — top-right
    draw.text((240, -10), comp_type, font=font_type, fill="black")

    # Value — top-left (smaller font for S-type part numbers)
    if comp_type == 'S':
        draw.text((2, 12), value, font=font_value_small, fill="black")
    else:
        draw.text((2, 8),  value, font=font_value,       fill="black")

    # Specs: footprint only for silicon; footprint + voltage for passives
    specs = footprint if comp_type == 'S' else f"{footprint} {voltage}"
    draw.text((2, 100), specs, font=font_specs, fill="black")

    # Stack two identical halves into the full 2-in-1 strip
    label = Image.new("RGB", LABEL_SIZE, "white")
    label.paste(section, (0, 0))
    label.paste(section, (0, INDIVIDUAL_SIZE[1]))
    return label


def generate_labels_pdf(rows, output_pdf="labels.pdf"):
    """Generate a Dymo 11353 PDF — one page per component row."""
    surface = cairo.PDFSurface(output_pdf, _PAGE_W_PT, _PAGE_H_PT)
    ctx     = cairo.Context(surface)

    scale_x = _PAGE_W_PT / LABEL_SIZE[0]
    scale_y = _PAGE_H_PT / LABEL_SIZE[1]

    for idx, row in enumerate(rows):
        comp_type = row['Type']
        value     = row['Value']
        voltage   = _str(row.get('Voltage'))
        footprint = _str(row.get('Footprint'))

        print(f"Generating label {idx+1}/{len(rows)}: {comp_type} {value} {voltage} {footprint}")

        label = create_component_label(comp_type, value, voltage, footprint)

        buf = io.BytesIO()
        label.save(buf, format="PNG")
        buf.seek(0)

        img_surf = cairo.ImageSurface.create_from_png(buf)
        ctx.save()
        ctx.scale(scale_x, scale_y)
        ctx.set_source_surface(img_surf, 0, 0)
        ctx.paint()
        ctx.restore()
        surface.show_page()

    surface.finish()
    print(f"\nPDF saved to: {output_pdf}")


def print_labels_to_dymo(pdf_file="labels.pdf", printer_name="DYMO_LabelWriter_4XL", num_pages=None):
    """Send the label PDF to a Dymo printer via CUPS."""
    command = [
        "lp",
        "-d", printer_name,
        "-o", f"media={_LBL_MEDIA}",
        "-o", "scaling=fit-to-page",
    ]
    if num_pages:
        command.extend(["-P", f"1-{num_pages}"])
    command.append(pdf_file)
    try:
        subprocess.run(command, check=True)
        print(f"Labels sent to printer: {printer_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error printing: {e}")
        return False


# ============================================================================
# MAIN PROGRAM
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description='Generate Dymo 11353 component labels',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python app.py csv components.csv
  python app.py csv components.csv -o my_labels.pdf
  python app.py direct labels_batch.csv
  python app.py single -t C -v 10uF -V 16V -f 0805
  python app.py single -t R -v 10k -f 0402 --print
''',
    )
    sub = parser.add_subparsers(dest='mode')

    # csv mode — extract from BOM then generate labels
    p = sub.add_parser('csv', help='Extract C/R from BOM CSV and generate labels')
    p.add_argument('input_csv')
    p.add_argument('-o', '--output', default='labels.pdf')
    p.add_argument('--extracted-csv', default='capacitors_resistors_extracted.csv')
    p.add_argument('--print',   dest='print_labels', action='store_true')
    p.add_argument('--printer', default='DYMO_LabelWriter_4XL')
    p.add_argument('--pages',   type=int, default=None)

    # direct mode — pre-formatted Type,Value,Voltage,Footprint CSV
    p = sub.add_parser('direct', help='Generate labels from pre-formatted CSV')
    p.add_argument('input_csv')
    p.add_argument('-o', '--output', default='labels.pdf')
    p.add_argument('--print',   dest='print_labels', action='store_true')
    p.add_argument('--printer', default='DYMO_LabelWriter_4XL')
    p.add_argument('--pages',   type=int, default=None)

    # single mode — one component from command line
    p = sub.add_parser('single', help='Generate a label for one component')
    p.add_argument('-t', '--type',     required=True, choices=['C', 'R', 'L', 'S'])
    p.add_argument('-v', '--value',    required=True)
    p.add_argument('-V', '--voltage',  default='N/A')
    p.add_argument('-f', '--footprint',required=True)
    p.add_argument('-o', '--output',   default='labels.pdf')
    p.add_argument('--print',   dest='print_labels', action='store_true')
    p.add_argument('--printer', default='DYMO_LabelWriter_4XL')

    return parser.parse_args()


def main():
    args = parse_args()

    if args.mode is None:
        print("Error: please specify a mode (csv / direct / single)")
        print("Use --help for usage information")
        return

    if args.mode == 'csv':
        print("=" * 60)
        print("STEP 1: EXTRACTING COMPONENTS")
        print("=" * 60)

        rows = extract_components(args.input_csv)
        save_components_csv(rows, args.extracted_csv)

        print(f"\nExtracted {len(rows)} components")
        print_components_table(rows)
        print(f"\nSaved to: {args.extracted_csv}")

        print("\n" + "=" * 60)
        print("STEP 2: GENERATING PDF LABELS")
        print("=" * 60 + "\n")

        generate_labels_pdf(rows, args.output)

        if args.print_labels:
            print("\n" + "=" * 60)
            print("STEP 3: PRINTING LABELS")
            print("=" * 60)
            n = args.pages or len(rows)
            print(f"\nPrinting {n} label(s) to {args.printer}...")
            print_labels_to_dymo(args.output, args.printer, n)

    elif args.mode == 'direct':
        print("=" * 60)
        print("LOADING COMPONENTS FROM CSV")
        print("=" * 60)

        rows = load_components_csv(args.input_csv)
        print(f"\nLoaded {len(rows)} components:")
        print_components_table(rows)

        print("\n" + "=" * 60)
        print("GENERATING PDF LABELS")
        print("=" * 60 + "\n")

        generate_labels_pdf(rows, args.output)

        if args.print_labels:
            n = args.pages or len(rows)
            print(f"\nPrinting {n} label(s) to {args.printer}...")
            print_labels_to_dymo(args.output, args.printer, n)

    elif args.mode == 'single':
        rows = [{'Type': args.type, 'Value': args.value,
                 'Voltage': args.voltage, 'Footprint': args.footprint}]

        print("=" * 60)
        print("GENERATING SINGLE COMPONENT LABEL")
        print("=" * 60)
        print(f"\nComponent: {args.type} {args.value} {args.voltage} {args.footprint}\n")

        generate_labels_pdf(rows, args.output)

        if args.print_labels:
            print(f"\nPrinting to {args.printer}...")
            print_labels_to_dymo(args.output, args.printer, 1)

    print("\nDone!")


if __name__ == "__main__":
    main()
