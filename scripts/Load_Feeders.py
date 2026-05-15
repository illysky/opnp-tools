"""
Load Feeders -- OpenPnP Jython Script
========================================
Allocates created feeders to physical holders, then walks through each one
interactively so you can load the tape and teach the two reference holes.

Holder concept
--------------
A holder is a 160 mm physical rail divided into 3 equal segments (~53 mm each).
A tape occupies ceil(cut_length / segment_mm) segments, where:
  cut_length = feeder.maxFeedCount x feeder.partPitch

Holders are arranged in a row along X. Feeders with matching (width, thickness)
can share a holder regardless of tape colour.

holder_config.json
------------------
Persisted in the scripts folder. Prompted on first run or when "Reconfigure"
is chosen. Fields:
  start_x    -- X coordinate of holder 0, segment 0
  start_y    -- Y coordinate of holder 0, segment 0 (tape direction)
  z          -- pick Z (before thickness offset; use the base Z height)
  spacing_x  -- X distance between adjacent holders (mm)
  segment_mm -- Y length of one segment (default 160/3 ≈ 53.33 mm)

Interactive load loop
---------------------
For each allocated feeder (in holder order):
  1. Popup: part, tape spec, cut length, holder/segment
  2. Options: Load / Skip / Exit
  3. Load:
     a. Camera moves to holder segment position
     b. "Jog to reference hole 1, click OK"  → capture location
     c. "Jog to reference hole 2, click OK"  → capture location
     d. setReferenceHoleLocation + setLastHoleLocation + enable feeder + save
"""

from __future__ import absolute_import
import os, re, json, math

from javax.swing import (JOptionPane, JPanel, JLabel, JTextField,
                         BorderFactory)
from java.awt import GridBagLayout, GridBagConstraints, Insets

from org.openpnp.model import Configuration, LengthUnit, Length, Location
from org.openpnp.util import MovableUtils

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

DIALOG_TITLE  = "Load Feeders"
DIALOG_WIDTH  = 360
SEGMENT_COUNT = 3
HOLDER_MM     = 160.0
SEGMENT_MM    = HOLDER_MM / SEGMENT_COUNT   # ≈ 53.33

VALID_WIDTHS      = {8, 12, 16}
VALID_THICKNESSES = {35, 70, 100}
VALID_COLOURS     = {"B", "W", "C"}
VALID_PITCHES     = {2, 4, 8}

CONFIG_FILE = os.path.expanduser(
    "~/.openpnp2/scripts/illysky/holder_config.json")

