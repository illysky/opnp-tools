"""
Create Feeders -- OpenPnP Jython Script
========================================
Workflow:
  1. Optionally clear existing feeders (Yes / No / Cancel).
  2. Select an open board, enter build size, passive attrition % and
     pick Z height — all in one form.
  3. Per-part quantities are calculated from board placement counts x build
     size x (1 + attrition/100).  Tapes that would exceed MAX_TAPE_MM
     (160 mm) are split across multiple feeder slots.
  4. All ReferenceStripFeeders are created, pre-assigned to their part and
     pitch, named with the per-feeder count, and left disabled so pick
     positions can be taught before use.

Tape pitch per package is read from package_rules.json (tape_pitch_mm field).
"""

from __future__ import absolute_import
import os, re, json, math
import xml.etree.ElementTree as ET

from javax.swing import (JOptionPane, JPanel, JLabel, JTextField,
                         JComboBox, BorderFactory)
from java.awt import GridBagLayout, GridBagConstraints, Insets

from org.openpnp.model import Configuration, LengthUnit, Length, Location

try:
    from org.openpnp.machine.reference.feeder import ReferenceStripFeeder
except ImportError:
    try:
        from org.openpnp.machine.reference import ReferenceStripFeeder
    except ImportError:
        ReferenceStripFeeder = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIALOG_TITLE = "Create Feeders"
DIALOG_WIDTH = 340
MAX_TAPE_MM  = 160.0
FIDUCIAL_RE  = re.compile(r'(?i)fiducial')

