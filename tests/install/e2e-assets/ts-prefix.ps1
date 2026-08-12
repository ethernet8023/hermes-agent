# Add-TsPrefix: prefix each pipeline line with [+MM:SS] relative to the
# moment the pipeline started (the playback.html leg player's sync axis).
#
# Usage (dot-sourced from a driver):
#   & cmd 2>&1 | Add-TsPrefix | Out-File -Encoding UTF8 $log
#
# Call under the relaxed-EAP dance the driver already uses around native
# invocations (merging stderr through a pipe under EAP=Stop turns native
# stderr chatter into a terminating NativeCommandError).

$script:TsPrefixStart = Get-Date

function Add-TsPrefix {
    process {
        $t = (Get-Date) - $script:TsPrefixStart
        "[+{0:D2}:{1:D2}] {2}" -f [math]::Floor($t.TotalMinutes), [math]::Floor($t.TotalSeconds % 60), $_
    }
}
