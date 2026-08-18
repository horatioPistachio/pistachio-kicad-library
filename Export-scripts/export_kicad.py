#!/usr/bin/env python3
"""
Export KiCad project artifacts (Gerbers, Excellon, STEP, PDFs, BOM) using kicad-cli.

- Cross-platform (Windows/macOS/Linux)
- Works locally and in CI (GitHub Actions)

Python 3.10+
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - handle missing dependency with message
    yaml = None

# -----------------------------
# Defaults and Config Handling
# -----------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "general": {
        "clean_output": True,
        "zip_gerbers": True,
        "fail_fast": True,
        "kicad_cli": None,  # Optional explicit path to kicad-cli
    },
    "gerbers": {
        "enabled": True,
        "layers": [
            "F.Cu",
            "B.Cu",
            "F.Paste",
            "B.Paste",
            "F.Silkscreen",
            "B.Silkscreen",
            "F.Mask",
            "B.Mask",
            "Edge.Cuts",
        ],
        "drill": {
            "enabled": True,
            "units": "mm",  # mm or inch
            "map_format": "gerber",  # gerber|pdf|svg (support varies by KiCad version)
            "merge_npth": False,
        },
    },
    "pos": {
        "enabled": True,
        # Output format for position files. KiCad supports CSV and TSV commonly.
        "format": "csv",  # csv|tsv (support may vary)
        # Units for coordinates
        "units": "mm",     # mm|inch
        # Which side(s) to export
        "side": "both",    # front|back|both
    },
    "pcb_pdf": {
        "enabled": True,
        "layers": [
            "F.Cu",
            "B.Cu",
            "F.Silkscreen",
            "B.Silkscreen",
            "F.Mask",
            "B.Mask",
            "Edge.Cuts",
        ],
        # First page of the PCB PDF: a single composite plot of the whole board.
        "stack_page": {
            "enabled": True,
            "layers": None,        # None/auto => every layer enabled in the .kicad_pcb
            "exclude_layers": [],  # layer names to drop from the composite
        },
        # Layers carrying process notes (e.g. conformal coating). Forced onto the
        # stack page and plotted last so they draw on top of everything else.
        "notes_layers": [],
        # Opt-in: also give the notes layers their own page in the per-layer sequence.
        "notes_own_page": False,
        "monochrome": False,
        "include_title_block": True,
        "page_size": "A4",
    },
    "schematics_pdf": {
        "enabled": True,
        "monochrome": False,
        "page_size": "A4",
        "include_title_block": True,
    },
    "step": {
        "enabled": True,
        "units": "mm",
        "include_tracks_zones": False,  # Support varies; kept for future use
        "model_precision": "high",
        "ignore_missing_models": True,   # treat missing 3D models as warnings
        "fallback_board_only": True,     # on failure due to missing models, export board-only
    },
    "bom": {
        "enabled": True,
        "method": "cli",  # cli|plugin (fallback)
        "output_format": "csv",
        "plugin": "bom_csv_grouped_by_value",
        "plugin_args": [],
        # Use KiCad's special substitutions for quantity and DNP
        "fields": ["Reference", "${QUANTITY}", "Value", "Footprint", "Supplier", "Supplier Part Number", "${DNP}"],
        "group_by": ["Value", "Footprint"],
        # Scan the schematic hierarchy and add every symbol field found to the
        # BOM, so custom fields (MPN, LCSC, Manufacturer, ...) are never dropped
        # just because they are not listed in `fields`.
        "include_all_fields": True,
        # Field names to keep out of the BOM even if present on symbols.
        "exclude_fields": [],
        # Post-process the CSV: fold together columns whose headers differ only
        # by case/spacing/punctuation (e.g. "MPN" vs "mpn", "Part Number" vs
        # "Part_Number"). Columns with conflicting values are left alone.
        "merge_similar_fields": True,
    },
}


def _deep_update(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_update(a[k], v)
        else:
            a[k] = v
    return a


# -----------------------------
# Utility
# -----------------------------
def _sanitize_label(s: str) -> str:
    """Return a filesystem-friendly label: underscores instead of spaces, only [-._a-zA-Z0-9]."""
    import re

    s = s.replace(" ", "_")
    s = re.sub(r"[^-._a-zA-Z0-9]", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("._-") or "artifact"


INVOKED: List[Dict[str, Any]] = []


def _is_missing_model_err(text: str) -> bool:
    t = (text or "").lower()
    return ("could not add 3d model" in t) or ("file not found:" in t and ".step" in t)


@dataclass
class RunResult:
    code: int
    out: str
    err: str


def run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
        verbose: bool = False) -> RunResult:
    if verbose:
        print("$", " ".join(cmd))
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        out = p.stdout.strip()
        err = p.stderr.strip()
        INVOKED.append({
            "cmd": cmd,
            "cwd": str(cwd) if cwd else None,
            "code": p.returncode,
            "stdout_preview": out[:500],
            "stderr_preview": err[:500],
        })
        return RunResult(p.returncode, out, err)
    except FileNotFoundError as e:
        INVOKED.append({
            "cmd": cmd,
            "cwd": str(cwd) if cwd else None,
            "code": 127,
            "stdout_preview": "",
            "stderr_preview": str(e)[:500],
        })
        return RunResult(127, "", str(e))


def find_kicad_cli(explicit: Optional[str] = None, verbose: bool = False) -> Tuple[str, str]:
    """Return (path, version). Try PATH, then common OS-specific locations."""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    exe = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
    on_path = shutil.which(exe)
    if on_path:
        candidates.append(on_path)

    if os.name == "nt":
        # Try common KiCad 10, 9 and 8 paths
        for ver in ("10.0", "10", "9.0", "9", "8.0", "8"):
            candidates.append(fr"C:/Program Files/KiCad/{ver}/bin/kicad-cli.exe")
    else:
        # macOS Homebrew default and common Linux paths
        candidates += [
            "/usr/local/bin/kicad-cli",
            "/opt/homebrew/bin/kicad-cli",
            "/usr/bin/kicad-cli",
        ]

    tried: List[str] = []
    for c in candidates:
        if not c:
            continue
        tried.append(c)
        if not Path(c).exists():
            continue
        res = run([c, "--version"], verbose=verbose)
        if res.code == 0 and res.out:
            return c, res.out.splitlines()[0]
    # last attempt: just try the name
    res = run([exe, "--version"], verbose=verbose)
    if res.code == 0 and res.out:
        return exe, res.out.splitlines()[0]

    msg = "Could not find 'kicad-cli'. Tried: " + ", ".join(tried or [exe])
    raise FileNotFoundError(msg)


# -----------------------------
# Project detection
# -----------------------------
@dataclass
class Project:
    dir: Path
    name: str
    pro: Path
    pcb: Path
    sch: Path


def detect_project(project_dir: Path, project_name: Optional[str]) -> Project:
    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory does not exist: {project_dir}")

    pro_files = sorted(project_dir.glob("*.kicad_pro"))
    if project_name:
        pro = project_dir / f"{project_name}.kicad_pro"
        if not pro.exists():
            raise FileNotFoundError(f".kicad_pro not found for project_name: {pro}")
    else:
        if len(pro_files) == 0:
            raise FileNotFoundError("No .kicad_pro found in project-dir")
        if len(pro_files) > 1:
            names = ", ".join(p.name for p in pro_files)
            raise ValueError(f"Multiple .kicad_pro files found: {names}. Use --project-name to disambiguate.")
        pro = pro_files[0]
        project_name = pro.stem

    pcb = project_dir / f"{project_name}.kicad_pcb"
    sch = project_dir / f"{project_name}.kicad_sch"

    if not pcb.exists():
        # If board file is elsewhere or named differently, bail with a clear message
        raise FileNotFoundError(f"PCB file not found: {pcb}")
    if not sch.exists():
        # For hierarchical projects, a top-level .kicad_sch should exist
        raise FileNotFoundError(f"Schematic file not found: {sch}")

    return Project(dir=project_dir, name=project_name, pro=pro, pcb=pcb, sch=sch)


# -----------------------------
# Exporters
# -----------------------------

def export_gerbers_and_drill(kicad: str, proj: Project, out_dir: Path, cfg: Dict[str, Any],
                             zip_gerbers: bool, verbose: bool) -> Optional[Path]:
    if not cfg.get("enabled", True):
        return None

    g_dir = out_dir / "gerbers"
    g_dir.mkdir(parents=True, exist_ok=True)

    # Gerbers
    layers = cfg.get("layers") or []
    if layers:
        cmd = [
            kicad, "pcb", "export", "gerbers",
            str(proj.pcb),
            "-o", str(g_dir),
            "--layers", ",".join(layers),
        ]
    else:
        cmd = [kicad, "pcb", "export", "gerbers", str(proj.pcb), "-o", str(g_dir)]
    res = run(cmd, verbose=verbose)
    if res.code != 0:
        raise RuntimeError(f"Gerber export failed: {res.err or res.out}")

    # Drill/Excellon
    d_cfg = cfg.get("drill", {})
    if d_cfg.get("enabled", True):
        drill_cmd = [
            kicad, "pcb", "export", "drill",
            str(proj.pcb),
            "-o", str(g_dir),
        ]
        units = d_cfg.get("units")
        if units in {"mm", "inch", "in"}:
            drill_cmd += ["--excellon-units", "in" if units in {"inch", "in"} else "mm"]

        # Generate map file when requested (default config requests a map)
        map_fmt = d_cfg.get("map_format")
        if map_fmt:
            # KiCad 9 expects pdf|gerberx2|ps|dxf|svg. Map legacy 'gerber' to 'gerberx2'
            mf = str(map_fmt).lower()
            if mf == "gerber":
                mf = "gerberx2"
            drill_cmd += ["--generate-map", "--map-format", mf]

        # NPTH merge/separate: if merge_npth is False => separate files
        if d_cfg.get("merge_npth") is False:
            drill_cmd += ["--excellon-separate-th"]
        res2 = run(drill_cmd, verbose=verbose)
        if res2.code != 0:
            raise RuntimeError(f"Drill export failed: {res2.err or res2.out}")

    # Zip
    if zip_gerbers:
        zip_name = f"{FBASE}_gerbers.zip"  # FBASE set in main
        zip_path = out_dir / zip_name
        make_zip(zip_path, g_dir)
        return zip_path
    return None


def export_step(kicad: str, proj: Project, out_dir: Path, cfg: Dict[str, Any], verbose: bool) -> Path:
    if not cfg.get("enabled", True):
        raise RuntimeError("STEP export disabled by config")
    out_path = out_dir / f"{FBASE}.step"
    cmd = [kicad, "pcb", "export", "step", str(proj.pcb), "-o", str(out_path)]

    # Optional includes based on config (supported in KiCad 9)
    if cfg.get("include_tracks_zones"):
        cmd += ["--include-tracks", "--include-zones"]
    if cfg.get("include_pads"):
        cmd += ["--include-pads"]
    if cfg.get("include_inner_copper"):
        cmd += ["--include-inner-copper"]
    if cfg.get("include_silkscreen"):
        cmd += ["--include-silkscreen"]
    if cfg.get("include_soldermask"):
        cmd += ["--include-soldermask"]
    if cfg.get("board_only"):
        cmd += ["--board-only"]
    if cfg.get("fuse_shapes"):
        cmd += ["--fuse-shapes"]
    # Origin selection
    user_origin = cfg.get("user_origin")
    if isinstance(user_origin, str) and user_origin:
        if user_origin.lower() == "grid":
            cmd += ["--grid-origin"]
        elif user_origin.lower() == "drill":
            cmd += ["--drill-origin"]
        else:
            # Allow explicit coordinates like "25.4x25.4mm" or "1x1in"
            cmd += ["--user-origin", user_origin]
    res = run(cmd, verbose=verbose)
    if res.code != 0:
        # If failure looks like missing 3D models and ignoring is enabled, try fallback
        if cfg.get("ignore_missing_models", True) and _is_missing_model_err(res.err or res.out):
            warn = (res.err or res.out).strip()
            print("Warning: STEP export reported missing 3D models; attempting fallback.", file=sys.stderr)
            if warn:
                print(warn, file=sys.stderr)
            if cfg.get("fallback_board_only", True):
                fallback_cmd = [
                    kicad, "pcb", "export", "step", str(proj.pcb), "-o", str(out_path),
                    "--board-only",
                ]
                # keep any optional includes that make sense with board-only
                if cfg.get("include_silkscreen"):
                    fallback_cmd += ["--include-silkscreen"]
                if cfg.get("include_soldermask"):
                    fallback_cmd += ["--include-soldermask"]
                res2 = run(fallback_cmd, verbose=verbose)
                if res2.code == 0 and out_path.exists():
                    return out_path
            # If fallback disabled or also failed, surface original error
        raise RuntimeError(f"STEP export failed: {res.err or res.out}")
    return out_path


def read_board_layers(pcb: Path) -> List[str]:
    """Return the canonical names of every layer enabled in a .kicad_pcb.

    The board-level layer table looks like:

        (layers
          (0 "F.Cu" signal)
          (9 "User.1" user "Coating Notes")
          ...
        )

    Only enabled layers are listed, so this is exactly "all available layers".
    We take the canonical (untranslated) name in position 1 -- that is what
    kicad-cli's --layers expects -- and ignore any user-facing alias in
    position 3. Pad/footprint layer lists have no leading integer index, so the
    index requirement keeps them out. Returns [] if the table can't be parsed.
    """
    import re

    try:
        text = pcb.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    m = re.search(r"^\s*\(layers\b", text, re.MULTILINE)
    if not m:
        return []

    # Walk forward from the opening paren to its match to isolate the block.
    depth = 0
    start = text.index("(", m.start())
    end = -1
    for i in range(start, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return []

    layers: List[str] = []
    for name in re.findall(r'\(\s*\d+\s+"([^"]+)"', text[start:end]):
        if name not in layers:
            layers.append(name)
    return layers


def merge_pdfs(out_path: Path, parts: List[Path], bookmarks: List[str]) -> bool:
    """Concatenate `parts` into `out_path`, adding one outline entry per page.

    `bookmarks` is aligned to the resulting pages; extra pages are left unnamed.
    Returns False if pypdf is unavailable so callers can degrade gracefully.
    """
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
    except Exception:
        return False

    writer = PdfWriter()
    for part in parts:
        reader = PdfReader(str(part))
        for page in reader.pages:
            writer.add_page(page)

    for idx, title in enumerate(bookmarks):
        if idx >= len(writer.pages):
            break
        try:
            writer.add_outline_item(title, idx)
        except Exception:
            # Outlines are a nicety; never fail the export over them.
            break

    if out_path.exists():
        out_path.unlink()
    with out_path.open("wb") as f:
        writer.write(f)
    return True


def export_pcb_pdf(kicad: str, proj: Project, out_dir: Path, cfg: Dict[str, Any], verbose: bool) -> Dict[str, str]:
    """Export the PCB PDF: a full-stack composite first page, then one page per layer.

    Returns a dict of produced artifacts keyed by 'pcb_pdf' (and 'pcb_pdf_stack'
    when the two halves could not be merged).
    """
    if not cfg.get("enabled", True):
        raise RuntimeError("PCB PDF export disabled by config")
    out_path = out_dir / f"{FBASE}_PCB.pdf"
    print(f"Exporting PCB PDF to {out_path} ...")
    # Remove any pre-existing file so kicad-cli can write cleanly.
    if out_path.exists():
        out_path.unlink()

    def _common_flags(cmd: List[str]) -> List[str]:
        if cfg.get("include_title_block", True):
            cmd += ["--include-border-title"]
        if cfg.get("monochrome"):
            cmd += ["--black-and-white"]
        return cmd

    notes_layers = [str(x) for x in (cfg.get("notes_layers") or [])]

    layers = list(cfg.get("layers") or [])
    if notes_layers and cfg.get("notes_own_page"):
        for name in notes_layers:
            if name not in layers:
                layers.append(name)

    # --- Page 1: full-stack composite ---
    stack_cfg = cfg.get("stack_page") or {}
    stack_tmp: Optional[Path] = None
    if stack_cfg.get("enabled", True):
        stack_layers = stack_cfg.get("layers")
        if not stack_layers:
            stack_layers = read_board_layers(proj.pcb)
            if not stack_layers:
                print(
                    f"Warning: could not read the layer table from {proj.pcb.name}; "
                    "falling back to the configured pcb_pdf.layers for the stack page.",
                    file=sys.stderr,
                )
                stack_layers = list(cfg.get("layers") or [])
        stack_layers = [str(x) for x in stack_layers]

        excluded = {str(x) for x in (stack_cfg.get("exclude_layers") or [])}
        # Notes layers go last so they plot on top of copper and silkscreen.
        stack_layers = [x for x in stack_layers if x not in excluded and x not in notes_layers]
        stack_layers += [x for x in notes_layers if x not in excluded]

        if not stack_layers:
            print("Warning: stack page has no layers to plot; skipping it.", file=sys.stderr)
        else:
            stack_tmp = out_dir / f"{FBASE}_PCB_stack.tmp.pdf"
            if stack_tmp.exists():
                stack_tmp.unlink()
            # --mode-single composites every listed layer onto one page; -o is the
            # complete file path in this mode.
            cmd = _common_flags([
                kicad, "pcb", "export", "pdf", str(proj.pcb), "-o", str(stack_tmp),
                "--layers", ",".join(stack_layers),
                "--mode-single",
            ])
            res = run(cmd, verbose=verbose)
            if res.code != 0 or not stack_tmp.exists():
                raise RuntimeError(f"PCB stack page export failed: {res.err or res.out}")

    # --- Pages 2..N: one layer per page ---
    layers_tmp = out_dir / f"{FBASE}_PCB_layers.tmp.pdf" if stack_tmp else out_path
    if layers_tmp.exists():
        layers_tmp.unlink()

    cmd = [kicad, "pcb", "export", "pdf", str(proj.pcb), "-o", str(layers_tmp)]
    if layers:
        cmd += ["--layers", ",".join(layers)]
    # Produce a single multi-page PDF (one layer per page).
    # KiCad 10+ expects -o to be the output file path when using --mode-multipage.
    cmd += ["--mode-multipage"]
    _common_flags(cmd)
    res = run(cmd, verbose=verbose)
    if res.code != 0:
        raise RuntimeError(f"PCB PDF export failed: {res.err or res.out}")

    if not layers_tmp.exists():
        raise RuntimeError(f"PCB PDF export failed: No PDF produced at {layers_tmp}")

    if stack_tmp is None:
        return {"pcb_pdf": str(out_path)}

    # --- Merge: stack page in front of the per-layer pages ---
    if merge_pdfs(out_path, [stack_tmp, layers_tmp], ["Full Stack"] + layers):
        stack_tmp.unlink(missing_ok=True)
        layers_tmp.unlink(missing_ok=True)
        return {"pcb_pdf": str(out_path)}

    # pypdf unavailable: ship the two halves side by side rather than failing.
    stack_path = out_dir / f"{FBASE}_PCB_Stack.pdf"
    if stack_path.exists():
        stack_path.unlink()
    stack_tmp.replace(stack_path)
    layers_tmp.replace(out_path)
    print(
        "Warning: pypdf is not installed, so the full-stack page could not be merged "
        f"into {out_path.name}. It was written separately as {stack_path.name}. "
        "Install it with 'pip install pypdf' to get a single combined PDF.",
        file=sys.stderr,
    )
    return {"pcb_pdf": str(out_path), "pcb_pdf_stack": str(stack_path)}


def export_sch_pdf(kicad: str, proj: Project, out_dir: Path, cfg: Dict[str, Any], verbose: bool) -> Path:
    if not cfg.get("enabled", True):
        raise RuntimeError("Schematics PDF export disabled by config")
    out_path = out_dir / f"{FBASE}.pdf"
    cmd = [kicad, "sch", "export", "pdf", str(proj.sch), "-o", str(out_path)]
    if cfg.get("monochrome"):
        cmd += ["--black-and-white"]
    res = run(cmd, verbose=verbose)
    if res.code != 0:
        raise RuntimeError(f"Schematics PDF export failed: {res.err or res.out}")
    return out_path


# -----------------------------
# BOM field discovery
# -----------------------------
# Matches `(property "Name" "Value" ...)` inside a KiCad s-expression file.
_PROP_RE = re.compile(r'\(\s*property\s+"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')

# Symbol properties that are library bookkeeping rather than BOM data.
_SKIP_FIELD_PREFIXES = ("ki_",)
_SKIP_FIELD_NAMES = {"sheetname", "sheetfile", "sheet name", "sheet file"}

# A column counts as a part number if its name looks like one; users name these
# all sorts of ways (MPN, Mouser Part Number, Manufacturer_PN, ...).
_PART_NUM_RE = re.compile(r"part\s*[-_ ]?\s*(number|no\b|num\b)|\bmpn\b|\bp/?n\b|part[-_]?number", re.IGNORECASE)
_SUPPLIER_RE = re.compile(r"supplier|distributor|vendor", re.IGNORECASE)


def _sexpr_block_end(text: str, start: int) -> int:
    """Return the index just past the ')' closing the s-expression at text[start] == '('."""
    depth = 0
    i = start
    n = len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _iter_sexpr_blocks(text: str, token: str):
    """Yield the source of each top-level `(token ...)` block, brace- and string-aware."""
    pat = re.compile(r"\(\s*" + re.escape(token) + r"(?=[\s(])")
    i = 0
    while True:
        m = pat.search(text, i)
        if not m:
            return
        end = _sexpr_block_end(text, m.start())
        yield text[m.start():end]
        i = end


def _unescape_sexpr(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def discover_symbol_fields(top_sch: Path, verbose: bool = False) -> List[str]:
    """Collect every property name used by symbols across the schematic hierarchy.

    Walks the root sheet and every sheet it references (recursively), skipping
    `lib_symbols` template definitions and KiCad-internal `ki_*` properties.
    Returns names in first-seen order.
    """
    found: List[str] = []
    seen: set = set()
    visited: set = set()
    queue: List[Path] = [top_sch]

    while queue:
        sch = queue.pop(0)
        try:
            key = str(sch.resolve()).lower()
        except OSError:
            key = str(sch).lower()
        if key in visited:
            continue
        visited.add(key)

        try:
            text = sch.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"Warning: could not read {sch} for BOM field discovery: {e}", file=sys.stderr)
            continue

        # Library definitions carry template fields, not instance data.
        for blk in _iter_sexpr_blocks(text, "lib_symbols"):
            text = text.replace(blk, "", 1)

        for sym in _iter_sexpr_blocks(text, "symbol"):
            for m in _PROP_RE.finditer(sym):
                name = _unescape_sexpr(m.group(1)).strip()
                if not name or name in seen:
                    continue
                low = name.lower()
                if low in _SKIP_FIELD_NAMES or low.startswith(_SKIP_FIELD_PREFIXES):
                    continue
                if "," in name or '"' in name:
                    # kicad-cli takes a comma-separated list; such a name cannot be requested.
                    print(f"Warning: skipping BOM field with unsupported name: {name!r}", file=sys.stderr)
                    continue
                seen.add(name)
                found.append(name)

        # Follow hierarchical sheets.
        for sheet in _iter_sexpr_blocks(text, "sheet"):
            for m in _PROP_RE.finditer(sheet):
                if _unescape_sexpr(m.group(1)).strip().lower() in {"sheetfile", "sheet file"}:
                    rel = _unescape_sexpr(m.group(2)).strip()
                    if rel:
                        queue.append(sch.parent / rel)

    if verbose:
        print(f"BOM: discovered {len(found)} symbol field(s) across {len(visited)} sheet(s): {', '.join(found)}")
    return found


def _normalize_field_key(name: str) -> str:
    """Collapse a field name to its comparison key: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _read_csv_table(path: Path) -> Tuple[List[List[str]], str]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";"]).delimiter
        except Exception:
            delim = ","
        rows = list(csv.reader(f, delimiter=delim))
    return rows, delim


