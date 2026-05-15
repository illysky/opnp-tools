"""
Create Feeders -- OpenPnP Jython Script
========================================
Workflow:
  1. Select board (from boards currently open in OpenPnP), enter build size
     and passive attrition %.
  2. Script calculates cut-tape requirements for each part, splitting into
     multiple feeder slots when a single tape would exceed MAX_TAPE_MM (160 mm).
  3. Enter feeder pick Z height (mm) -- applies to all created feeders.
  4. All ReferenceStripFeeders are created, pre-assigned to their part, named
     with the per-feeder count, and left disabled so positions can be taught.

Tape pitch per package is read from package_rules.json (tape_pitch_mm field).
"""

from __future__ import absolute_import
import os, re, json, math
import xml.etree.ElementTree as ET

from javax.swing import (JOptionPane, JPanel, JLabel, JTextField,
                         JComboBox, BoxLayout, BorderFactory)
from java.awt import GridBagLayout, GridBagConstraints, Insets, Dimension

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
DIALOG_WIDTH = 340          # px — consistent popup width
MAX_TAPE_MM  = 160.0        # split into a new feeder when tape exceeds this

RULES_FILE = os.path.expanduser("~/.openpnp2/scripts/illysky/package_rules.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(text):
    """Fixed-width HTML label so all JOptionPane dialogs are the same width."""
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


def _lookup_tape_pitch(pkg_id, rules):
    """Return tape_pitch_mm for pkg_id by matching rules patterns."""
    for rule in rules:
        pattern = rule.get("pattern", "")
        if pattern and re.search(pattern, pkg_id, re.IGNORECASE):
            return float(rule.get("tape_pitch_mm", 4.0))
    return 4.0  # catch-all default


def _count_placements(board):
    """Return {part_id: count} for all Place-type, enabled placements."""
    board_file = board.getFile()
    if board_file is None:
        return {}
    tree = ET.parse(board_file.getAbsolutePath())
    counts = {}
    for pl in tree.getroot().iter("placement"):
        if pl.get("type", "Place") != "Place":
            continue
        if pl.get("enabled", "true").lower() == "false":
            continue
        part_id = pl.get("part-id", "").strip()
        if part_id:
            counts[part_id] = counts.get(part_id, 0) + 1
    return counts


def _add_form_row(panel, gbc, label_text, widget, row):
    """Append one label + widget row to a GridBagLayout panel."""
    gbc.gridwidth = 1
    gbc.gridx, gbc.gridy = 0, row
    gbc.anchor  = GridBagConstraints.LINE_END
    gbc.insets  = Insets(3, 4, 3, 8)
    panel.add(JLabel(label_text), gbc)
    gbc.gridx   = 1
    gbc.anchor  = GridBagConstraints.LINE_START
    gbc.insets  = Insets(3, 0, 3, 4)
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
    # Dialog 1: board selection + build parameters
    # ------------------------------------------------------------------
    open_boards = list(cfg.getBoards())
    if not open_boards:
        JOptionPane.showMessageDialog(None,
            _msg("No boards are currently open in OpenPnP.\n"
                 "Open a board first via File > Open Board, then run this script."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    combo_board  = JComboBox([b.getName() for b in open_boards])
    tf_build     = JTextField("1", 6)
    tf_attrition = JTextField("10", 6)

    panel1 = JPanel(GridBagLayout())
    panel1.setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8))
    gbc = GridBagConstraints()
    gbc.fill    = GridBagConstraints.HORIZONTAL
    gbc.weightx = 1.0

    # Board label + combo spans full width
    gbc.gridwidth, gbc.gridx, gbc.gridy = 2, 0, 0
    gbc.anchor = GridBagConstraints.LINE_START
    gbc.insets = Insets(3, 4, 2, 4)
    panel1.add(JLabel("Board:"), gbc)
    gbc.gridy = 1
    gbc.insets = Insets(0, 4, 8, 4)
    panel1.add(combo_board, gbc)

    _add_form_row(panel1, gbc, "Build size:",    tf_build,     2)
    _add_form_row(panel1, gbc, "Attrition (%):", tf_attrition, 3)

    ok = JOptionPane.showConfirmDialog(None, panel1, DIALOG_TITLE,
         JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE)
    if ok != JOptionPane.OK_OPTION:
        return

    try:
        build_size = int(tf_build.getText().strip())
        attrition  = float(tf_attrition.getText().strip()) / 100.0
    except ValueError:
        JOptionPane.showMessageDialog(None,
            _msg("Build size must be a whole number.\nAttrition must be a number."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    selected_board = open_boards[combo_board.getSelectedIndex()]

    placement_counts = _count_placements(selected_board)
    if not placement_counts:
        JOptionPane.showMessageDialog(None,
            _msg("No placements found in board: " + selected_board.getName()),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Calculate feeder plan
    # ------------------------------------------------------------------
    feeder_plan = []
    skipped     = []

    for part_id in sorted(placement_counts.keys()):
        board_count = placement_counts[part_id]
        part = cfg.getPart(part_id)
        if part is None:
            skipped.append(part_id)
            continue

        pkg    = part.getPackage()
        pkg_id = pkg.getId() if pkg else ""
        pitch  = _lookup_tape_pitch(pkg_id, rules)

        total_qty   = int(math.ceil(board_count * build_size * (1.0 + attrition)))
        tape_mm     = total_qty * pitch
        num_feeders = max(1, int(math.ceil(tape_mm / MAX_TAPE_MM)))
        per_feeder  = int(math.ceil(float(total_qty) / num_feeders))

        feeder_plan.append({
            "part_id":     part_id,
            "part":        part,
            "pkg_id":      pkg_id,
            "pitch":       pitch,
            "board_count": board_count,
            "total_qty":   total_qty,
            "num_feeders": num_feeders,
            "per_feeder":  per_feeder,
        })

    if not feeder_plan:
        JOptionPane.showMessageDialog(None,
            _msg("No known parts found in the board.\n"
                 "Run 'Config Parts from Board' first."),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Dialog 2: feeder Z height
    # ------------------------------------------------------------------
    tf_z = JTextField("0.0", 6)

    panel2 = JPanel(GridBagLayout())
    panel2.setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8))
    gbc2 = GridBagConstraints()
    gbc2.fill    = GridBagConstraints.HORIZONTAL
    gbc2.weightx = 1.0
    _add_form_row(panel2, gbc2, "Pick Z height (mm):", tf_z, 0)

    ok2 = JOptionPane.showConfirmDialog(None, panel2, DIALOG_TITLE,
          JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE)
    if ok2 != JOptionPane.OK_OPTION:
        return

    try:
        feeder_z = float(tf_z.getText().strip())
    except ValueError:
        JOptionPane.showMessageDialog(None,
            _msg("Feeder height must be a number."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Create feeders
    # ------------------------------------------------------------------
    machine = cfg.getMachine()
    created = 0

    for fp in feeder_plan:
        for i in range(fp["num_feeders"]):
            if fp["num_feeders"] > 1:
                name = "{}_F{}_x{}".format(fp["part_id"], i + 1, fp["per_feeder"])
            else:
                name = "{}_x{}".format(fp["part_id"], fp["per_feeder"])

            feeder = ReferenceStripFeeder()
            feeder.setName(name)
            feeder.setPart(fp["part"])
            feeder.setPartPitch(Length(fp["pitch"], LengthUnit.Millimeters))
            feeder.setEnabled(False)  # user must teach pick position before enabling

            # Set Z on reference hole locations so the machine has a
            # starting Z even before the user jogs to teach X/Y.
            z_loc = Location(LengthUnit.Millimeters, 0.0, 0.0, feeder_z, 0.0)
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
        lines.append("{} parts skipped (not in OpenPnP -- run Config Parts first)".format(len(skipped)))
    lines.append("Feeders are disabled -- teach pick positions then enable.")

    JOptionPane.showMessageDialog(None,
        _msg("\n".join(lines)),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


run()
