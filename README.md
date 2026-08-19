# Operations Toolkit 1.2.2

Operations Toolkit includes the working cnMaestro Speed Manager with the approved compact service/tool navigation. Version 1.2.2 restores packaged update checks while preserving the v1.2.1 interface and cnMaestro behavior.

## What changed in 1.2.2

- Packaged builds now use the repository's built-in `latest.json` update manifest when no sidecar configuration exists.
- `update_config.json` remains an optional override for deployments that need a different manifest URL.
- Preserved the v1.2.1 interface and the v1.1.0 cnMaestro API, TLS, package, scan/filter/selection, preview/publish, audit, and cache behavior.

No cnMaestro API or backend behavior was redesigned for this release. Older releases remain available in GitHub release history.

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
pyinstaller --noconfirm --clean --onefile --windowed --name "Operations-Toolkit-v1.2.2" cnmaestro_speed_manager.py
```

The executable is written to `dist\Operations-Toolkit-v1.2.2.exe`.

## Regression guard

`release_checks/ast_behavior_guard.py` compares the API class and nonvisual operational methods with the original v1.1.0 source by AST. The intentionally changed updater resolver/check path is covered by focused unit tests. CI also checks Python syntax/import, builds the Windows one-file/windowed executable, and launches and closes the GUI briefly with preview mode enabled and without making cnMaestro calls.

## Approved interface

![Operations Toolkit compact nested service and tool navigation](screenshots/operations-toolkit-compact-navigation.png)

## Approximate matching safety

Approximate matching changes display/grouping only. Preview retains actual live QoS. The downstream tolerance defaults to 10%. Upload variance is shown but does not determine the suggestion.