def _merge_similar_columns(path: Path, verbose: bool) -> None:
    """Fold together BOM columns whose headers differ only by case/spacing/punctuation.

    KiCad matches symbol fields exactly, so "MPN" and "mpn" become two columns
    with half the data in each. Merge them when they never disagree; if any row
    holds two different non-empty values, leave both columns and warn instead.
    """
    import csv

    try:
        rows, delim = _read_csv_table(path)
    except Exception as e:
        print(f"Warning: could not post-process BOM columns: {e}", file=sys.stderr)
        return
    if not rows:
        return

    header = rows[0]
    body = rows[1:]
    groups: Dict[str, List[int]] = {}
    for idx, name in enumerate(header):
        key = _normalize_field_key(name)
        if not key:
            continue
        groups.setdefault(key, []).append(idx)

    drop: set = set()
    changed = False
    for key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        names = [header[i] for i in idxs]
        conflict = False
        for row in body:
            values = {row[i].strip() for i in idxs if i < len(row) and row[i].strip()}
            if len(values) > 1:
                conflict = True
                break
        if conflict:
            print(
                "Warning: BOM columns {} hold different values on the same row; "
                "leaving them separate. Rename the symbol fields to a single spelling.".format(
                    ", ".join(repr(n) for n in names)
                ),
                file=sys.stderr,
            )
            continue
        # Keep the variant that carries the most data; ties keep the leftmost.
        counts = [sum(1 for row in body if i < len(row) and row[i].strip()) for i in idxs]
        keep_pos = counts.index(max(counts))
        keep = idxs[keep_pos]
        for row in body:
            if keep < len(row) and row[keep].strip():
                continue
            for i in idxs:
                if i < len(row) and row[i].strip():
                    while len(row) <= keep:
                        row.append("")
                    row[keep] = row[i]
                    break
        drop.update(i for i in idxs if i != keep)
        changed = True
        print(
            "BOM: merged columns {} into {!r}.".format(", ".join(repr(n) for n in names), header[keep]),
            file=sys.stderr,
        )

    if not changed:
        return

    keep_idx = [i for i in range(len(header)) if i not in drop]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=delim, lineterminator="\n")
        for row in [header] + body:
            w.writerow([row[i] if i < len(row) else "" for i in keep_idx])
    if verbose:
        print(f"BOM: rewrote {path.name} with {len(keep_idx)} column(s)")


