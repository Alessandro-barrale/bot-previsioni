# Sveglia di riserva dal PC: ogni 10 minuti (Utilita' di pianificazione) controlla
# l'ultimo giro del bot su GitHub e, se e' piu' vecchio di 25 minuti e non ce n'e'
# uno in corso, avvia il workflow "Previsioni" con gh (gia' autenticato).
# Non servono chiavi in locale: il giro vero avviene su GitHub con i suoi segreti.
$repo = "Alessandro-barrale/bot-previsioni"
$workflow = "previsioni.yaml"
$gh = "C:\Program Files\GitHub CLI\gh.exe"
$log = Join-Path $PSScriptRoot "dati\sveglia.log"
$ora = Get-Date -Format "yyyy-MM-dd HH:mm"
try {
    $json = & $gh run list --repo $repo --workflow $workflow --limit 1 --json createdAt,status 2>$null | ConvertFrom-Json
    if ($json -and $json.Count -gt 0) {
        $ultimo = ([datetime]$json[0].createdAt).ToUniversalTime()
        $stato = $json[0].status
    } else {
        $ultimo = [datetime]::MinValue; $stato = "nessuno"
    }
    $minuti = [int]((Get-Date).ToUniversalTime() - $ultimo).TotalMinutes
    if ($stato -eq "in_progress" -or $stato -eq "queued") {
        Add-Content $log "$ora  giro in corso, niente da fare"
    } elseif ($minuti -gt 25) {
        & $gh workflow run $workflow --repo $repo 2>&1 | Out-Null
        Add-Content $log "$ora  SVEGLIATO (ultimo giro $minuti min fa)"
    } else {
        Add-Content $log "$ora  niente da fare (ultimo giro $minuti min fa)"
    }
} catch {
    Add-Content $log "$ora  errore: $_"
}
# Tiene il diario corto: ultime 500 righe.
if ((Get-Content $log | Measure-Object -Line).Lines -gt 600) {
    Get-Content $log -Tail 500 | Set-Content $log
}
