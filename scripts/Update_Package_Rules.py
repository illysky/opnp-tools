"""
Update Package Rules
====================
OpenPnP Jython script — appears in the Scripts menu automatically.

Scans all packages currently in OpenPnP and, for each one that has been
manually configured (nozzle tip, height > 0, or body dimensions > 0),
finds the matching rule in package_rules.json and updates it with the
current values from OpenPnP.

Run this after tweaking any package in the OpenPnP UI so that future
"Config Parts from Board" runs use your refined settings.
"""

from __future__ import absolute_import
import os, re, json, shutil
from datetime import datetime

from javax.swing import JOptionPane

from org.openpnp.model import Configuration, LengthUnit

RULES_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "package_rules.json")

FIDUCIAL_PATTERN = re.compile(r"fiducial|fidhole|fid\b", re.IGNORECASE)

# ---------------------------------------------------------------------------

def _load_rules():
    with open(RULES_FILE) as f:
        return json.load(f)


def _save_rules(data):
    # Backup first
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = RULES_FILE + "." + ts + ".bak"
    shutil.copy2(RULES_FILE, bak)
    with open(RULES_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return bak


def _get_nozzle_name(pkg, machine_nozzle_id_to_name):
    """Return the human-readable nozzle tip name for the first compatible tip."""
    try:
        # Use reflection to read compatibleNozzleTipIds
        cls = pkg.getClass()
        while cls is not None:
            for field in cls.getDeclaredFields():
                if field.getName() == "compatibleNozzleTipIds":
                    field.setAccessible(True)
                    ids = field.get(pkg)
                    if ids and not ids.isEmpty():
                        tip_id = ids.get(0)
                        return machine_nozzle_id_to_name.get(tip_id, tip_id)
                    return None
            cls = cls.getSuperclass()
    except Exception:
        pass
    return None


def _build_nozzle_id_to_name(machine_xml_path):
    """Parse machine.xml → {id: name} for all nozzle tips."""
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


# ---------------------------------------------------------------------------

def run():
    cfg = Configuration.get()

    config_dir        = cfg.getConfigurationDirectory().getAbsolutePath()
    machine_xml_path  = os.path.join(config_dir, "machine.xml")
    id_to_name        = _build_nozzle_id_to_name(machine_xml_path)

    if not os.path.exists(RULES_FILE):
        JOptionPane.showMessageDialog(None,
            "package_rules.json not found at:\n" + RULES_FILE,
            "Update Package Rules", JOptionPane.ERROR_MESSAGE)
        return

    data  = _load_rules()
    rules = data.get("rules", [])

    n_updated = 0
    n_added   = 0

    # Specific rules = all rules except the catch-all (pattern == "")
    specific_rules = [r for r in rules if r.get("pattern", "")]
    catchall_rule  = next((r for r in rules if not r.get("pattern", "")), None)

    for pkg in cfg.getPackages():
        pkg_id = pkg.getId()
        if FIDUCIAL_PATTERN.search(pkg_id):
            continue

        # Read current state from OpenPnP
        nozzle_name = _get_nozzle_name(pkg, id_to_name)

        height_mm = 0.0
        try:
            h = pkg.getHeight()
            if h is not None:
                height_mm = h.convertToUnits(LengthUnit.Millimeters).getValue()
        except Exception:
            pass

        bw, bh = 0.0, 0.0
        try:
            fp = pkg.getFootprint()
            if fp is not None:
                bw = fp.getBodyWidth()
                bh = fp.getBodyHeight()
        except Exception:
            pass

        # Find matching specific rule
        matched_rule = None
        for rule in specific_rules:
            if re.search(rule["pattern"], pkg_id, re.IGNORECASE):
                matched_rule = rule
                break

        if matched_rule is not None:
            # Update the existing rule with any configured values
            if not nozzle_name and height_mm == 0.0 and bw == 0.0 and bh == 0.0:
                continue
            changed = False
            if nozzle_name and matched_rule.get("nozzle") != nozzle_name:
                matched_rule["nozzle"]         = nozzle_name;  changed = True
            if height_mm > 0.0 and matched_rule.get("height_mm", 0.0) != height_mm:
                matched_rule["height_mm"]      = round(height_mm, 3); changed = True
            if bw > 0.0 and matched_rule.get("body_width_mm", 0.0) != bw:
                matched_rule["body_width_mm"]  = round(bw, 3); changed = True
            if bh > 0.0 and matched_rule.get("body_height_mm", 0.0) != bh:
                matched_rule["body_height_mm"] = round(bh, 3); changed = True
            if changed:
                n_updated += 1

        else:
            # Only add a new rule if the package is fully configured —
            # nozzle assigned AND body dimensions set. That way we know
            # the user has deliberately set it up in OpenPnP.
            if not nozzle_name or bw == 0.0 or bh == 0.0:
                continue

            new_rule = {
                "pattern":        re.escape(pkg_id),
                "nozzle":         nozzle_name,
                "height_mm":      round(height_mm, 3) if height_mm > 0.0 else 1.0,
                "body_width_mm":  round(bw, 3),
                "body_height_mm": round(bh, 3),
            }
            # Insert before the catch-all so it is evaluated first
            if catchall_rule and catchall_rule in rules:
                rules.insert(rules.index(catchall_rule), new_rule)
            else:
                rules.append(new_rule)
            specific_rules.append(new_rule)
            n_added += 1

    if n_updated == 0 and n_added == 0:
        JOptionPane.showMessageDialog(None,
            "No changes needed — rules already match the current package configuration.",
            "Update Package Rules", JOptionPane.INFORMATION_MESSAGE)
        return

    bak = _save_rules(data)

    JOptionPane.showMessageDialog(None,
        "{} rule(s) updated, {} new rule(s) added.\nBackup: {}".format(n_updated, n_added, bak),
        "Update Package Rules — Done", JOptionPane.INFORMATION_MESSAGE)


run()