def export_bom(kicad: str, proj: Project, out_dir: Path, cfg: Dict[str, Any], verbose: bool) -> Path:
    if not cfg.get("enabled", True):
        raise RuntimeError("BOM export disabled by config")
    out_path = out_dir / f"{FBASE}_BOM.csv"

    # KiCad 9 syntax: no --format, no --plugin. Use --format-preset, --fields, --group-by, etc.
    cmd = [kicad, "sch", "export", "bom", str(proj.sch), "-o", str(out_path)]

    fmt = (cfg.get("output_format") or "csv").lower()
    # Prefer CSV preset if requested; other presets can be added as needed
    if fmt in {"csv", "tsv"}:
        preset = "CSV" if fmt == "csv" else "TSV"
        cmd += ["--format-preset", preset]

    # Start from the configured fields, then fold in every field actually
    # present on symbols, so a column is never dropped just because its name
    # does not match the config exactly.
    user_fields = list(map(str, cfg.get("fields") or []))
    if not user_fields:
        # If no user fields provided, seed with sensible defaults
        user_fields = ["Reference", "${QUANTITY}", "Value", "Footprint", "${DNP}"]

    # Normalize quantity token: allow users to specify 'Qty' or 'Quantity' and convert to ${QUANTITY}
    normalized_fields: List[str] = []
    for f in user_fields:
        fl = f.strip()
        if fl.lower() in {"qty", "quantity", "${quantity}"}:
            normalized_fields.append("${QUANTITY}")
        elif fl.lower() in {"dnp", "${dnp}"}:
            normalized_fields.append("${DNP}")
        else:
            normalized_fields.append(fl)

    exclude = {str(x).strip().lower() for x in (cfg.get("exclude_fields") or [])}
    fields_final: List[str] = []
    seen = set()
    # Preserve order of user_fields
    for f in normalized_fields:
        if f and f not in seen and f.lower() not in exclude:
            fields_final.append(f)
            seen.add(f)

    discovered: List[str] = []
    if cfg.get("include_all_fields", True):
        try:
            discovered = discover_symbol_fields(proj.sch, verbose)
        except Exception as e:
            print(f"Warning: BOM field discovery failed ({e}); using configured fields only.", file=sys.stderr)
    # Case/spacing variants are separate fields to KiCad, so request each one
    # here; _merge_similar_columns folds them back together afterwards.
    extras: List[str] = []
    for f in sorted(discovered, key=str.lower):
        if f in seen or f.lower() in exclude:
            continue
        extras.append(f)
        seen.add(f)
    fields_final += extras
    if extras:
        print(f"BOM: including {len(extras)} extra schematic field(s): {', '.join(extras)}", file=sys.stderr)

    # Append the supplier placeholders only when nothing already covers them:
    # "MPN" or "Mouser Part Number" satisfies the part-number requirement.
    for f in ["Supplier", "Supplier Part Number"]:
        pat = _PART_NUM_RE if _PART_NUM_RE.search(f) else _SUPPLIER_RE
        if f in seen or f.lower() in exclude or any(pat.search(s) for s in seen):
            continue
        fields_final.append(f)
        seen.add(f)
    if fields_final:
        cmd += ["--fields", ",".join(fields_final)]

    # Compute labels so headers are friendly (e.g., 'Qty' for ${QUANTITY}) unless user provided them
    labels_cfg = cfg.get("labels")
    if labels_cfg and isinstance(labels_cfg, list) and len(labels_cfg) == len(fields_final):
        labels_final = list(map(str, labels_cfg))
    else:
        labels_final = []
        for f in fields_final:
            if f == "${QUANTITY}":
                labels_final.append("Qty")
            elif f == "Reference":
                labels_final.append("Reference")
            elif f == "${DNP}":
                labels_final.append("DNP")
            else:
                labels_final.append(f)
    if labels_final:
        cmd += ["--labels", ",".join(labels_final)]

    group_by = cfg.get("group_by")
    if group_by:
        cmd += ["--group-by", ",".join(map(str, group_by))]

    res = run(cmd, verbose=verbose)
    if res.code == 0 and out_path.exists():
        if cfg.get("merge_similar_fields", True):
            _merge_similar_columns(out_path, verbose)
        # Post-check: warn if supplier/part-number data is missing from header
        try:
            rows, _delim = _read_csv_table(out_path)
            header = [h.strip() for h in (rows[0] if rows else [])]
            if not any(_SUPPLIER_RE.search(h) for h in header):
                print("Warning: BOM has no supplier column. Add a 'Supplier' field to your symbols or update BOM settings.", file=sys.stderr)
            if not any(_PART_NUM_RE.search(h) for h in header):
                print("Warning: BOM has no part-number column (e.g. 'MPN', 'Supplier Part Number', 'Mouser Part Number'). Add this field to your symbols or update BOM settings.", file=sys.stderr)
            # A part number that varies within a grouped row is silently lost by
            # kicad-cli unless the column is part of --group-by.
            group_by = [str(g).strip() for g in (cfg.get("group_by") or [])]
            pn_cols = [h for h in header if _PART_NUM_RE.search(h)]
            ungrouped = [h for h in pn_cols if h not in group_by]
            if group_by and ungrouped:
                print(
                    "Note: part-number column(s) {} are not in bom.group_by {}; symbols that share a "
                    "value/footprint but carry different part numbers collapse into one row.".format(
                        ", ".join(repr(h) for h in ungrouped), group_by
                    ),
                    file=sys.stderr,
                )
        except Exception:
            # Non-fatal: ignore parsing issues
            pass
        return out_path

    raise RuntimeError(f"BOM export failed: {res.err or res.out}")