RULES_FILE = os.path.expanduser("~/.openpnp2/scripts/illysky/package_rules.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(text):
    html = "<html><div style='width:{}px'>{}</div></html>".format(
        DIALOG_WIDTH, text.replace("\n", "<br>"))
    return JLabel(html)


def _load_rules():
    try:
        with open(RULES_FILE) as f:
            data = json.load(f)
        return data.get("rules", [])
    except Exception as e:
        JOptionPane.showMessageDialog(None,
            _msg("Could not load package_rules.json:\n{}".format(e)),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return []


def _parse_tape_spec(spec):
    """Parse '8-35-B' -> (width_mm=8.0, thickness_mm=0.35, colour='B').

    Format: '{width}-{thickness*100}-{colour}'
      width     : 8 | 12 | 16
      thickness : 35=0.35mm  70=0.70mm  100=1.0mm
      colour    : B=black  W=white  C=clear
    Returns defaults (8.0, 0.35, 'B') on any parse error.
    """
    try:
        parts = spec.split("-")
        width     = float(parts[0])
        thickness = float(parts[1]) / 100.0
        colour    = parts[2].upper() if len(parts) > 2 else "B"
        return width, thickness, colour
    except Exception:
        return 8.0, 0.35, "B"


def _lookup_tape(pkg_id, rules):
    """Return (pitch_mm, width_mm, thickness_mm) for pkg_id from rules."""
    for rule in rules:
        pattern = rule.get("pattern", "")
        if pattern and re.search(pattern, pkg_id, re.IGNORECASE):
            pitch = float(rule.get("tape_pitch_mm", 4.0))
            width, thickness, _ = _parse_tape_spec(rule.get("tape_spec", "8-70-B"))
            return pitch, width, thickness
    return 4.0, 8.0, 0.70


def _count_placements(board):
    """Return {part_id: count} for all non-fiducial, enabled placements.

    Accepts type="Place" and type="Placement" (both appear in the wild
    depending on how the board XML was generated).  Only skips explicit
    Fiducial entries and disabled (DNP) placements.
    """
    board_file = board.getFile()
    if board_file is None:
        return {}
    tree = ET.parse(board_file.getAbsolutePath())
    counts = {}
    for pl in tree.getroot().iter("placement"):
        pl_type = pl.get("type", "Placement")
        if "fiducial" in pl_type.lower():
            continue
        if pl.get("enabled", "true").lower() == "false":
            continue
        part_id = pl.get("part-id", "").strip()
        if part_id:
            counts[part_id] = counts.get(part_id, 0) + 1
    return counts


def _add_form_row(panel, gbc, label_text, widget, row):
    gbc.gridwidth = 1
    gbc.gridx, gbc.gridy = 0, row
    gbc.anchor = GridBagConstraints.LINE_END
    gbc.insets = Insets(4, 4, 4, 8)
    panel.add(JLabel(label_text), gbc)
    gbc.gridx  = 1
    gbc.anchor = GridBagConstraints.LINE_START
    gbc.insets = Insets(4, 0, 4, 4)
    panel.add(widget, gbc)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    if ReferenceStripFeeder is None:
        JOptionPane.showMessageDialog(None,
            _msg("ReferenceStripFeeder class not found.\n"
                 "This script requires OpenPnP 2.x."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    cfg   = Configuration.get()
    rules = _load_rules()

    # ------------------------------------------------------------------
    # Dialog 1: clear existing feeders?
    # ------------------------------------------------------------------
    OPTS = ["Yes", "No", "Cancel"]
    choice = JOptionPane.showOptionDialog(
        None,
        _msg("Clear all existing feeders before creating new ones?"),
        DIALOG_TITLE,
        JOptionPane.DEFAULT_OPTION, JOptionPane.QUESTION_MESSAGE,
        None, OPTS, OPTS[1],
    )
    if choice == 2 or choice == JOptionPane.CLOSED_OPTION:
        return
    do_clear = (choice == 0)

    # ------------------------------------------------------------------
    # Dialog 2: board + build parameters
    # ------------------------------------------------------------------
    open_boards = list(cfg.getBoards())
    if not open_boards:
        JOptionPane.showMessageDialog(None,
            _msg("No boards are currently open in OpenPnP.\n"
                 "Open a board first via File > Open Board, then run this script."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    combo_board  = JComboBox([b.getName() for b in open_boards])
    tf_build     = JTextField("1",  6)
    tf_attrition = JTextField("10", 6)
    tf_z         = JTextField("0.0", 6)

    panel = JPanel(GridBagLayout())
    panel.setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8))
    gbc = GridBagConstraints()
    gbc.fill    = GridBagConstraints.HORIZONTAL
    gbc.weightx = 1.0

    # Board selector spans both columns
    gbc.gridwidth, gbc.gridx, gbc.gridy = 2, 0, 0
    gbc.anchor = GridBagConstraints.LINE_START
    gbc.insets = Insets(4, 4, 2, 4)
    panel.add(JLabel("Board:"), gbc)
    gbc.gridy  = 1
    gbc.insets = Insets(0, 4, 10, 4)
    panel.add(combo_board, gbc)

    _add_form_row(panel, gbc, "Build size:",    tf_build,     2)
    _add_form_row(panel, gbc, "Attrition (%):", tf_attrition, 3)
    _add_form_row(panel, gbc, "Pick Z (mm):",   tf_z,         4)

    ok = JOptionPane.showConfirmDialog(None, panel, DIALOG_TITLE,
         JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE)
    if ok != JOptionPane.OK_OPTION:
        return

    try:
        build_size = int(tf_build.getText().strip())
        attrition  = float(tf_attrition.getText().strip()) / 100.0
        feeder_z   = float(tf_z.getText().strip())
    except ValueError:
        JOptionPane.showMessageDialog(None,
            _msg("Build size must be a whole number.\n"
                 "Attrition and Z must be numbers."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    selected_board   = open_boards[combo_board.getSelectedIndex()]
    placement_counts = _count_placements(selected_board)

    if not placement_counts:
        JOptionPane.showMessageDialog(None,
            _msg("No placements found in board: " + selected_board.getName()),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Optionally clear existing feeders
    # ------------------------------------------------------------------
    machine = cfg.getMachine()
    if do_clear:
        for feeder in list(machine.getFeeders()):
            machine.removeFeeder(feeder)

    # ------------------------------------------------------------------
    # Build feeder plan
    # ------------------------------------------------------------------
    feeder_plan = []
    skipped     = []

    for part_id in sorted(placement_counts.keys()):
        if FIDUCIAL_RE.search(part_id):
            continue
        board_count = placement_counts[part_id]
        part = cfg.getPart(part_id)
        if part is None:
            skipped.append(part_id)
            continue

        pkg    = part.getPackage()
        pkg_id = pkg.getId() if pkg else ""
        pitch, tape_w, tape_t = _lookup_tape(pkg_id, rules)

        total_qty   = int(math.ceil(board_count * build_size * (1.0 + attrition)))
        tape_mm     = total_qty * pitch
        num_feeders = max(1, int(math.ceil(tape_mm / MAX_TAPE_MM)))
        per_feeder  = int(math.ceil(float(total_qty) / num_feeders))

        feeder_plan.append({
            "part_id":        part_id,
            "part":           part,
            "pitch":          pitch,
            "tape_width":     tape_w,
            "tape_thickness": tape_t,
            "num_feeders":    num_feeders,
            "per_feeder":     per_feeder,
        })

    if not feeder_plan:
        JOptionPane.showMessageDialog(None,
            _msg("No known parts found in the board.\n"
                 "Run 'Config Parts from Board' first."),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Create feeders
    # ------------------------------------------------------------------
    created = 0

    for fp in feeder_plan:
        # Z = user value + tape thickness (component sits on top of the tape pocket)
        pick_z = feeder_z + fp["tape_thickness"]
        z_loc  = Location(LengthUnit.Millimeters, 0.0, 0.0, pick_z, 0.0)

        for i in range(fp["num_feeders"]):
            if fp["num_feeders"] > 1:
                name = "{}_F{}_x{}".format(fp["part_id"], i + 1, fp["per_feeder"])
            else:
                name = "{}_x{}".format(fp["part_id"], fp["per_feeder"])

            feeder = ReferenceStripFeeder()
            feeder.setName(name)
            feeder.setPart(fp["part"])
            feeder.setPartPitch(Length(fp["pitch"], LengthUnit.Millimeters))
            feeder.setTapeWidth(Length(fp["tape_width"], LengthUnit.Millimeters))
            feeder.setEnabled(False)

            # Feed count — try common field names across OpenPnP versions
            try:
                feeder.setFeedCount(fp["per_feeder"])
            except Exception:
                pass
            try:
                feeder.setPartCount(fp["per_feeder"])
            except Exception:
                pass

            try:
                feeder.setReferenceHoleLocation(z_loc)
                feeder.setLastHoleLocation(z_loc)
            except Exception:
                pass

            machine.addFeeder(feeder)
            created += 1

    cfg.save()

    lines = ["{} feeders created ({} unique parts)".format(created, len(feeder_plan))]
    if skipped:
        lines.append("{} skipped (not in OpenPnP -- run Config Parts first)".format(len(skipped)))
    lines.append("Feeders are disabled -- teach pick positions then enable.")

    JOptionPane.showMessageDialog(None,
        _msg("\n".join(lines)),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


run()
