"""
Create Feeders -- OpenPnP Jython Script
========================================
Workflow:
  1. Optionally clear existing feeders (Yes / No / Cancel).
  2. Enter quantity per feeder and pick Z height (mm).
  3. Script iterates all non-fiducial parts already configured in OpenPnP,
     looks up each package's tape pitch from package_rules.json, and splits
     into multiple feeder slots when a tape would exceed MAX_TAPE_MM (160 mm).
  4. All ReferenceStripFeeders are created, pre-assigned to their part and
     pitch, named with the per-feeder count, and left disabled so positions
     can be taught before use.
"""

from __future__ import absolute_import
import os, re, json, math

from javax.swing import JOptionPane, JPanel, JLabel, JTextField, BorderFactory
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

DIALOG_TITLE    = "Create Feeders"
DIALOG_WIDTH    = 320
MAX_TAPE_MM     = 160.0
FIDUCIAL_RE     = re.compile(r'(?i)fiducial')

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


def _lookup_tape_pitch(pkg_id, rules):
    for rule in rules:
        pattern = rule.get("pattern", "")
        if pattern and re.search(pattern, pkg_id, re.IGNORECASE):
            return float(rule.get("tape_pitch_mm", 4.0))
    return 4.0


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
    # Dialog 2: quantity + Z height
    # ------------------------------------------------------------------
    tf_qty = JTextField("50", 6)
    tf_z   = JTextField("0.0", 6)

    panel = JPanel(GridBagLayout())
    panel.setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8))
    gbc = GridBagConstraints()
    gbc.fill    = GridBagConstraints.HORIZONTAL
    gbc.weightx = 1.0

    _add_form_row(panel, gbc, "Quantity per feeder:", tf_qty, 0)
    _add_form_row(panel, gbc, "Pick Z height (mm):",  tf_z,   1)

    ok = JOptionPane.showConfirmDialog(None, panel, DIALOG_TITLE,
         JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE)
    if ok != JOptionPane.OK_OPTION:
        return

    try:
        qty_per_feeder = int(tf_qty.getText().strip())
        feeder_z       = float(tf_z.getText().strip())
    except ValueError:
        JOptionPane.showMessageDialog(None,
            _msg("Quantity must be a whole number.\nZ height must be a number."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Optionally clear existing feeders
    # ------------------------------------------------------------------
    machine = cfg.getMachine()

    if do_clear:
        for feeder in list(machine.getFeeders()):
            machine.removeFeeder(feeder)

    # ------------------------------------------------------------------
    # Build feeder plan from all configured parts
    # ------------------------------------------------------------------
    all_parts = list(cfg.getParts())
    feeder_plan = []

    for part in sorted(all_parts, key=lambda p: p.getId()):
        part_id = part.getId()
        if FIDUCIAL_RE.search(part_id):
            continue

        pkg    = part.getPackage()
        pkg_id = pkg.getId() if pkg else ""
        pitch  = _lookup_tape_pitch(pkg_id, rules)

        tape_mm     = qty_per_feeder * pitch
        num_feeders = max(1, int(math.ceil(tape_mm / MAX_TAPE_MM)))
        per_feeder  = int(math.ceil(float(qty_per_feeder) / num_feeders))

        feeder_plan.append({
            "part_id":     part_id,
            "part":        part,
            "pitch":       pitch,
            "num_feeders": num_feeders,
            "per_feeder":  per_feeder,
        })

    if not feeder_plan:
        JOptionPane.showMessageDialog(None,
            _msg("No parts found in OpenPnP.\nRun 'Config Parts from Board' first."),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Create feeders
    # ------------------------------------------------------------------
    z_loc   = Location(LengthUnit.Millimeters, 0.0, 0.0, feeder_z, 0.0)
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
            feeder.setEnabled(False)

            try:
                feeder.setReferenceHoleLocation(z_loc)
                feeder.setLastHoleLocation(z_loc)
            except Exception:
                pass

            machine.addFeeder(feeder)
            created += 1

    cfg.save()

    JOptionPane.showMessageDialog(None,
        _msg("{} feeders created ({} unique parts)\n"
             "Feeders are disabled -- teach pick positions then enable.".format(
             created, len(feeder_plan))),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


run()