def export_pos(kicad: str, proj: Project, out_dir: Path, cfg: Dict[str, Any], verbose: bool) -> Dict[str, str]:
    """Export component position (pick-and-place) files.

    Returns a dict of produced files keyed by 'pos_front' and/or 'pos_back' (or 'pos' for single-side).
    """
    if not cfg.get("enabled", True):
        return {}

    fmt = str(cfg.get("format", "csv")).lower()
    if fmt not in {"csv", "tsv"}:
        # Fall back to csv to avoid CLI incompatibility
        fmt = "csv"
    units = str(cfg.get("units", "mm")).lower()
    if units in {"in", "inch", "inches"}:
        units = "inch"
    else:
        units = "mm"

    side = str(cfg.get("side", "both")).lower()
    if side not in {"front", "back", "both"}:
        side = "both"

    produced: Dict[str, str] = {}

    def _one(side_sel: str, key: str) -> Optional[Path]:
        # Write directly to a predictable file name for each side
        suffix = "Front" if side_sel == "front" else "Back"
        ext = "csv" if fmt == "csv" else "tsv"
        out_path = out_dir / f"{FBASE}_POS_{suffix}.{ext}"
        cmd = [
            kicad, "pcb", "export", "pos", str(proj.pcb),
            "-o", str(out_path),
            "--format", fmt,
            "--units", units,
            "--side", side_sel,
        ]
        res = run(cmd, verbose=verbose)
        if res.code == 0 and out_path.exists():
            produced[key] = str(out_path)
            return out_path
        # Surface CLI output to help diagnose version/flag mismatches
        raise RuntimeError(f"Position export ({side_sel}) failed: {res.err or res.out}")

    if side == "front":
        _one("front", "pos_front")
    elif side == "back":
        _one("back", "pos_back")
    else:
        # both
        _one("front", "pos_front")
        _one("back", "pos_back")

    return produced


