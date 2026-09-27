# ClipChimp installer: installs what ClipChimp needs, starts it now,
# and starts it every time you sign in to Windows.
$ErrorActionPreference = 'Stop'

function Find-Pythonw {
    # pythonw.exe = Python without a console window
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $exe = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($exe) {
            $candidate = Join-Path (Split-Path $exe) 'pythonw.exe'
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

try {
    $script = Join-Path $PSScriptRoot 'clipchimp.py'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        throw 'clipchimp.py was not found next to install.ps1.'
    }

    $pythonw = Find-Pythonw
    if (-not $pythonw) {
        throw 'Python was not found. Install Python 3 from python.org, then run this again.'
    }
    $python = Join-Path (Split-Path $pythonw) 'python.exe'

    Write-Host 'Installing what ClipChimp needs...'
    & $python -m pip install --user --quiet -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the requirements.' }

    $startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
    $shortcutPath = Join-Path $startup 'ClipChimp.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '"' + $script + '"'
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = 'ClipChimp screen snipper'
    $shortcut.Save()

    Start-Process -FilePath $pythonw -ArgumentList ('"' + $script + '"') -WorkingDirectory $PSScriptRoot
    Write-Host 'ClipChimp is running. It will also start every time you sign in.' -ForegroundColor Green
}
catch {
    Write-Host ('Something went wrong: ' + $_.Exception.Message) -ForegroundColor Red
}
finally {
    Read-Host 'Press Enter to close'
}