DEFAULT_CONFIG = {
    "start_x":    6.628,
    "start_y":    230.016,
    "z":          4.2,
    "spacing_x":  10.0,
    "segment_mm": SEGMENT_MM,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(text):
    html = "<html><div style='width:{}px'>{}</div></html>".format(
        DIALOG_WIDTH, text.replace("\n", "<br>"))
    return JLabel(html)


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
# Holder config persistence
# ---------------------------------------------------------------------------

def _load_config():
    """Load holder_config.json; return None if missing or corrupt."""
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        # Validate required keys
        for k in DEFAULT_CONFIG:
            if k not in data:
                return None
        return data
    except Exception:
        return None


def _save_config(cfg_data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg_data, f, indent=2)


def _config_dialog(existing=None):
    """Show the holder layout config form.  Returns dict or None on cancel."""
    src = existing if existing else DEFAULT_CONFIG

    tf_sx  = JTextField(str(src["start_x"]),    8)
    tf_sy  = JTextField(str(src["start_y"]),    8)
    tf_z   = JTextField(str(src["z"]),          8)
    tf_sp  = JTextField(str(src["spacing_x"]),  8)
    tf_seg = JTextField(str(src["segment_mm"]), 8)

    panel = JPanel(GridBagLayout())
    panel.setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8))
    gbc = GridBagConstraints()
    gbc.fill = GridBagConstraints.HORIZONTAL
    gbc.weightx = 1.0

    _add_form_row(panel, gbc, "Start X (mm):",       tf_sx,  0)
    _add_form_row(panel, gbc, "Start Y (mm):",       tf_sy,  1)
    _add_form_row(panel, gbc, "Z height (mm):",      tf_z,   2)
    _add_form_row(panel, gbc, "Holder spacing X (mm):", tf_sp, 3)
    _add_form_row(panel, gbc, "Segment length Y (mm):", tf_seg, 4)

    ok = JOptionPane.showConfirmDialog(None, panel,
         DIALOG_TITLE + " -- Holder Layout",
         JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE)
    if ok != JOptionPane.OK_OPTION:
        return None

    try:
        return {
            "start_x":    float(tf_sx.getText().strip()),
            "start_y":    float(tf_sy.getText().strip()),
            "z":          float(tf_z.getText().strip()),
            "spacing_x":  float(tf_sp.getText().strip()),
            "segment_mm": float(tf_seg.getText().strip()),
        }
    except ValueError:
        JOptionPane.showMessageDialog(None,
            _msg("All values must be numbers."),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return None


# ---------------------------------------------------------------------------
# Tape spec parsing
# ---------------------------------------------------------------------------

def _parse_tape_spec(spec):
    """Return (width, thickness_int, colour, pitch) or None if blank.
    Raises ValueError with message on invalid format."""
    if not spec or not spec.strip():
        return None
    p = spec.strip().split("-")
    if len(p) != 4:
        raise ValueError("'{}' -- expected 4 fields".format(spec))
    try:
        w  = int(p[0]); t = int(p[1]); c = p[2].upper(); pt = int(p[3])
    except ValueError:
        raise ValueError("'{}' -- non-numeric field".format(spec))
    if w  not in VALID_WIDTHS:
        raise ValueError("'{}' -- width {} not in {}".format(spec, w, sorted(VALID_WIDTHS)))
    if t  not in VALID_THICKNESSES:
        raise ValueError("'{}' -- thickness {} not in {}".format(spec, t, sorted(VALID_THICKNESSES)))
    if c  not in VALID_COLOURS:
        raise ValueError("'{}' -- colour {} not in {}".format(spec, c, sorted(VALID_COLOURS)))
    if pt not in VALID_PITCHES:
        raise ValueError("'{}' -- pitch {} not in {}".format(spec, pt, sorted(VALID_PITCHES)))
    return w, t, c, float(pt)


# ---------------------------------------------------------------------------
# Holder allocation
# ---------------------------------------------------------------------------

def _allocate(feeders_info, segment_mm):
    """Bin-pack feeders into holders.

    feeders_info: list of dicts with keys:
      feeder, part_id, pkg_id, spec_str, width, thickness, pitch, cut_length

    Returns the same list with added keys:
      holder_key  -- (width, thickness) string for labelling
      holder_idx  -- physical holder index (global, across all widths)
      seg_start   -- first segment index within this holder (0-2)
      segs_used   -- number of segments this tape occupies

    Also returns a list of (holder_key, holder_idx) tuples in physical order
    so we can label them.
    """
    # Group by (width, thickness); sort so narrower/thinner holders come first
    from collections import OrderedDict
    groups = OrderedDict()
    for fi in sorted(feeders_info, key=lambda x: (x["width"], x["thickness"], x["part_id"])):
        key = (fi["width"], fi["thickness"])
        groups.setdefault(key, []).append(fi)

    holder_counter = [0]   # global holder index, shared across groups
    all_holders    = []    # (key_label, holder_idx) for display

    for key, group in groups.items():
        w, t  = key
        label = "{}mm-{}".format(w, t)
        # Running holder for this group
        cur_holder_idx  = None
        cur_slots_left  = 0

        for fi in group:
            segs = max(1, int(math.ceil(fi["cut_length"] / segment_mm)))
            segs = min(segs, SEGMENT_COUNT)   # cap at 3

            if cur_holder_idx is None or segs > cur_slots_left:
                # Start a new physical holder
                cur_holder_idx = holder_counter[0]
                holder_counter[0] += 1
                cur_slots_left = SEGMENT_COUNT
                all_holders.append((label, cur_holder_idx))

            seg_start = SEGMENT_COUNT - cur_slots_left
            fi["holder_key"] = label
            fi["holder_idx"] = cur_holder_idx
            fi["seg_start"]  = seg_start
            fi["segs_used"]  = segs
            cur_slots_left  -= segs

    return feeders_info, all_holders


# ---------------------------------------------------------------------------
# Camera movement
# ---------------------------------------------------------------------------

def _move_camera_to_holder(camera, config, holder_idx, seg_start):
    """Move camera to the approximate position of a holder segment."""
    seg_mm = config["segment_mm"]
    x = config["start_x"] + holder_idx * config["spacing_x"]
    y = config["start_y"] + seg_start  * seg_mm
    z = config["z"]
    target = Location(LengthUnit.Millimeters, x, y, z, 90.0)
    try:
        MovableUtils.moveToLocationAtSafeZ(camera, target)
        return True
    except Exception as e:
        JOptionPane.showMessageDialog(None,
            _msg("Camera move failed:\n{}".format(e)),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return False


def _capture_hole(camera, prompt):
    """Show a prompt dialog, then capture the camera's current location.
    Returns Location or None on cancel."""
    ok = JOptionPane.showConfirmDialog(None,
         _msg(prompt),
         DIALOG_TITLE,
         JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE)
    if ok != JOptionPane.OK_OPTION:
        return None
    try:
        return camera.getLocation()
    except Exception as e:
        JOptionPane.showMessageDialog(None,
            _msg("Could not read camera position:\n{}".format(e)),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return None


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

    cfg     = Configuration.get()
    machine = cfg.getMachine()

    # Get camera for movement
    try:
        head   = machine.getDefaultHead()
        camera = head.getDefaultCamera()
    except Exception as e:
        JOptionPane.showMessageDialog(None,
            _msg("Could not get camera:\n{}".format(e)),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Holder config
    # ------------------------------------------------------------------
    existing_config = _load_config()

    if existing_config is None:
        # First run — must configure
        JOptionPane.showMessageDialog(None,
            _msg("No holder layout configured yet.\n"
                 "Please enter the holder positions on the next screen."),
            DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)
        holder_cfg = _config_dialog()
        if holder_cfg is None:
            return
        _save_config(holder_cfg)
    else:
        OPTS = ["Use existing", "Reconfigure", "Cancel"]
        choice = JOptionPane.showOptionDialog(
            None,
            _msg("Holder layout: start ({}, {})  spacing {}mm  seg {}mm".format(
                existing_config["start_x"], existing_config["start_y"],
                existing_config["spacing_x"], existing_config["segment_mm"])),
            DIALOG_TITLE,
            JOptionPane.DEFAULT_OPTION, JOptionPane.QUESTION_MESSAGE,
            None, OPTS, OPTS[0])
        if choice == 2 or choice == JOptionPane.CLOSED_OPTION:
            return
        if choice == 1:
            holder_cfg = _config_dialog(existing_config)
            if holder_cfg is None:
                return
            _save_config(holder_cfg)
        else:
            holder_cfg = existing_config

    seg_mm = holder_cfg["segment_mm"]

    # ------------------------------------------------------------------
    # Read feeders and parse tape specs
    # ------------------------------------------------------------------
    feeders_info = []
    skipped      = []

    for feeder in machine.getFeeders():
        if not isinstance(feeder, ReferenceStripFeeder):
            continue

        part = feeder.getPart()
        if part is None:
            skipped.append("{} (no part)".format(feeder.getName()))
            continue

        part_id = part.getId()
        pkg     = part.getPackage()
        pkg_id  = pkg.getId() if pkg else ""

        spec_str = ""
        try:
            ts = pkg.getTapeSpecification() if pkg else None
            if ts:
                spec_str = ts.strip()
        except Exception:
            pass

        try:
            parsed = _parse_tape_spec(spec_str)
        except ValueError as e:
            skipped.append("{}: invalid tape spec {}".format(part_id, e))
            continue

        if parsed is None:
            skipped.append("{} (no tape spec)".format(part_id))
            continue

        width, thickness, colour, pitch = parsed

        max_count  = 0
        cut_length = 0.0
        try:
            max_count = feeder.getMaxFeedCount()
            pp = feeder.getPartPitch()
            if pp is not None:
                pitch_mm = pp.convertToUnits(LengthUnit.Millimeters).getValue()
                cut_length = max_count * pitch_mm
            else:
                cut_length = max_count * pitch
        except Exception:
            cut_length = max_count * pitch

        feeders_info.append({
            "feeder":      feeder,
            "part_id":     part_id,
            "pkg_id":      pkg_id,
            "spec_str":    spec_str,
            "width":       width,
            "thickness":   thickness,
            "colour":      colour,
            "pitch":       pitch,
            "cut_length":  cut_length,
            "max_count":   max_count,
        })

    if not feeders_info:
        msg = "No feeders with a valid tape spec found.\nRun Create Parts then Create Feeders first."
        if skipped:
            msg += "\n\nSkipped:\n" + "\n".join(skipped[:10])
        JOptionPane.showMessageDialog(None, _msg(msg),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return

    # ------------------------------------------------------------------
    # Allocate to holders
    # ------------------------------------------------------------------
    feeders_info, all_holders = _allocate(feeders_info, seg_mm)

    # Sort feeders into load order: holder_idx, then seg_start
    feeders_info.sort(key=lambda x: (x["holder_idx"], x["seg_start"]))

    # ------------------------------------------------------------------
    # Summary dialog
    # ------------------------------------------------------------------
    n_holders = len(all_holders)
    lines = [
        "{} feeders allocated to {} holder(s)".format(len(feeders_info), n_holders),
    ]
    # Show holder breakdown
    holder_lines = []
    prev_h = None
    for fi in feeders_info:
        if fi["holder_idx"] != prev_h:
            holder_lines.append(
                "  H{:02d} [{}]: {}".format(
                    fi["holder_idx"] + 1, fi["holder_key"], fi["part_id"]))
            prev_h = fi["holder_idx"]
        else:
            holder_lines.append("        + {}".format(fi["part_id"]))

    if len(holder_lines) <= 20:
        lines += holder_lines
    else:
        lines += holder_lines[:18]
        lines.append("  ... and {} more".format(len(holder_lines) - 18))

    if skipped:
        lines.append("\n{} skipped (no/invalid tape spec)".format(len(skipped)))

    ok = JOptionPane.showConfirmDialog(None,
         _msg("\n".join(lines)),
         DIALOG_TITLE,
         JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE)
    if ok != JOptionPane.OK_OPTION:
        return

    # ------------------------------------------------------------------
    # Interactive load loop
    # ------------------------------------------------------------------
    LOAD_OPTS = ["Load", "Skip", "Exit"]
    loaded = 0
    skipped_load = 0

    for fi in feeders_info:
        feeder     = fi["feeder"]
        part_id    = fi["part_id"]
        pkg_id     = fi["pkg_id"]
        spec_str   = fi["spec_str"]
        cut_length = fi["cut_length"]
        h_num      = fi["holder_idx"] + 1
        seg_s      = fi["seg_start"]
        segs       = fi["segs_used"]
        seg_label  = "seg {}-{}".format(seg_s + 1, seg_s + segs)

        info = (
            "Part:   {}\n"
            "Pkg:    {}\n"
            "Tape:   {}\n"
            "Cut:    {:.0f} mm  ({} parts)\n"
            "Holder: H{:02d} [{}]  {}".format(
                part_id, pkg_id, spec_str,
                cut_length, fi["max_count"],
                h_num, fi["holder_key"], seg_label)
        )

        choice = JOptionPane.showOptionDialog(
            None, _msg(info), DIALOG_TITLE,
            JOptionPane.DEFAULT_OPTION, JOptionPane.PLAIN_MESSAGE,
            None, LOAD_OPTS, LOAD_OPTS[0])

        if choice == 2 or choice == JOptionPane.CLOSED_OPTION:
            break   # Exit

        if choice == 1:
            skipped_load += 1
            continue   # Skip

        # ---- Load ----

        # 1. Move camera to holder segment position
        moved = _move_camera_to_holder(
            camera, holder_cfg, fi["holder_idx"], seg_s)
        if not moved:
            # Movement failed; offer to continue anyway or abort
            cont = JOptionPane.showConfirmDialog(None,
                _msg("Camera move failed. Continue with manual positioning?"),
                DIALOG_TITLE,
                JOptionPane.YES_NO_OPTION, JOptionPane.WARNING_MESSAGE)
            if cont != JOptionPane.YES_OPTION:
                break

        # 2. Capture reference hole 1
        hole1 = _capture_hole(
            camera,
            "Load the tape, then jog the camera to\n"
            "REFERENCE HOLE 1 and click OK.\n\n"
            "Part: {}  Holder H{:02d} {}".format(part_id, h_num, seg_label))
        if hole1 is None:
            skipped_load += 1
            continue

        # 3. Capture reference hole 2
        hole2 = _capture_hole(
            camera,
            "Jog the camera to REFERENCE HOLE 2\n"
            "(next sprocket hole along the tape) and click OK.\n\n"
            "Part: {}  Holder H{:02d} {}".format(part_id, h_num, seg_label))
        if hole2 is None:
            skipped_load += 1
            continue

        # 4. Apply to feeder and enable
        try:
            feeder.setReferenceHoleLocation(hole1)
            feeder.setLastHoleLocation(hole2)
            feeder.setFeedCount(0)
            feeder.setEnabled(True)
            cfg.save()
            loaded += 1
        except Exception as e:
            JOptionPane.showMessageDialog(None,
                _msg("Failed to configure feeder {}:\n{}".format(
                    feeder.getName(), e)),
                DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    JOptionPane.showMessageDialog(None,
        _msg("{} feeders loaded and enabled\n"
             "{} skipped".format(loaded, skipped_load)),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


run()
