[CmdletBinding()]
param (
    [string]$VenvDirectory = ".venv"
)

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = if ([System.IO.Path]::IsPathRooted($VenvDirectory)) {
    $VenvDirectory
}
else {
    Join-Path $scriptDirectory $VenvDirectory
}
$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
$confluenceScript = Join-Path $scriptDirectory "accessing_confluence.py"
$mailScript = Join-Path $scriptDirectory "mail.py"

if (-not (Test-Path -LiteralPath $activateScript -PathType Leaf)) {
    throw "Virtual environment activation script not found: $activateScript"
}

foreach ($scriptPath in @($confluenceScript, $mailScript)) {
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Required Python script not found: $scriptPath"
    }
}

Push-Location $scriptDirectory
try {
    Write-Host "Activating virtual environment..."
    . $activateScript

    Write-Host "Running accessing_confluence.py..."
    & python $confluenceScript
    if ($LASTEXITCODE -ne 0) {
        throw "accessing_confluence.py failed with exit code $LASTEXITCODE. mail.py was not run."
    }

    Write-Host "Running mail.py..."
    & python $mailScript
    if ($LASTEXITCODE -ne 0) {
        throw "mail.py failed with exit code $LASTEXITCODE."
    }

    Write-Host "Both scripts completed successfully."
}
finally {
    Pop-Location
}
