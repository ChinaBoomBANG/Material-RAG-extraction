' MaterialRAG silent launcher (double-click to run)
' Starts the local server in the background with pythonw (no console window),
' then opens the browser. Log: data\output\web.log
' Stop: just close the browser page (the server exits ~8s later).
Option Explicit

Dim fso, sh, root, py, logDir, logFile, q, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
q = Chr(34)     ' double quote, avoids nested-quote escaping

root = fso.GetParentFolderName(WScript.ScriptFullName)
py = root & "\my_venv\Scripts\pythonw.exe"

If Not fso.FileExists(py) Then
    MsgBox "Virtual environment not found:" & vbCrLf & py & vbCrLf & vbCrLf & _
           "Create it in the project root first:" & vbCrLf & _
           "  D:\python\python.exe -m venv my_venv" & vbCrLf & _
           "  my_venv\Scripts\python.exe -m pip install -r requirements.txt", _
           16, "MaterialRAG"
    WScript.Quit 1
End If

logDir = root & "\data\output"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
logFile = logDir & "\web.log"

' pythonw.exe has no console window; the app writes its own log file
cmd = q & py & q & " -m rag.cli serve --port 8848 --no-browser --log-file " & q & logFile & q
sh.Run cmd, 0, False

' Give the server ~2.5s to boot, then open the browser
WScript.Sleep 2500
sh.Run "http://127.0.0.1:8848/", 1, False