# -----------------------------
# Zip helper
# -----------------------------

def make_zip(zip_path: Path, src_dir: Path) -> None:
    import zipfile

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                arc = p.relative_to(src_dir)
                zf.write(p, arcname=str(arc))


# -----------------------------
# Manifest
# -----------------------------

def write_manifest(out_dir: Path, data: Dict[str, Any]) -> Path:
    man_path = out_dir / "manifest.json"
    with man_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return man_path


# -----------------------------
# Main
# -----------------------------
TAG = ""
TAG_SAFE = ""
NAME_FOR_FILES = ""
FBASE = ""  # {safe_name}_{tag_safe}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export KiCad project artifacts using kicad-cli")
    p.add_argument("--project-dir", required=True, help="Path to folder containing .kicad_pro")
    p.add_argument("--project-name", help="Basename of KiCad project without extension (disambiguate if multiple)")
    p.add_argument("--tag", help="Git tag or build label; defaults to UTC yyyyMMdd-HHmm")
    p.add_argument("--config", help="Path to YAML config overriding export options")
    p.add_argument("--out-dir", help="Output directory (default: Exports/{name}_{tag}/ under project dir)")
    # Color controls (applies to both PCB and schematic PDFs)
    color_grp = p.add_mutually_exclusive_group()
    color_grp.add_argument("--color", action="store_true", help="Force color PDF outputs (overrides config)")
    color_grp.add_argument("--monochrome", action="store_true", help="Force black-and-white PDF outputs (overrides config)")
    v = p.add_mutually_exclusive_group()
    v.add_argument("--verbose", action="store_true", help="Verbose logging")
    v.add_argument("--quiet", action="store_true", help="Minimal logging")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    global TAG
    global TAG_SAFE, NAME_FOR_FILES, FBASE
    args = parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    tag = args.tag or _dt.datetime.utcnow().strftime("%Y%m%d-%H%M")
    TAG = tag
    TAG_SAFE = _sanitize_label(tag)

    # Load config
    config: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            print(f"Config file not found: {cfg_path}", file=sys.stderr)
            return 2
        if yaml is None:
            print("PyYAML not installed. Install with 'pip install pyyaml' or omit --config.", file=sys.stderr)
            return 2
        with cfg_path.open("r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        if not isinstance(user_cfg, dict):
            print("Config file must contain a YAML mapping at the root.", file=sys.stderr)
            return 2
        _deep_update(config, user_cfg)

    # Apply CLI color overrides for PDF outputs
    if getattr(args, "color", False):
        config.setdefault("pcb_pdf", {}).update({"monochrome": False})
        config.setdefault("schematics_pdf", {}).update({"monochrome": False})
    elif getattr(args, "monochrome", False):
        config.setdefault("pcb_pdf", {}).update({"monochrome": True})
        config.setdefault("schematics_pdf", {}).update({"monochrome": True})

    verbose = bool(args.verbose)
    quiet = bool(args.quiet)

    # Detect project
    try:
        proj = detect_project(project_dir, args.project_name)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2

    # Output dir and filename base
    NAME_FOR_FILES = _sanitize_label(proj.name)
    FBASE = f"{NAME_FOR_FILES}_{TAG_SAFE}"
    if args.out_dir:
        out_dir = Path(args.out_dir).resolve()
    else:
        out_dir = proj.dir / "Exports" / f"{FBASE}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clean output if requested
    if config.get("general", {}).get("clean_output", True):
        for child in out_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

    # Find kicad-cli
    try:
        kicad_cli_path, kicad_cli_version = find_kicad_cli(config.get("general", {}).get("kicad_cli"), verbose=verbose)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    if not quiet:
        print(f"Using kicad-cli: {kicad_cli_path} ({kicad_cli_version})")
        print(f"Project: {proj.name}")
        print(f"Outputs: {out_dir}")
        print(f"Tag: {tag}")

    outputs: Dict[str, str] = {}

    # Export sequence
    try:
        gz = export_gerbers_and_drill(
            kicad_cli_path, proj, out_dir, config.get("gerbers", {}),
            zip_gerbers=config.get("general", {}).get("zip_gerbers", True),
            verbose=verbose,
        )
        if gz is not None:
            outputs["gerbers_zip"] = str(gz)

        if config.get("step", {}).get("enabled", True):
            s = export_step(kicad_cli_path, proj, out_dir, config.get("step", {}), verbose)
            outputs["step"] = str(s)
        if config.get("pcb_pdf", {}).get("enabled", True):
            outputs.update(export_pcb_pdf(kicad_cli_path, proj, out_dir, config.get("pcb_pdf", {}), verbose))
        if config.get("schematics_pdf", {}).get("enabled", True):
            spdf = export_sch_pdf(kicad_cli_path, proj, out_dir, config.get("schematics_pdf", {}), verbose)
            outputs["schematics_pdf"] = str(spdf)
        if config.get("bom", {}).get("enabled", True):
            b = export_bom(kicad_cli_path, proj, out_dir, config.get("bom", {}), verbose)
            outputs["bom_csv"] = str(b)
        # Component position (pick-and-place) files
        if config.get("pos", {}).get("enabled", True):
            pos_outs = export_pos(kicad_cli_path, proj, out_dir, config.get("pos", {}), verbose)
            outputs.update(pos_outs)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    # Manifest
    manifest = {
        "project": {
            "name": proj.name,
            "dir": str(proj.dir),
            "pro": str(proj.pro),
            "pcb": str(proj.pcb),
            "sch": str(proj.sch),
            "safe_name": NAME_FOR_FILES or _sanitize_label(proj.name),
        },
        "tag": tag,
        "tag_safe": TAG_SAFE,
        "timestamp_utc": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "host": {
            "os": platform.system(),
            "release": platform.release(),
            "python": sys.version.split()[0],
        },
        "tools": {
            "kicad_cli_path": kicad_cli_path,
            "kicad_cli_version": kicad_cli_version,
        },
        "config": config,
        "outputs": outputs,
        "outputs_dir": str(out_dir),
        "invoked_commands": INVOKED,
    }
    man_path = write_manifest(out_dir, manifest)

    if not quiet:
        print("Artifacts:")
        for k, v in outputs.items():
            print(f"- {k}: {v}")
        print(f"Manifest: {man_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
