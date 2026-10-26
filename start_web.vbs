' MaterialRAG silent launcher (double-click to run)
' Starts the local server in the background with pythonw (no console window),
' then opens the browser once the server actually answers. Log: data\output\web.log
' Stop: just close the browser page (idle -> exits after web.shutdown_grace_s,
'       15s by default; it will NOT stop while an extraction is running).
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

' Poll /api/status until the server answers (max 30s), then open the browser.
' A fixed 2.5s sleep used to fire too early: the index takes a few seconds to
' load, so the browser hit "127.0.0.1 refused to connect" and only worked after
' a manual refresh. Proxy lookups are disabled explicitly, because a system-wide
' proxy would otherwise intercept even 127.0.0.1 and report a false negative.
Dim waited
waited = 0
Do While waited < 30000
    If ServerReady() Then Exit Do
    WScript.Sleep 300
    waited = waited + 300
Loop
sh.Run "http://127.0.0.1:8848/", 1, False

Function ServerReady()
    ' Returns True when the local server responds; never uses a proxy.
    ' Timeouts are set explicitly: WinHTTP defaults can wait far longer than
    ' intended on a not-yet-listening port, which shows up as "double-click
    ' does nothing for a while".
    On Error Resume Next
    Dim w
    Set w = CreateObject("WinHttp.WinHttpRequest.5.1")
    w.SetAutoLogProxy 0
    w.SetTimeouts 500, 500, 1000, 1000
    w.Open "GET", "http://127.0.0.1:8848/api/status", False
    w.Send
    ServerReady = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
End Function
