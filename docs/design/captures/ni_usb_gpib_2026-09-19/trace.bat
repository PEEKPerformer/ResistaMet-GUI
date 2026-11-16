@echo off
rem usage: trace.bat <scenario>  -> oracle\<scenario>.etl + .xml + .log
set D=%~dp0.
logman stop usbtrace -ets >nul 2>&1
logman start usbtrace -p Microsoft-Windows-USB-USBPORT 0xFFFFFFFFFFFFFFFF 0xFF -nb 128 512 -bs 1024 -o %D%\%1.etl -ets >nul
python %D%\scenario.py %1 > %D%\%1.log 2>&1
logman stop usbtrace -ets >nul
type %D%\%1.log
tracerpt %D%\%1.etl -o %D%\%1.xml -of XML -y >nul
