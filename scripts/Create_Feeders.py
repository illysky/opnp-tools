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
  4. Tape spec is read from each Package's tapeSpecification field
     (set by Create Parts).  Format: '{width}-{thickness*100}-{colour}-{pitch}'
     e.g. '8-70-W-2'.
       width    : 8 | 12 | 16  (mm)
       thickness: 35=0.35mm  70=0.70mm  100=1.0mm  (added to pick Z)
       colour   : B | W | C   (reserved for future vision settings)
       pitch    : 2 | 4 | 8   (mm between components)
  5. Parts with a blank tape spec are skipped.  Parts with an invalid spec
     are collected and shown in an error summary before any feeders are made.
  6. All ReferenceStripFeeders are created, pre-assigned to their part,
     tape width, part pitch and max feed count.  Feeders are left disabled
     so pick positions can be taught before use.
"""

from __future__ import absolute_import
import os, re, math
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

DIALOG_TITLE  = "Create Feeders"
DIALOG_WIDTH  = 340
MAX_TAPE_MM   = 160.0
FIDUCIAL_RE   = re.compile(r'(?i)fiducial')

VALID_WIDTHS      = {8, 12, 16}
VALID_THICKNESSES = {35, 70, 100}
VALID_COLOURS     = {"B", "W", "C"}
VALID_PITCHES     = {2, 4, 8}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(text):
    html = "<html><div style='width:{}px'>{}</div></html>".format(
        DIALOG_WIDTH, text.replace("\n", "<br>"))
    return JLabel(html)


def _parse_tape_spec(spec, part_id, pkg_id):
    """Parse and validate a tape spec string.

    Returns (width_mm, thickness_mm, colour, pitch_mm) on success.
    Returns None if spec is blank (caller should skip the part).
    Raises ValueError with a descriptive message if spec is invalid.
    """
    if not spec or not spec.strip():
        return None   # blank = skip silently

    label = "{} [{}]".format(part_id, pkg_id)
    s = spec.strip()
    parts = s.split("-")

    if len(parts) != 4:
        raise ValueError("{}: expected 4 fields (e.g. 8-70-W-4), got '{}'".format(label, s))

    # Width
    try:
        w = int(parts[0])
    except ValueError:
        raise ValueError("{}: width '{}' is not a number".format(label, parts[0]))
    if w not in VALID_WIDTHS:
        raise ValueError("{}: width {} not in {}".format(label, w, sorted(VALID_WIDTHS)))

    # Thickness
    try:
        t = int(parts[1])
    except ValueError:
        raise ValueError("{}: thickness '{}' is not a number".format(label, parts[1]))
    if t not in VALID_THICKNESSES:
        raise ValueError("{}: thickness {} not in {}".format(label, t, sorted(VALID_THICKNESSES)))

    # Colour
    c = parts[2].upper()
    if c not in VALID_COLOURS:
        raise ValueError("{}: colour '{}' not in {}".format(label, c, sorted(VALID_COLOURS)))

    # Pitch
    try:
        p = int(parts[3])
    except ValueError:
        raise ValueError("{}: pitch '{}' is not a number".format(label, parts[3]))
    if p not in VALID_PITCHES:
        raise ValueError("{}: pitch {} not in {}".format(label, p, sorted(VALID_PITCHES)))

    return float(w), t / 100.0, c, float(p)


def _count_placements(board):
    """Return {part_id: count} for all non-fiducial, enabled placements."""
    board_file = board.getFile()
    if board_file is None:
        return {}
    tree = ET.parse(board_file.getAbsolutePath())
    counts = {}
    for pl in tree.getroot().iter("placement"):
        if "fiducial" in pl.get("type", "").lower():
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

    cfg = Configuration.get()

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
    tf_build     = JTextField("1",   6)
    tf_attrition = JTextField("10",  6)
    tf_z         = JTextField("4.2", 6)

    panel = JPanel(GridBagLayout())
    panel.setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8))
    gbc = GridBagConstraints()
    gbc.fill    = GridBagConstraints.HORIZONTAL
    gbc.weightx = 1.0

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
    # Build feeder plan — validate tape specs first, collect all errors
    # ------------------------------------------------------------------
    feeder_plan  = []
    skipped_none = []   # part not in OpenPnP
    skipped_spec = []   # blank tape spec
    errors       = []   # invalid tape spec strings

    for part_id in sorted(placement_counts.keys()):
        if FIDUCIAL_RE.search(part_id):
            continue
        board_count = placement_counts[part_id]

        part = cfg.getPart(part_id)
        if part is None:
            skipped_none.append(part_id)
            continue

        pkg    = part.getPackage()
        pkg_id = pkg.getId() if pkg else ""

        # Read tape spec from Package object (set by Create Parts)
        tape_spec_str = ""
        try:
            ts = pkg.getTapeSpecification() if pkg else None
            if ts:
                tape_spec_str = ts.strip()
        except Exception:
            pass

        try:
            parsed = _parse_tape_spec(tape_spec_str, part_id, pkg_id)
        except ValueError as e:
            errors.append(str(e))
            continue

        if parsed is None:
            skipped_spec.append("{} [{}]".format(part_id, pkg_id))
            continue

        tape_w, tape_t, _colour, pitch = parsed

        # Attrition only applies to passives (C / L / R designators)
        is_passive  = part_id[0].upper() in ('C', 'L', 'R')
        multiplier  = (1.0 + attrition) if is_passive else 1.0
        total_qty   = int(math.ceil(board_count * build_size * multiplier))
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

    # Show all errors before doing anything — user must fix them first
    if errors:
        JOptionPane.showMessageDialog(None,
            _msg("Invalid tape spec on {} part(s) -- fix in package rules "
                 "then re-run Create Parts:\n\n{}".format(
                     len(errors), "\n".join(errors))),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    if not feeder_plan:
        lines = ["No feeders to create."]
        if skipped_spec:
            lines.append("{} parts have no tape spec -- run Create Parts first.".format(
                len(skipped_spec)))
        if skipped_none:
            lines.append("{} parts not in OpenPnP -- run Create Parts first.".format(
                len(skipped_none)))
        JOptionPane.showMessageDialog(None, _msg("\n".join(lines)),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Clear existing feeders if requested
    # ------------------------------------------------------------------
    machine = cfg.getMachine()
    if do_clear:
        for feeder in list(machine.getFeeders()):
            machine.removeFeeder(feeder)

    # ------------------------------------------------------------------
    # Create feeders — names are sequential per tape width group
    #   8mm  -> 8MM-01, 8MM-02, ...
    #   12mm -> 12MM-01, 12MM-02, ...
    #   16mm -> 16MM-01, 16MM-02, ...
    # ------------------------------------------------------------------
    created  = 0
    seq_by_width = {}   # tape_width (int) -> next sequence number

    for fp in feeder_plan:
        # Z = user value + tape thickness so nozzle reaches into the pocket
        pick_z = feeder_z + fp["tape_thickness"]
        z_loc  = Location(LengthUnit.Millimeters, 0.0, 0.0, pick_z, 0.0)

        for i in range(fp["num_feeders"]):
            w_key = int(fp["tape_width"])
            seq   = seq_by_width.get(w_key, 1)
            seq_by_width[w_key] = seq + 1
            name  = "{}MM-{:02d}".format(w_key, seq)

            feeder = ReferenceStripFeeder()
            feeder.setName(name)
            feeder.setPart(fp["part"])
            feeder.setPartPitch(Length(fp["pitch"],      LengthUnit.Millimeters))
            feeder.setTapeWidth(Length(fp["tape_width"], LengthUnit.Millimeters))
            feeder.setFeedCount(0)
            feeder.setMaxFeedCount(fp["per_feeder"])
            feeder.setEnabled(False)

            try:
                feeder.setReferenceHoleLocation(z_loc)
                feeder.setLastHoleLocation(z_loc)
            except Exception:
                pass

            machine.addFeeder(feeder)
            created += 1

    cfg.save()

    lines = ["{} feeders created ({} unique parts)".format(created, len(feeder_plan))]
    if skipped_spec:
        lines.append("{} skipped (no tape spec)".format(len(skipped_spec)))
    if skipped_none:
        lines.append("{} skipped (not in OpenPnP)".format(len(skipped_none)))
    lines.append("Feeders disabled -- teach pick positions then enable.")

    JOptionPane.showMessageDialog(None,
        _msg("\n".join(lines)),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


run()
