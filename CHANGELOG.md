# pnp_creator.py — Changelog

## Session: Strip Feeder Redesign + PDF Overhaul

### Core Script Changes

#### Feeder Model
- Replaced block-based feeder model with `_Feeder` class supporting up to 3 tape segments per 160 mm physical feeder at snap positions 0 / 50 / 100 mm
- Packing respects `tape_mm` and `thickness` constraints
- Components requiring > 160 mm are split across multiple physical feeders
- Passive continuation chunks padded to minimum 25 mm (`_MIN_VF_MM`) to avoid tiny cuts

#### Tape / BOM Calculation
- Removed 16 mm clip allowance from tape length — no longer needed
- `min_strip_mm` (default 34 mm) now applies **only** to C / R / L passives
- Attrition also restricted to C / R / L passives
- `qty_required` (boards × qty_per_board) and `qty_total` (with attrition) tracked separately
- `qty_loaded` (physical pockets = chunk_mm ÷ pitch_mm) written to `max-feed-count` in machine.xml

#### Tape Rules (`tape_rules.json`)
- Added `thickness` field to all rules
- Skip rules moved to top for priority evaluation
- New/updated rules:
  - WLCSP → 8 mm tape, thin
  - DA7212, HC-1.2-3PWT, TYPE-C-31-M-12 → 12 mm tape, thin
  - nRF5340 (QFN-98) → 16 mm tape
  - 20854 → 12 mm tape
- Unknown packages default to 8 mm / 4 mm pitch / thin (packed but flagged as `defaulted`)

#### Part Labels
- `_part_label()` extracts `Value-Footprint` using `package_id` for reliable parsing
- Truncates value and footprint independently to 10 chars, trailing punctuation stripped

---

### PDF Map Changes

#### Page Layout
- Output is now **A4 portrait** (595 × 842 pt), multi-page with automatic page breaks
- One physical feeder per row, filling the full page width
- Housing extends from 8 pt inset on both left and right edges
- Tape content anchored at `MARGIN = 32 pt` from left, extending to `HOUSING_INSET = 8 pt` from right
- Group headers and title block align with housing left edge (8 pt)
- Continuation pages start content 10 pt from top; bottom reserve is 12 pt

#### Feeder Housing
- Rounded rectangle background with 2.5 pt corner radius
- Physical feeder number (`01`, `02`, …) centred in the left label zone
- No emboss / highlight effect

#### Tape Strips
- Sprocket holes start 2 mm from each tape's own left edge, then every 4 mm
- Tiny gap (`TAPE_INSET = 2 pt`) between adjacent tapes on the same feeder
- Passives: white tape, dark text, component-colour identification bar
- Non-passives: flat dark grey tape, white text, component-colour bar
- Text not clipped — can overflow into housing area for readability

#### Text on Tape
- Line 1 (bold): `01-A: Value-Footprint`
- Line 2: `Qty: N | NNmm`
- Font sizes derived from 8 mm tape geometry and applied uniformly to all tape widths
- Text positioned top-down from just below the colour bar

#### Title Block (page 1)
- `Feeder Map  |  filename.csv` — 14 pt bold
- `Boards: N` — 10 pt
- `Components Allocated: packed/total` — 10 pt

#### Group Headers
- `Nmm Feeders  |  Qty N` — repeated at top of continuation page if group spans a page break

#### Footer (last page)
- OVERFLOW parts listed if any feeder exceeded capacity
- Hand-place (skipped) parts listed
- Defaulted-to-8mm parts listed
