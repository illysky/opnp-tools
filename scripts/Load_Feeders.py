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
                         BorderFactory, SwingUtilities)
from java.awt import GridBagLayout, GridBagConstraints, Insets
from java.lang import Thread as JThread, Runnable

from org.openpnp.model import Configuration, LengthUnit, Length, Location
from org.openpnp.util import MovableUtils, OpenCvUtils
from org.openpnp.util.UiUtils import submitUiMachineTask
from org.openpnp.gui import MainFrame

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
    "spacing_x":  12.0,
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
# Camera movement and auto hole finding
# ---------------------------------------------------------------------------

# EIA-481 sprocket hole dimensions (all standard tape widths)
SPROCKET_HOLE_DIA_MIN = Length(1.2, LengthUnit.Millimeters)
SPROCKET_HOLE_DIA_MAX = Length(1.8, LengthUnit.Millimeters)
SPROCKET_HOLE_MIN_DIST = Length(3.0, LengthUnit.Millimeters)
SPROCKET_HOLE_PITCH    = 4.0   # mm — fixed for all EIA-481 tapes
SPROCKET_PITCH_TOL     = 0.6   # mm — tolerance on expected 4mm pitch
SCAN_STEP_MM           = 4.0   # step size when scanning along Y
SCAN_MAX_MM            = 60.0  # maximum scan distance from start


def _move_direct(camera, target):
    """Move camera directly — call only from inside a machine task thread."""
    MovableUtils.moveToLocationAtSafeZ(camera, target)


def _move_camera_to_holder_task(camera, config, holder_idx, seg_start):
    """Submit a single machine task to move to a holder segment.
    Blocks until complete.  Returns True/False."""
    x = config["start_x"] + holder_idx * config["spacing_x"]
    y = config["start_y"] + seg_start  * config["segment_mm"]
    z = config["z"]
    target = Location(LengthUnit.Millimeters, x, y, z, 90.0)

    def do_move():
        _move_direct(camera, target)

    try:
        submitUiMachineTask(do_move).get()
        return True
    except Exception as e:
        JOptionPane.showMessageDialog(None,
            _msg("Camera move failed:\n{}".format(e)),
            DIALOG_TITLE, JOptionPane.WARNING_MESSAGE)
        return False


def _push_frame_to_ui(camera, img):
    """Schedule a camera frame update on the EDT (fire-and-forget)."""
    captured_img = img
    captured_cam = camera
    class Push(Runnable):
        def run(self):
            try:
                view = MainFrame.get().getCameraViews().getCameraView(captured_cam)
                if view is not None:
                    view.frameReceived(captured_img)
            except Exception:
                pass
    SwingUtilities.invokeLater(Push())


def _find_holes_at_current_pos(camera):
    """Capture frame, push to UI, run HoughCircles.
    Must be called from the machine task thread (direct camera access)."""
    try:
        img = camera.lightSettleAndCapture()
        if img is not None:
            _push_frame_to_ui(camera, img)
    except Exception:
        pass
    try:
        circles = list(OpenCvUtils.houghCircles(
            camera,
            SPROCKET_HOLE_DIA_MIN,
            SPROCKET_HOLE_DIA_MAX,
            SPROCKET_HOLE_MIN_DIST))
        circles.sort(key=lambda c: c.getY())
        return circles
    except Exception:
        return []


SPROCKET_X_TOL = 0.5   # mm — two sprocket holes must share the same X (same edge)


GAP_THRESHOLD_MM = 5.0   # no holes for this distance = end of a tape zone


def _set_status(msg):
    """Print scan status to the OpenPnP log/console (visible in the UI footer)."""
    print("[Load Feeders] " + msg)


