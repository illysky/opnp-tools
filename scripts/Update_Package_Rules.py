"""
Update Package Rules
====================
OpenPnP Jython script — appears in the Scripts menu automatically.

Does two passes over package_rules.json:

1. UPDATE PACKAGES — scans all packages in OpenPnP. For each one with a
   nozzle assigned and body dims set, finds the matching rule and updates
   nozzle / body_width_mm / body_height_mm. Adds a new rule for any package
   that has no specific match (nozzle + both dims required).

2. UPDATE PARTS — scans all parts in OpenPnP. For each one with height > 0,
   finds the rule matching its package ID and updates height_mm.

Run this after tweaking packages or part heights in the OpenPnP UI.
"""

from __future__ import absolute_import
import os, re, json

from javax.swing import JOptionPane, JLabel

DIALOG_TITLE = "Update Package Rules"
DIALOG_WIDTH = 300

def _msg(text):
    html = "<html><div style='width:{}px'>{}</div></html>".format(
        DIALOG_WIDTH, text.replace("\n", "<br>"))
    return JLabel(html)

from org.openpnp.model import Configuration, LengthUnit

RULES_FILE = os.path.expanduser("~/.openpnp2/scripts/illysky/package_rules.json")

FIDUCIAL_PATTERN = re.compile(r"fiducial|fidhole|fid\b", re.IGNORECASE)

# ---------------------------------------------------------------------------

def _load_rules():
    with open(RULES_FILE) as f:
        return json.load(f)


def _save_rules(data):
    with open(RULES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _get_nozzle_name(pkg, id_to_name):
    """Return the human-readable nozzle tip name for the first compatible tip."""
    try:
        cls = pkg.getClass()
        while cls is not None:
            for field in cls.getDeclaredFields():
                if field.getName() == "compatibleNozzleTipIds":
                    field.setAccessible(True)
                    ids = field.get(pkg)
                    if ids and not ids.isEmpty():
                        return id_to_name.get(ids.get(0), ids.get(0))
                    return None
            cls = cls.getSuperclass()
    except Exception:
        pass
    return None


def _build_nozzle_id_to_name(machine_xml_path):
    import xml.etree.ElementTree as ET
    mapping = {}
    try:
        tree = ET.parse(machine_xml_path)
        for nt in tree.getroot().iter("nozzle-tip"):
            name = nt.get("name", "").strip()
            tid  = nt.get("id",   "").strip()
            if name and tid:
                mapping[tid] = name
    except Exception:
        pass
    return mapping


def _find_rule(pkg_id, specific_rules):
    """Return the first specific rule matching pkg_id, or None."""
    for rule in specific_rules:
        if re.search(rule["pattern"], pkg_id, re.IGNORECASE):
            return rule
    return None


# ---------------------------------------------------------------------------

def run():
    cfg = Configuration.get()

    config_dir       = cfg.getConfigurationDirectory().getAbsolutePath()
    id_to_name       = _build_nozzle_id_to_name(os.path.join(config_dir, "machine.xml"))

    if not os.path.exists(RULES_FILE):
        JOptionPane.showMessageDialog(None,
            _msg("package_rules.json not found at:\n" + RULES_FILE),
            DIALOG_TITLE, JOptionPane.ERROR_MESSAGE)
        return

    data  = _load_rules()
    rules = data.get("rules", [])

    specific_rules = [r for r in rules if r.get("pattern", "")]
    catchall_rule  = next((r for r in rules if not r.get("pattern", "")), None)

    pkg_updated = 0
    pkg_added   = 0
    part_updated = 0

    # -----------------------------------------------------------------------
    # Pass 1: packages → nozzle, body dims
    # -----------------------------------------------------------------------
    for pkg in cfg.getPackages():
        pkg_id = pkg.getId()
        if FIDUCIAL_PATTERN.search(pkg_id):
            continue

        nozzle_name = _get_nozzle_name(pkg, id_to_name)
        bw, bh = 0.0, 0.0
        try:
            fp = pkg.getFootprint()
            if fp is not None:
                bw = round(fp.getBodyWidth(),  3)
                bh = round(fp.getBodyHeight(), 3)
        except Exception:
            pass

        matched = _find_rule(pkg_id, specific_rules)

        if matched is not None:
            if not nozzle_name and bw == 0.0 and bh == 0.0:
                continue
            changed = False
            if nozzle_name and matched.get("nozzle") != nozzle_name:
                matched["nozzle"]        = nozzle_name; changed = True
            if bw > 0.0 and matched.get("body_width_mm", 0.0) != bw:
                matched["body_width_mm"] = bw;          changed = True
            if bh > 0.0 and matched.get("body_height_mm", 0.0) != bh:
                matched["body_height_mm"]= bh;          changed = True
            if changed:
                pkg_updated += 1

        else:
            # Add new rule only if fully configured
            if not nozzle_name or bw == 0.0 or bh == 0.0:
                continue
            new_rule = {
                "pattern":        re.escape(pkg_id),
                "nozzle":         nozzle_name,
                "height_mm":      1.0,
                "body_width_mm":  bw,
                "body_height_mm": bh,
            }
            if catchall_rule and catchall_rule in rules:
                rules.insert(rules.index(catchall_rule), new_rule)
            else:
                rules.append(new_rule)
            specific_rules.append(new_rule)
            pkg_added += 1

    # -----------------------------------------------------------------------
    # Pass 2: parts → height_mm
    # -----------------------------------------------------------------------
    for part in cfg.getParts():
        part_id = part.getId()
        if FIDUCIAL_PATTERN.search(part_id):
            continue

        height_mm = 0.0
        try:
            h = part.getHeight()
            if h is not None:
                height_mm = round(h.convertToUnits(LengthUnit.Millimeters).getValue(), 3)
        except Exception:
            pass

        if height_mm == 0.0:
            continue

        # Derive package id from part id (comment[:10]-footprint[:10] format)
        pkg = part.getPackage()
        pkg_id = pkg.getId() if pkg is not None else ""
        if not pkg_id:
            continue

        matched = _find_rule(pkg_id, specific_rules)
        if matched is not None and matched.get("height_mm", 0.0) != height_mm:
            matched["height_mm"] = height_mm
            part_updated += 1

    # -----------------------------------------------------------------------
    if pkg_updated == 0 and pkg_added == 0 and part_updated == 0:
        JOptionPane.showMessageDialog(None, _msg("No changes."),
            DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)
        return

    _save_rules(data)

    JOptionPane.showMessageDialog(None,
        _msg("{} Package Added\n{} Package Updated\n{} Part Updated".format(
            pkg_added, pkg_updated, part_updated)),
        DIALOG_TITLE, JOptionPane.INFORMATION_MESSAGE)


run()
