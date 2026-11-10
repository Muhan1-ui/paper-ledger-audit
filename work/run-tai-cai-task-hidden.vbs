Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\TaiCaiTracker\work\run-tai-cai-task.ps1"" -Mode prematch -PythonPath ""D:\TaiCaiTracker\.venv\Scripts\python.exe""", 0, False
