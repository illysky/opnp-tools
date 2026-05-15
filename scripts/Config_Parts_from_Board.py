"""
Config Parts from Board
=======================
OpenPnP Jython script — appears in the Scripts menu automatically.

Workflow:
  1. Ask whether to clear existing (non-fiducial) parts and packages.
  2. Show a dropdown of boards currently open in OpenPnP.
  3. Parse the chosen board's placements for unique part-id / package-id pairs.
  4. Create packages (nozzle, height, footprint geometry) from package_rules.json.
  5. Create parts linked to their packages.
  6. Save configuration and show a summary.

Rules are loaded from package_rules.json next to this script.
Run "Update Package Rules" after tweaking packages in OpenPnP to save
your changes back to package_rules.json.
"""

from __future__ import absolute_import
import os, re, json
import xml.etree.ElementTree as ET

from javax.swing import (JOptionPane, JComboBox, JPanel, JLabel, BoxLayout)

DIALOG_TITLE = "Config Parts from Board"
DIALOG_WIDTH = 300   # px — keeps all popups the same width

def _msg(text):
    """Wrap text in a fixed-width HTML label for consistent dialog sizing."""
    html = "<html><div style='width:{}px'>{}</div></html>".format(
        DIALOG_WIDTH, text.replace("\n", "<br>"))
    return JLabel(html)

from org.openpnp.model import Configuration, Part, LengthUnit, Length
# 'Package' clashes with the Python built-in keyword, import via the module alias
from org.openpnp import model as _openpnp_model

# ---------------------------------------------------------------------------
# Rules file — loaded at runtime, edit with Update_Package_Rules script
# ---------------------------------------------------------------------------

RULES_FILE = os.path.expanduser("~/.openpnp2/scripts/illysky/package_rules.json")

