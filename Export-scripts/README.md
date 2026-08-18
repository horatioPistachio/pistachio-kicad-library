KiCad Export Automation
=======================

This directory contains a Python tool `export_kicad.py` to export KiCad artifacts (Gerbers, Excellon, STEP, PDFs, BOM) using KiCad's CLI. See `export.yaml` for an example configuration. A GitHub Actions workflow (`.github/workflows/release-artifacts.yaml`) exports and uploads artifacts on tag pushes.

Artifacts generated
- `<project>_<tag>_gerbers.zip` (Gerbers + drills + map files)
- `<project>_<tag>.step` (board STEP)
- `<project>_<tag>.pdf` (combined schematics PDF: all sheets)
- `<project>_<tag>_PCB.pdf` (PCB PDF; page 1 is a full-stack composite of every layer enabled in the board, then one layer per page)
- `<project>_<tag>_BOM.csv` (BOM)
- `manifest.json` (tools, config, outputs summary)

All artifacts are written to `Exports/<project>_<tag>/` under the project directory. Filenames avoid spaces automatically.

Prerequisites
-------------
- Python 3.10+
- KiCad 10.x preferred (9.x compatible). Ensure `kicad-cli` is installed.
	- Windows: Install KiCad; the script auto-detects versions 10, 9, and 8 under `C:\\Program Files\\KiCad\\`. Alternatively, set `general.kicad_cli` in `export.yaml` to the full path (e.g., `C:/Program Files/KiCad/10.0/bin/kicad-cli.exe`).
	- Ubuntu: Use the KiCad PPA for v10: `ppa:kicad/kicad-10.0-releases` (see the CI workflow for commands). `kicad-cli` will be on PATH.
	- macOS: Install via Homebrew `brew install --cask kicad`. Ensure `/usr/local/bin` or `/opt/homebrew/bin` is on PATH.
- Optional: `PyYAML` if you use a custom config file (`pip install pyyaml`).
- Optional: `pypdf` to merge the PCB full-stack page and the per-layer pages into one PDF (`pip install pypdf`). Without it the export still succeeds, but the stack page is written separately as `<project>_<tag>_PCB_Stack.pdf`.

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
	- layers: list of layers; one layer per page, from page 2 onward
	- stack_page: the composite first page
		- enabled: true|false (default true) — set false to get the old one-layer-per-page-only output
		- layers: list of layers to composite; omit or `null` to plot every layer enabled in the `.kicad_pcb` (including inner copper and User layers)
		- exclude_layers: layer names to drop from the composite (e.g. `[F.Fab, B.Fab]`)
	- notes_layers: layers holding process notes such as conformal coating (e.g. `[User.1]`); always included on the stack page and plotted last so they draw on top
	- notes_own_page: true|false (default false) — also give the notes layers their own page in the per-layer sequence, for when the composite is too busy to read them on
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
	- fields: columns to place first, in this order (`Qty`/`Quantity` and `DNP` are translated to KiCad's `${QUANTITY}`/`${DNP}` tokens)
	- include_all_fields: true|false (default true) — scan the schematic hierarchy and append every field found on symbols, so custom fields (MPN, LCSC, Manufacturer, Supplier...) are never dropped because their name is not in `fields`
	- exclude_fields: field names to keep out of the BOM even when present on symbols
	- merge_similar_fields: true|false (default true) — fold together columns whose names differ only by case/spacing/punctuation (`MPN` vs `mpn`, `Part Number` vs `Part_Number`); columns holding conflicting values on the same row are left separate and reported
	- group_by: list of fields to group by

	BOM column notes
	- KiCad matches symbol fields by exact name, so `MPN` and `mpn` are two different fields. `include_all_fields` requests both and `merge_similar_fields` combines them in the CSV; the export prints which columns it merged.
	- Fields not listed in `group_by` are collapsed when rows are merged, so two parts with the same value and footprint but different part numbers become one row. The export prints a note when a part-number column is missing from `group_by` — add it there (or drop `group_by`) if that matters.

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
- BOM is missing a part-number (or other custom) column:
	- Keep `bom.include_all_fields: true`; the exporter reads the schematic and adds every symbol field, so the name no longer has to match `bom.fields` exactly.
	- Run with `--verbose` to see the discovered field list and the assembled `--fields` argument.
	- If the column exists but rows are blank, the field is probably spelled differently on different symbols (KiCad treats `MPN` and `mpn` as separate fields). `merge_similar_fields` combines them unless they disagree; the run prints a warning naming the columns it could not merge.
- CLI option not recognized:
	- Flags vary across KiCad versions; try removing the option in `export.yaml` or adjust your KiCad version.