def _all_hole_pairs(circles):
    """Return ALL valid (hole1, hole2) pairs from circles, sorted by hole1.Y ascending.
    Scanning the full range and taking the lowest-Y pair ensures we get the
    beginning of the tape (pick end) even if detection is easier near the tail."""
    pairs = []
    for i in range(len(circles)):
        for j in range(len(circles)):
            if i == j:
                continue
            c1, c2 = circles[i], circles[j]
            dy = c2.getY() - c1.getY()
            dx = abs(c2.getX() - c1.getX())
            if dy > 0 and abs(dy - SPROCKET_HOLE_PITCH) <= SPROCKET_PITCH_TOL \
                      and dx <= SPROCKET_X_TOL:
                pairs.append((c1, c2))
    pairs.sort(key=lambda p: p[0].getY())
    return pairs


def _auto_find_holes(camera, start_x, start_y, z):
    """Scan the FULL segment range in +Y, collect every valid hole pair, then
    return the pair closest to start_y (the pick/beginning end of the tape).
    Runs inside a single machine task so the EDT stays free to repaint."""
    all_pairs   = []
    total_steps = int(math.ceil(SCAN_MAX_MM / SCAN_STEP_MM)) + 1

    def scan():
        for step in range(total_steps):
            scan_y = start_y + step * SCAN_STEP_MM
            _set_status("Scanning step {}/{} at Y={:.1f}mm ...".format(
                step + 1, total_steps, scan_y))
            target = Location(LengthUnit.Millimeters, start_x, scan_y, z, 90.0)
            try:
                _move_direct(camera, target)
            except Exception:
                continue
            circles = _find_holes_at_current_pos(camera)
            _set_status("  {} circle(s) found".format(len(circles)))
            for pair in _all_hole_pairs(circles):
                # Avoid duplicate pairs (same Y within 1mm)
                if not any(abs(p[0].getY() - pair[0].getY()) < 1.0 for p in all_pairs):
                    all_pairs.append(pair)
                    _set_status("  Candidate pair Y={:.2f} / Y={:.2f}".format(
                        pair[0].getY(), pair[1].getY()))

        if all_pairs:
            best = all_pairs[0]   # already sorted by Y — lowest = beginning of tape
            _set_status("Best pair (closest to pick end): Y={:.2f} / Y={:.2f}".format(
                best[0].getY(), best[1].getY()))
        else:
            _set_status("Scan complete — no hole pair found in {:.0f}mm".format(SCAN_MAX_MM))

    try:
        submitUiMachineTask(scan).get()
    except Exception as e:
        _set_status("Scan task error: {}".format(e))

    if all_pairs:
        return all_pairs[0]
    return None, None


# ---------------------------------------------------------------------------
# Full-holder auto scan
# ---------------------------------------------------------------------------

def _scan_holder(camera, nominal_x, start_y, scan_mm, z):
    """Scan an entire holder in one machine task.
    Self-corrects X from the first detected hole pair (3D-print drift compensation).
    Returns list of (scan_y, pairs) for every step."""
    detections  = []
    total_steps = int(math.ceil(scan_mm / SCAN_STEP_MM)) + 1
    current_x   = [nominal_x]   # mutable so inner fn can update it

    def scan():
        for step in range(total_steps):
            scan_y = start_y + step * SCAN_STEP_MM
            target = Location(LengthUnit.Millimeters, current_x[0], scan_y, z, 90.0)
            try:
                _move_direct(camera, target)
            except Exception:
                detections.append((scan_y, []))
                continue
            circles = _find_holes_at_current_pos(camera)
            pairs   = _all_hole_pairs(circles)
            detections.append((scan_y, pairs))
            _set_status("H x={:.2f} Y={:.1f}: {} circle(s) {} pair(s)".format(
                current_x[0], scan_y, len(circles), len(pairs)))

            # X self-correction: use first detected pair to recalibrate X
            if pairs and current_x[0] == nominal_x:
                h1, h2   = pairs[0]
                actual_x = (h1.getX() + h2.getX()) / 2.0
                drift    = actual_x - nominal_x
                current_x[0] = actual_x
                _set_status("  X corrected: nominal {:.2f} → actual {:.2f} (drift {:.2f}mm)".format(
                    nominal_x, actual_x, drift))

    submitUiMachineTask(scan).get()
    return detections


