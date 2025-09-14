KiCad Export Automation
=======================

This directory contains a Python tool `export_kicad.py` to export KiCad artifacts (Gerbers, Excellon, STEP, PDFs, BOM) using KiCad's CLI. See `export.yaml` for an example configuration. A GitHub Actions workflow (`.github/workflows/release-artifacts.yaml`) exports and uploads artifacts on tag pushes.

Artifacts generated
- `<project>_<tag>_gerbers.zip` (Gerbers + drills + map files)
- `<project>_<tag>.step` (board STEP)
- `<project>_<tag>.pdf` (combined schematics PDF: all sheets)
- `<project>_<tag>_PCB.pdf` (PCB layer stack PDF; one layer per page)
- `<project>_<tag>_BOM.csv` (BOM)
- `manifest.json` (tools, config, outputs summary)

All artifacts are written to `Exports/<project>_<tag>/` under the project directory. Filenames avoid spaces automatically.

Prerequisites
-------------
- Python 3.10+
- KiCad 9.x preferred (8.x likely compatible). Ensure `kicad-cli` is installed.
	- Windows: Install KiCad; ensure `C:\\Program Files\\KiCad\\9.0\\bin` is on PATH, or set `general.kicad_cli` in `export.yaml` to the full path (e.g., `C:/Program Files/KiCad/9.0/bin/kicad-cli.exe`).
	- Ubuntu: Use the KiCad PPA for v9: `ppa:kicad/kicad-9.0` (see the CI workflow for commands). `kicad-cli` will be on PATH.
	- macOS: Install via Homebrew `brew install --cask kicad`. Ensure `/usr/local/bin` or `/opt/homebrew/bin` is on PATH.
- Optional: `PyYAML` if you use a custom config file (`pip install pyyaml`).

Local usage (Windows PowerShell)
--------------------------------
From this directory (`ECAD/WashingLineMonitor-P0001`):

```powershell
# Minimal (auto tag = UTC yyyyMMdd-HHmm)
python .\export_kicad.py --project-dir .

# With explicit tag
python .\export_kicad.py --project-dir . --tag v1.2.3

# With config overrides
python .\export_kicad.py --project-dir . --config .\export.yaml

# If multiple .kicad_pro files exist, disambiguate
python .\export_kicad.py --project-dir . --project-name WashingLineMonitor-P0001
```

From the repo root, you can also run:

```powershell
python .\ECAD\WashingLineMonitor-P0001\export_kicad.py --project-dir .\ECAD\WashingLineMonitor-P0001 --tag v1.2.3
```

Configuration reference
-----------------------
See `export.yaml` for a complete, commented example. Defaults are applied when keys are omitted.

- general
	- clean_output: true|false (default true) — remove existing files in output directory first
	- zip_gerbers: true|false (default true) — zip the Gerber/drill outputs
	- kicad_cli: string|null — explicit path to `kicad-cli` if not on PATH
- gerbers
	- enabled: true|false (default true)
	- layers: list of KiCad layer names to plot (e.g., F.Cu, B.Mask, Edge.Cuts)
	- drill:
		- enabled: true|false (default true)
		- units: mm|inch (default mm)
		- map_format: gerber|pdf|svg (depends on KiCad version)
		- merge_npth: true|false
- pcb_pdf
	- enabled: true|false (default true)
	- layers: list of layers; one layer per page
	- monochrome: true|false (default true)
	- page_size: A4|Letter (if supported by CLI; used for consistency)
- schematics_pdf
	- enabled: true|false (default true)
	- monochrome: true|false (default true)
	- page_size: A4|Letter
	- include_title_block: true|false
- step
	- enabled: true|false (default true)
	- units: mm|inch (default mm)
	- include_tracks_zones: true|false (availability depends on KiCad)
	- model_precision: low|medium|high
- bom
	- enabled: true|false (default true)
	- method: cli|plugin (default cli)
	- output_format: csv (passed to CLI when supported)
	- plugin: plugin name (when method=plugin), e.g., `bom_csv_grouped_by_value`
	- plugin_args: extra plugin args (when method=plugin)
	- fields: list of fields to include
	- group_by: list of fields to group by

CI with GitHub Actions
----------------------
Workflow: `.github/workflows/release-artifacts.yaml`

- Triggers on pushing any tag (e.g., `git tag v1.2.3 && git push origin v1.2.3`).
- Checks out code with tags; sets up Python; installs KiCad 9 on Ubuntu.
- Runs `export_kicad.py` with `--tag ${{ github.ref_name }}`.
- Uploads `Exports/<project>_<tag>` as a build artifact.
- Optionally attaches artifacts to a GitHub Release.

Required permissions (set in the workflow):
- `contents: write` for creating a Release and uploading files.

Troubleshooting
---------------
- "Could not find 'kicad-cli'":
	- Verify KiCad installation; ensure `kicad-cli` is on PATH.
	- On Windows, set `general.kicad_cli` in `export.yaml` to the full path.
- Multiple `.kicad_pro` files in the directory:
	- Use `--project-name` to disambiguate which project to export.
- BOM export fails or produces empty CSV:
	- Some KiCad versions require a plugin; set `bom.method: plugin` and specify `bom.plugin`.
- CLI option not recognized:
	- Flags vary across KiCad versions; try removing the option in `export.yaml` or adjust your KiCad version.

