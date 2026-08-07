$EnvFile = Join-Path $PSScriptRoot ".env"

if (Test-Path $EnvFile) {
    foreach ($RawLine in Get-Content $EnvFile) {
        $Line = $RawLine.Trim()
        if (-not $Line -or $Line.StartsWith("#") -or -not $Line.Contains("=")) {
            continue
        }

        $Parts = $Line.Split("=", 2)
        $Name = $Parts[0].Trim()
        $Value = $Parts[1].Trim().Trim('"').Trim("'")
        if ($Name) {
            Set-Item -Path "Env:$Name" -Value $Value
        }
    }
}
