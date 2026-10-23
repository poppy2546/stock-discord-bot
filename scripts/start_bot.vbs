Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
WshShell.CurrentDirectory = strDir

' ปิด instance เก่าที่ค้างอยู่ก่อนเริ่มใหม่ เพื่อป้องกันพอร์ตซ้ำหรือโปรเซสชนกัน
WshShell.Run "powershell -WindowStyle Hidden -Command ""Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*.exe') -and ($_.CommandLine -like '*discord_bot.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }""", 0, True

' รันบอทผ่าน pythonw ในโหมดซ่อนหน้าต่าง 100%
pythonwPath = strDir & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(pythonwPath) Then
    WshShell.Run "powershell -NoProfile -WindowStyle Hidden -Command ""Start-Process -FilePath '" & pythonwPath & "' -ArgumentList 'discord_bot.py' -WorkingDirectory '" & strDir & "' -WindowStyle Hidden""", 0, True
Else
    WshShell.Run "powershell -NoProfile -WindowStyle Hidden -Command ""Start-Process -FilePath 'pythonw' -ArgumentList 'discord_bot.py' -WorkingDirectory '" & strDir & "' -WindowStyle Hidden""", 0, True
End If