def _zones_from_detections(detections):
    """Convert a flat list of (y, pairs) into tape zones.
    A zone ends when there are no pairs for >= GAP_THRESHOLD_MM.
    Returns list of (zone_start_y, best_hole1, best_hole2) sorted by zone_start_y."""
    zones          = []
    zone_pairs     = []     # all pairs seen in current zone
    zone_start     = None
    last_active_y  = None

    for scan_y, pairs in detections:
        if pairs:
            if zone_start is None:
                zone_start = scan_y
            last_active_y = scan_y
            for p in pairs:
                if not any(abs(ep[0].getY() - p[0].getY()) < 1.0 for ep in zone_pairs):
                    zone_pairs.append(p)
        else:
            if last_active_y is not None:
                gap = scan_y - last_active_y
                if gap >= GAP_THRESHOLD_MM:
                    # Close current zone
                    if zone_pairs:
                        best = sorted(zone_pairs, key=lambda p: p[0].getY())[0]
                        zones.append((zone_start, best[0], best[1]))
                        _set_status("Zone at Y={:.1f}: best pair {:.2f}/{:.2f}".format(
                            zone_start, best[0].getY(), best[1].getY()))
                    zone_pairs    = []
                    zone_start    = None
                    last_active_y = None

    # Close final zone
    if zone_pairs:
        best = sorted(zone_pairs, key=lambda p: p[0].getY())[0]
        zones.append((zone_start, best[0], best[1]))
        _set_status("Zone at Y={:.1f}: best pair {:.2f}/{:.2f}".format(
            zone_start, best[0].getY(), best[1].getY()))

    return zones


def _loading_guide(feeders_info):
    """Walk the user through loading tapes holder by holder with Back/Next.
    Returns True when user confirms all loaded, False if cancelled."""
    from collections import OrderedDict

    # Build ordered list of holders with their feeders
    holders = OrderedDict()
    for fi in sorted(feeders_info, key=lambda f: (f["holder_idx"], f["seg_start"])):
        holders.setdefault(fi["holder_idx"], []).append(fi)

    pages   = []
    for holder_idx, flist in holders.items():
        key = flist[0]["holder_key"]
        lines = ["<b>H{:02d} &nbsp; {} holder</b><br><br>".format(holder_idx + 1, key)]
        for fi in flist:
            lines.append("&nbsp;&nbsp;• &nbsp;<b>{}</b> &nbsp; {} pcs &nbsp; {:.0f}mm cut &nbsp; [{}]".format(
                fi["part_id"], fi["max_count"], fi["cut_length"], fi["spec_str"]))
        pages.append("<br>".join(lines))

    total = len(pages)
    idx   = 0

    while True:
        is_first = (idx == 0)
        is_last  = (idx == total - 1)

        if is_last:
            btn_labels = ["← Back", "Start Scan", "Cancel"] if not is_first else ["Start Scan", "Cancel"]
        else:
            btn_labels = ["← Back", "Next →", "Cancel"] if not is_first else ["Next →", "Cancel"]

        header = "Loading guide  {}/{}".format(idx + 1, total)
        choice = JOptionPane.showOptionDialog(
            None,
            _msg("Load into holder:<br><br>" + pages[idx]),
            DIALOG_TITLE + "  —  " + header,
            JOptionPane.DEFAULT_OPTION, JOptionPane.PLAIN_MESSAGE,
            None, btn_labels, btn_labels[0])

        if choice == JOptionPane.CLOSED_OPTION:
            return False

        label = btn_labels[choice]
        if label == "Cancel":
            return False
        elif label == "← Back":
            idx -= 1
        elif label == "Next →":
            idx += 1
        elif label == "Start Scan":
            return True


