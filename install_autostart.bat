@echo off
rem Pasang pemicu: worker otomatis jalan (tersembunyi) setiap kali Windows login.
rem Untuk mencabut: jalankan uninstall_autostart.bat
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\tracker_hourly_worker.vbs"
> "%VBS%" echo CreateObject("WScript.Shell").Run """%~dp0start_worker.bat""", 0, False
echo Autostart terpasang: %VBS%