def _load_rules():
    """Load rules from package_rules.json. Falls back to a minimal catch-all."""
    try:
        with open(RULES_FILE) as f:
            data = json.load(f)
        return data.get("rules", [])
    except Exception as e:
        JOptionPane.showMessageDialog(None,
            _msg("Could not load package_rules.json:\n{}\n\nUsing catch-all defaults.".format(e)),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return [{"pattern": "", "nozzle": "N24", "height_mm": 1.0,
                 "body_width_mm": 0.0, "body_height_mm": 0.0}]

# Fiducial guard — part/package IDs matching this are never removed
FIDUCIAL_PATTERN = re.compile(r"fiducial|fidhole|fid\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_nozzle_name_to_id(machine_xml_path):
    """
    Parse machine.xml and return a dict mapping nozzle tip name → id.
    e.g. {"N045": "NT1", "N24": "TIP16f0412ed2d50009", ...}
    Falls back to an empty dict if the file cannot be read.
    """
    mapping = {}
    try:
        tree = ET.parse(machine_xml_path)
        for nt in tree.getroot().iter("nozzle-tip"):
            name = nt.get("name", "").strip()
            tid  = nt.get("id",   "").strip()
            if name and tid:
                mapping[name] = tid
    except Exception:
        pass
    return mapping


def _parse_tape_spec(spec):
    """Parse '8-70-W-2' -> (width_mm, thickness_mm, colour, pitch_mm)."""
    try:
        p = spec.split("-")
        return float(p[0]), float(p[1]) / 100.0, \
               p[2].upper() if len(p) > 2 else "B", \
               float(p[3]) if len(p) > 3 else 4.0
    except Exception:
        return 8.0, 0.35, "B", 4.0


def _lookup_package(pkg_id, rules, nozzle_name_to_id):
    """
    Return (nozzle_tip_id, height_mm, body_width_mm, body_height_mm, tape_spec)
    for the first rule whose pattern matches pkg_id (case-insensitive).
    """
    for rule in rules:
        if re.search(rule["pattern"], pkg_id, re.IGNORECASE):
            nozzle_name = rule.get("nozzle", "N24")
            nozzle_id   = nozzle_name_to_id.get(nozzle_name, nozzle_name)
            return (nozzle_id,
                    rule.get("height_mm", 1.0),
                    rule.get("body_width_mm", 0.0),
                    rule.get("body_height_mm", 0.0),
                    rule.get("tape_spec", "8-35-B-4"))
    return None, 1.0, 0.0, 0.0, "8-35-B-4"


def _add_nozzle_tip_to_package(pkg, nozzle_id):
    """
    Add nozzle_id to the package's compatible nozzle tip list in memory.
    Uses Java reflection to access the private field directly — this works
    regardless of which OpenPnP API version is installed.
    Also fires a property change event so the UI refreshes immediately.
    """
    cls = pkg.getClass()
    while cls is not None:
        for field in cls.getDeclaredFields():
            if field.getName() == "compatibleNozzleTipIds":
                field.setAccessible(True)
                id_list = field.get(pkg)
                if not id_list.contains(nozzle_id):
                    id_list.add(nozzle_id)
                # Jython can call protected methods directly — this fires
                # the property change so the UI updates without a restart.
                try:
                    pkg.firePropertyChange("compatibleNozzleTipIds", None, id_list)
                except Exception:
                    pass
                return True
        cls = cls.getSuperclass()
    return False


def _parse_board(board):
    """
    Return list of unique (part_id, package_id) pairs from an open Board object.

    Strategy: read directly from the board's backing file via ElementTree so we
    get the raw part-id strings even when the parts don't yet exist in the
    configuration (placement.getPart() would return None in that case).

    package_id is derived as the segment after the first '-' in part_id, which
    matches the format produced by pnp_creator.py: comment[:10]-footprint[:10].
    """
    board_file = board.getFile()
    if board_file is None:
        return []
    path = board_file.getAbsolutePath()
    tree = ET.parse(path)
    seen = {}
    for pl in tree.getroot().iter("placement"):
        part_id = pl.get("part-id", "").strip()
        if not part_id:
            continue
        dash = part_id.find("-")
        pkg_id = part_id[dash + 1:] if dash != -1 else part_id
        if part_id not in seen:
            seen[part_id] = pkg_id
    return list(seen.items())  # [(part_id, pkg_id), ...]

# ---------------------------------------------------------------------------
# Main — wrapped in a function so we can return on cancel without any error
# ---------------------------------------------------------------------------

def run():
    cfg = Configuration.get()

    # Load rules from package_rules.json
    rules = _load_rules()

    # Resolve nozzle tip names (e.g. "N24") to their internal IDs from machine.xml
    config_dir        = cfg.getConfigurationDirectory().getAbsolutePath()
    machine_xml_path  = os.path.join(config_dir, "machine.xml")
    nozzle_name_to_id = _build_nozzle_name_to_id(machine_xml_path)

    # --- Dialog 1: clear existing? ----------------------------------------
    CLEAR_OPTIONS = ["Yes", "No", "Cancel"]
    clear_choice = JOptionPane.showOptionDialog(
        None,
        _msg("Clear all existing (non-fiducial) parts and packages before importing?"),
        DIALOG_TITLE,
        JOptionPane.DEFAULT_OPTION,
        JOptionPane.QUESTION_MESSAGE,
        None,
        CLEAR_OPTIONS,
        CLEAR_OPTIONS[1],
    )

    if clear_choice == 2 or clear_choice == JOptionPane.CLOSED_OPTION:
        return  # cancelled — silent exit

    do_clear = (clear_choice == 0)

    # --- Dialog 2: board selection (boards open in OpenPnP) ---------------
    open_boards = list(cfg.getBoards())
    if not open_boards:
        JOptionPane.showMessageDialog(
            None,
            _msg("No boards are currently open in OpenPnP.\n"
                 "Open a board first via File > Open Board, then run this script again."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE,
        )
        return

    display_names = [b.getName() for b in open_boards]
    combo = JComboBox(display_names)
    panel = JPanel()
    panel.setLayout(BoxLayout(panel, BoxLayout.Y_AXIS))
    panel.add(JLabel("Select board to import parts from:"))
    panel.add(combo)

    ok = JOptionPane.showConfirmDialog(
        None,
        panel,
        DIALOG_TITLE,
        JOptionPane.OK_CANCEL_OPTION,
        JOptionPane.PLAIN_MESSAGE,
    )

    if ok != JOptionPane.OK_OPTION:
        return  # cancelled — silent exit

    selected_board = open_boards[combo.getSelectedIndex()]
    selected_name  = selected_board.getName()

    # --- Parse board ------------------------------------------------------
    pairs = _parse_board(selected_board)
    if not pairs:
        JOptionPane.showMessageDialog(
            None,
            _msg("No placements with a part-id found in board: " + selected_name),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE,
        )
        return

    # --- Build package/part lookup maps -----------------------------------
    pkg_height_map    = {}   # pkg_id -> height_mm
    pkg_nozzle_map    = {}   # pkg_id -> nozzle_tip_id
    pkg_geometry_map  = {}   # pkg_id -> (body_width_mm, body_height_mm)
    pkg_tape_spec_map = {}   # pkg_id -> tape_spec string e.g. "8-70-W-2"
    new_part_ids      = set()
    new_pkg_ids       = set()

    for part_id, pkg_id in pairs:
        nozzle, height, bw, bh, tape_spec = _lookup_package(pkg_id, rules, nozzle_name_to_id)
        if height is not None:
            pkg_height_map[pkg_id]    = height
        if nozzle is not None:
            pkg_nozzle_map[pkg_id]    = nozzle
        pkg_geometry_map[pkg_id]  = (bw, bh)
        pkg_tape_spec_map[pkg_id] = tape_spec
        new_part_ids.add(part_id)
        new_pkg_ids.add(pkg_id)

    # --- Optionally clear existing non-fiducial parts + packages from memory -
    if do_clear:
        for p in list(cfg.getParts()):
            if not FIDUCIAL_PATTERN.search(p.getId()):
                try:
                    cfg.removePart(p)
                except Exception:
                    pass
        for pk in list(cfg.getPackages()):
            if not FIDUCIAL_PATTERN.search(pk.getId()):
                try:
                    cfg.removePackage(pk)
                except Exception:
                    pass

    # --- Create packages via API ------------------------------------------
    existing_pkg_ids = set(pkg.getId() for pkg in cfg.getPackages())
    n_pkgs_created   = 0
    n_unknown_pkgs   = 0

    for part_id, pkg_id in pairs:
        nozzle_id = pkg_nozzle_map.get(pkg_id)
        if pkg_id not in existing_pkg_ids:
            existing_pkg_ids.add(pkg_id)
            pkg = _openpnp_model.Package(pkg_id)
            pkg.setDescription(pkg_id)
            if nozzle_id is None:
                n_unknown_pkgs += 1
            cfg.addPackage(pkg)
            n_pkgs_created += 1

        # Apply nozzle tip, footprint geometry and tape spec to live object
        live_pkg = cfg.getPackage(pkg_id)
        if live_pkg is not None:
            if nozzle_id:
                _add_nozzle_tip_to_package(live_pkg, nozzle_id)
            bw, bh = pkg_geometry_map.get(pkg_id, (0.0, 0.0))
            fp = live_pkg.getFootprint()
            if fp is not None and (bw > 0.0 or bh > 0.0):
                fp.setBodyWidth(bw)
                fp.setBodyHeight(bh)
                fp.setUnits(LengthUnit.Millimeters)
            tape_spec_str = pkg_tape_spec_map.get(pkg_id, "8-35-B-4")
            try:
                live_pkg.setTapeSpecification(tape_spec_str)
            except Exception:
                pass

    # --- Create parts via API ---------------------------------------------
    existing_part_ids = set(p.getId() for p in cfg.getParts())
    n_parts_created   = 0

    for part_id, pkg_id in pairs:
        if part_id not in existing_part_ids:
            existing_part_ids.add(part_id)
            part = Part(part_id)
            linked_pkg = cfg.getPackage(pkg_id)
            if linked_pkg is not None:
                part.setPackage(linked_pkg)
            height = pkg_height_map.get(pkg_id)
            if height is not None:
                part.setHeight(Length(height, LengthUnit.Millimeters))
            cfg.addPart(part)
            n_parts_created += 1

    # --- Save (flushes in-memory state to XML files) ----------------------
    Configuration.get().save()

    # --- Post-process XML files -------------------------------------------
    # cfg.removePart/removePackage API is unreliable in OpenPnP 2.x so we
    # enforce both clear and nozzle-tip assignment directly on the saved XML.
    packages_xml = os.path.join(config_dir, "packages.xml")
    parts_xml    = os.path.join(config_dir, "parts.xml")

    # Patch packages.xml — optionally strip old entries, inject nozzle tips
    if os.path.exists(packages_xml):
        content = open(packages_xml).read()

        def _patch_package(m):
            block    = m.group(0)
            id_match = re.search(r'\bid="([^"]+)"', block)
            if not id_match:
                return block
            pid = id_match.group(1)
            # Remove if clearing and this pkg is not a fiducial or a new one
            if do_clear and not FIDUCIAL_PATTERN.search(pid) and pid not in new_pkg_ids:
                return ""
            # Replace the empty self-closing nozzle-tip tag that OpenPnP writes
            # by default with a populated one if we have a mapping for this pkg.
            nozzle = pkg_nozzle_map.get(pid)
            if nozzle:
                populated = (
                    '<compatible-nozzle-tip-ids class="java.util.ArrayList">\n'
                    '         <string>{}</string>\n'
                    '      </compatible-nozzle-tip-ids>'.format(nozzle)
                )
                # Replace the self-closing empty form OpenPnP 2.6 generates
                block = re.sub(
                    r'<compatible-nozzle-tip-ids\b[^/]*/>',
                    populated,
                    block,
                )
                # Handle idempotent re-run — update an already-populated tag
                block = re.sub(
                    r'(<compatible-nozzle-tip-ids\b[^>]*>)\s*<string>[^<]*</string>\s*(</compatible-nozzle-tip-ids>)',
                    r'\g<1>\n         <string>' + nozzle + r'</string>\n      \2',
                    block,
                )
            return block

        content = re.sub(r'<package\b.*?</package>', _patch_package,
                         content, flags=re.DOTALL)
        open(packages_xml, "w").write(content)

    # Patch parts.xml — optionally strip old entries
    if do_clear and os.path.exists(parts_xml):
        content = open(parts_xml).read()

        def _patch_part(m):
            block    = m.group(0)
            id_match = re.search(r'\bid="([^"]+)"', block)
            if not id_match:
                return block
            pid = id_match.group(1)
            if not FIDUCIAL_PATTERN.search(pid) and pid not in new_part_ids:
                return ""
            return block

        # Self-closing <part ... />
        content = re.sub(r'[ \t]*<part\b[^>]*/>\n?', _patch_part, content)
        # Block-style <part ...>...</part>
        content = re.sub(r'[ \t]*<part\b.*?</part>\n?', _patch_part,
                         content, flags=re.DOTALL)
        open(parts_xml, "w").write(content)

    # --- Summary ----------------------------------------------------------
    summary = "{}\n\n{} Parts Created\n{} Packages Created".format(
        selected_name, n_parts_created, n_pkgs_created)

    JOptionPane.showMessageDialog(
        None,
        _msg(summary),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE,
    )


run()
