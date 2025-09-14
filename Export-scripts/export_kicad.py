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
            "F.SilkS",
            "B.SilkS",
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
    "pcb_pdf": {
        "enabled": True,
        "layers": [
            "F.Cu",
            "B.Cu",
            "F.SilkS",
            "B.SilkS",
            "F.Mask",
            "B.Mask",
            "Edge.Cuts",
        ],
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
        # Try common KiCad 9 and 8 paths
        for ver in ("9.0", "9", "8.0", "8"):
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


def export_pcb_pdf(kicad: str, proj: Project, out_dir: Path, cfg: Dict[str, Any], verbose: bool) -> Path:
    if not cfg.get("enabled", True):
        raise RuntimeError("PCB PDF export disabled by config")
    out_path = out_dir / f"{FBASE}_PCB.pdf"
    print(f"Exporting PCB PDF to {out_path} ...")
    # KiCad's pcb pdf export treats -o as a directory (especially with --mode-multipage).
    # Export to a temp folder, then move/rename the single resulting PDF to out_path.
    temp_dir = out_dir / "_pcb_pdf_tmp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    cmd = [kicad, "pcb", "export", "pdf", str(proj.pcb), "-o", str(temp_dir)]
    layers = cfg.get("layers")
    if layers and isinstance(layers, list):
        cmd += ["--layers", ",".join(layers)]
    # Produce a single multi-page PDF (one layer per page)
    cmd += ["--mode-multipage"]
    if cfg.get("include_title_block", True):
        cmd += ["--include-border-title"]
    if cfg.get("monochrome"):
        cmd += ["--black-and-white"]
    res = run(cmd, verbose=verbose)
    if res.code != 0:
        # Clean up temp dir on failure
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"PCB PDF export failed: {res.err or res.out}")

    # Find the generated PDF inside temp_dir
    pdf_candidates = sorted(temp_dir.glob("*.pdf"))
    chosen: Optional[Path] = None
    preferred = temp_dir / f"{proj.name}.pdf"
    if preferred.exists():
        chosen = preferred
    elif pdf_candidates:
        chosen = pdf_candidates[0]

    if not chosen or not chosen.exists():
        # Clean up temp dir before erroring out
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"PCB PDF export failed: No PDF produced in {temp_dir}")

    # Move to desired output path
    try:
        if out_path.exists():
            out_path.unlink(missing_ok=True)  # type: ignore[arg-type]
        shutil.move(str(chosen), str(out_path))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return out_path


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

    # Ensure Supplier columns are always requested and quantity is the KiCad token
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
    required_fields = ["Supplier", "Supplier Part Number"]
    fields_final: List[str] = []
    seen = set()
    # Preserve order of user_fields
    for f in normalized_fields:
        if f not in seen:
            fields_final.append(f)
            seen.add(f)
    # Append required fields if missing
    for f in required_fields:
        if f not in seen:
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
        # Post-check: warn if required columns are missing from header
        try:
            import csv  # local import to avoid top-level dependency in non-BOM runs
            with out_path.open("r", encoding="utf-8-sig", newline="") as f:
                # Attempt to detect delimiter
                sample = f.read(2048)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";"])
                except Exception:
                    class _Dial: pass
                    dialect = _Dial(); setattr(dialect, "delimiter", ",")
                reader = csv.reader(f, delimiter=getattr(dialect, "delimiter", ","))
                header = next(reader, [])
                header_set = set(h.strip() for h in header)
                for missing_col in ["Supplier", "Supplier Part Number"]:
                    if missing_col not in header_set:
                        print(f"Warning: BOM is missing expected column '{missing_col}'. Add this field to your symbols or update BOM settings.", file=sys.stderr)
        except Exception:
            # Non-fatal: ignore parsing issues
            pass
        return out_path

    raise RuntimeError(f"BOM export failed: {res.err or res.out}")


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
            ppdf = export_pcb_pdf(kicad_cli_path, proj, out_dir, config.get("pcb_pdf", {}), verbose)
            outputs["pcb_pdf"] = str(ppdf)
        if config.get("schematics_pdf", {}).get("enabled", True):
            spdf = export_sch_pdf(kicad_cli_path, proj, out_dir, config.get("schematics_pdf", {}), verbose)
            outputs["schematics_pdf"] = str(spdf)
        if config.get("bom", {}).get("enabled", True):
            b = export_bom(kicad_cli_path, proj, out_dir, config.get("bom", {}), verbose)
            outputs["bom_csv"] = str(b)
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