def run_auto(cfg, camera, feeders_info, holder_cfg):
    """Fully automated mode: scan every holder, detect tape zones, configure feeders."""

    # Group feeders by holder_idx, sorted by seg_start within each holder
    from collections import OrderedDict
    by_holder = OrderedDict()
    for fi in sorted(feeders_info, key=lambda f: (f["holder_idx"], f["seg_start"])):
        by_holder.setdefault(fi["holder_idx"], []).append(fi)

    configured = 0
    not_found  = []

    for holder_idx, feeders in by_holder.items():
        x       = holder_cfg["start_x"] + holder_idx * holder_cfg["spacing_x"]
        start_y = holder_cfg["start_y"]
        z       = holder_cfg["z"]
        # Scan the full holder length (all segments)
        scan_mm = HOLDER_MM

        _set_status("Scanning holder {} (x={:.1f}) for {} feeder(s)...".format(
            holder_idx + 1, x, len(feeders)))

        try:
            detections = _scan_holder(camera, x, start_y, scan_mm, z)
        except Exception as e:
            _set_status("Holder {} scan failed: {}".format(holder_idx + 1, e))
            for fi in feeders:
                not_found.append(fi["part_id"])
            continue

        zones = _zones_from_detections(detections)
        _set_status("Holder {}: {} zone(s) found, {} feeder(s) expected".format(
            holder_idx + 1, len(zones), len(feeders)))

        # Match zones to feeders in order
        for i, fi in enumerate(feeders):
            if i >= len(zones):
                _set_status("  No zone for {}".format(fi["part_id"]))
                not_found.append(fi["part_id"])
                continue
            _, hole1, hole2 = zones[i]
            feeder = fi["feeder"]
            try:
                feeder.setReferenceHoleLocation(hole1)
                feeder.setLastHoleLocation(hole2)
                feeder.setFeedCount(0)
                feeder.setEnabled(True)
                configured += 1
                _set_status("  Configured {} at Y={:.2f}".format(
                    fi["part_id"], hole1.getY()))
            except Exception as e:
                _set_status("  Error configuring {}: {}".format(fi["part_id"], e))
                not_found.append(fi["part_id"])

    cfg.save()

    msg = "{} feeder(s) configured automatically.".format(configured)
    if not_found:
        msg += "\n\nNot found / failed ({}):\n{}".format(
            len(not_found), "\n".join(not_found))
    JOptionPane.showMessageDialog(None, _msg(msg),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


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

    # camera via the global 'machine' provided by OpenPnP scripting engine
    try:
        camera = machine.defaultHead.defaultCamera
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
    # Mode selection
    # ------------------------------------------------------------------
    MODE_OPTS = ["Auto Scan", "Interactive", "Cancel"]
    mode = JOptionPane.showOptionDialog(
        None,
        _msg("Auto Scan: load ALL tapes first, script scans every holder\n"
             "and configures feeders without further interaction.\n\n"
             "Interactive: step through each feeder one at a time."),
        DIALOG_TITLE,
        JOptionPane.DEFAULT_OPTION, JOptionPane.QUESTION_MESSAGE,
        None, MODE_OPTS, MODE_OPTS[0])
    if mode == 2 or mode == JOptionPane.CLOSED_OPTION:
        return

    # ------------------------------------------------------------------
    # Read feeders and parse tape specs
    # ------------------------------------------------------------------
    feeders_info = []
    skipped      = []

    for feeder in cfg.getMachine().getFeeders():
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
    # Auto scan mode
    # ------------------------------------------------------------------
    if mode == 0:
        if not _loading_guide(feeders_info):
            return
        run_auto(cfg, camera, feeders_info, holder_cfg)
        return

    # ------------------------------------------------------------------
    # Interactive load loop — holder by holder
    # ------------------------------------------------------------------
    from collections import OrderedDict

    by_holder = OrderedDict()
    for fi in feeders_info:
        by_holder.setdefault(fi["holder_idx"], []).append(fi)

    holder_list = list(by_holder.items())   # [(holder_idx, [fi, ...]), ...]
    total_holders = len(holder_list)
    loaded       = 0
    skipped_load = 0
    h_idx        = 0   # current position in holder_list

    while 0 <= h_idx < total_holders:
        holder_idx, flist = holder_list[h_idx]
        h_num  = holder_idx + 1
        h_key  = flist[0]["holder_key"]
        is_first = (h_idx == 0)
        is_last  = (h_idx == total_holders - 1)

        # Build component list for this holder
        part_lines = []
        for fi in flist:
            seg_label = "seg {}-{}".format(fi["seg_start"] + 1,
                                           fi["seg_start"] + fi["segs_used"])
            part_lines.append(
                "&nbsp;&nbsp;• &nbsp;<b>{}</b>"
                " &nbsp; {} pcs &nbsp; {:.0f}mm cut"
                " &nbsp; [{}] &nbsp; <i>{}</i>".format(
                    fi["part_id"], fi["max_count"],
                    fi["cut_length"], fi["spec_str"], seg_label))

        body = ("<b>H{:02d} &nbsp; {}</b><br><br>"
                "Load these tape(s), then click Scan:<br><br>"
                "{}".format(h_num, h_key, "<br>".join(part_lines)))

        if is_last:
            btns = (["← Back", "Scan", "Exit"] if not is_first
                    else ["Scan", "Exit"])
        else:
            btns = (["← Back", "Scan & Next →", "Exit"] if not is_first
                    else ["Scan & Next →", "Exit"])

        choice = JOptionPane.showOptionDialog(
            None, _msg(body),
            "{} — {}/{}".format(DIALOG_TITLE, h_idx + 1, total_holders),
            JOptionPane.DEFAULT_OPTION, JOptionPane.PLAIN_MESSAGE,
            None, btns, btns[0])

        if choice == JOptionPane.CLOSED_OPTION or btns[choice] == "Exit":
            break

        if btns[choice] == "← Back":
            h_idx -= 1
            continue

        # ---- Scan this holder ----
        x_holder = holder_cfg["start_x"] + holder_idx * holder_cfg["spacing_x"]
        start_y  = holder_cfg["start_y"]
        z_cfg    = holder_cfg["z"]

        _set_status("Interactive scan: holder {} (x={:.2f})".format(h_num, x_holder))

        try:
            detections = _scan_holder(camera, x_holder, start_y, HOLDER_MM, z_cfg)
        except Exception as e:
            JOptionPane.showMessageDialog(None,
                _msg("Scan failed for H{:02d}:\n{}".format(h_num, e)),
                DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
            h_idx += 1
            continue

        zones = _zones_from_detections(detections)
        _set_status("H{:02d}: {} zone(s) found, {} feeder(s)".format(
            h_num, len(zones), len(flist)))

        for i, fi in enumerate(flist):
            if i >= len(zones):
                _set_status("  No zone for {}".format(fi["part_id"]))
                skipped_load += 1
                continue
            _, hole1, hole2 = zones[i]
            feeder = fi["feeder"]
            try:
                feeder.setReferenceHoleLocation(hole1)
                feeder.setLastHoleLocation(hole2)
                feeder.setFeedCount(0)
                feeder.setEnabled(True)
                loaded += 1
                _set_status("  Configured {}".format(fi["part_id"]))
            except Exception as e:
                JOptionPane.showMessageDialog(None,
                    _msg("Failed to configure {}:\n{}".format(fi["part_id"], e)),
                    DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
                skipped_load += 1

        cfg.save()
        h_idx += 1

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    JOptionPane.showMessageDialog(None,
        _msg("{} feeders loaded and enabled\n"
             "{} skipped".format(loaded, skipped_load)),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


# If called on the Swing EDT (which OpenPnP scripts typically are),
# relaunch in a background thread so the EDT stays free to repaint
# the camera view while machine operations are running.
if SwingUtilities.isEventDispatchThread():
    class _Runner(Runnable):
        def run(self):
            run()
    t = JThread(_Runner())
    t.setDaemon(True)
    t.start()
else:
    run()
