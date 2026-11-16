param([Parameter(Mandatory=$true)][string]$Scenario)
$D = $PSScriptRoot
$pcap = "$D\$Scenario.pcap"
if (Test-Path $pcap) { Remove-Item $pcap }
$p = Start-Process -FilePath 'C:\Program Files\USBPcap\USBPcapCMD.exe' -ArgumentList @('-d','\\.\USBPcap1','-o',$pcap,'-s','65535','-b','4194304','--devices','2') -PassThru -WindowStyle Hidden -RedirectStandardOutput "$D\$Scenario.cap.out" -RedirectStandardError "$D\$Scenario.cap.err"
Start-Sleep -Seconds 2
& python "$D\scenario.py" $Scenario 2>&1 | Tee-Object -FilePath "$D\$Scenario.stdout.txt"
Start-Sleep -Seconds 1
Stop-Process -Id $p.Id -Force
Start-Sleep -Milliseconds 500
$f = Get-Item $pcap
"pcap: {0} bytes  sha256 {1}" -f $f.Length, (Get-FileHash $pcap -Algorithm SHA256).Hash.ToLower()
