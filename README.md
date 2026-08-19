# Operations Toolkit 1.3.0

Operations Toolkit includes the working cnMaestro Speed Manager with compact service/tool navigation. Version 1.3.0 restores the dense v1.2.0-style workspace while keeping the current nested service navigation and cnMaestro behavior.

## What changed in 1.3.0

- Restored the dense dark-navy v1.2.0-style Speed Manager workspace with compact auth, scan, filter, table, and publish sections.
- Kept the nested Overview > cnMaestro > Speed Manager navigation; Activity is renamed Audit Log.
- Scan & Filters, Preview & Publish, and Audit Log are in-app views, with the existing audit CSV export retained.
- Other dialogs and the cnMaestro API, TLS, scan/filter/selection, preview/publish, audit, cache, updater, latest-manifest, and package behavior are unchanged.

Older releases remain available in GitHub release history.

## Included v1.1.0 behavior

- Optional closest-downstream package suggestions with adjustable tolerance and visible DL/UL variance. Exact package remains authoritative.
- Sortable columns.
- Persistent checkbox selection across filters, with select/deselect visible and selected-only view.
- Publish progress bar, per-customer stage, success/failure counters, and completion popup.
- System, Light, and Dark appearance modes from the v1.1.0 baseline.

## Run from source

On Windows, run **Launch Operations Toolkit.bat**. Expand **cnMaestro** and select **Speed Manager**. Keep write actions disabled during validation.

The pinned dependencies are listed in `requirements.txt`.

## Update checks

No sidecar file is required. The app checks the built-in Operations Toolkit `latest.json` manifest and, after confirmation, opens the approved release download in the browser. It does not replace or install the executable automatically.

To override the manifest URL, place a valid `update_config.json` beside the executable. See `update_config.example.json` for the optional format.

## Build the Windows executable

Run `build_windows.bat`, or use the equivalent command after installing `requirements.txt`:

```powershell
pyinstaller --noconfirm --clean --onefile --windowed --name "Operations-Toolkit-v1.3.0" cnmaestro_speed_manager.py
```

The executable is written to `dist\Operations-Toolkit-v1.3.0.exe`.

## Regression guard

`release_checks/ast_behavior_guard.py` compares the API class and nonvisual operational methods with the original v1.1.0 source by AST. The intentionally changed updater resolver/check path and in-app visual navigation are covered by focused tests. CI also checks Python syntax/import, builds the Windows one-file/windowed executable, and launches and closes the GUI briefly with preview mode enabled and without making cnMaestro calls.

## Approved interface

![Operations Toolkit dense Speed Manager workspace](screenshots/operations-toolkit-v1.3.0-speed-manager-1280x760.png)

![Operations Toolkit in-app Audit Log](screenshots/operations-toolkit-v1.3.0-audit-log-1280x760.png)

## Approximate matching safety

Approximate matching changes display/grouping only. Preview retains actual live QoS. The downstream tolerance defaults to 10%. Upload variance is shown but does not determine the suggestion.
