$AppFolder=Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher=Join-Path $AppFolder 'Launch Operations Toolkit.bat'
$ShortcutPath=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Operations Toolkit.lnk'
$Shell=New-Object -ComObject WScript.Shell;$Shortcut=$Shell.CreateShortcut($ShortcutPath);$Shortcut.TargetPath=$Launcher;$Shortcut.WorkingDirectory=$AppFolder;$Shortcut.Description='Launch Operations Toolkit';$Shortcut.IconLocation="$env:SystemRoot\System32\shell32.dll,18";$Shortcut.Save()
Write-Host "Desktop shortcut created: $ShortcutPath"