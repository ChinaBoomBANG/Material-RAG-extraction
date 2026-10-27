' MaterialRAG launcher (double-click to run).
'
' ============================================================================
' KEEP THIS FILE PURE ASCII. No Chinese, no full-width punctuation, ever.
'
' Windows Script Host parses .vbs in the system ANSI codepage (cp936/GBK),
' NOT UTF-8. A UTF-8 Chinese string read as GBK takes 3 bytes per char, so an
' odd number of Chinese chars leaves one dangling lead byte that swallows the
' closing quote, and the engine dies BEFORE running anything with
' "Unterminated string constant" (0x800A0409). That is exactly what happened
' when the log messages in this file were first translated to Chinese.
' Chinese belongs in README / the web UI / logs written by Python (utf-8).
' ============================================================================
'
' What it does: start the server with pythonw (no console window), then open
' the browser as soon as the server drops its readiness marker.
' Logs: data\output\web.log (server) + data\output\launch.log (this script).
' Stop: close the browser page (idle -> exits after web.shutdown_grace_s,
'       15s by default; it will NOT stop while an extraction is running).
Option Explicit

Dim fso, sh, root, py, logDir, logFile, readyFile, launchLog, q, cmd, t0, waited

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
readyFile = logDir & "\web.ready"
launchLog = logDir & "\launch.log"

t0 = Timer()

' A server that is still running (page still open, or a launch that just
' happened) has left its readiness marker behind -> only open the browser.
' The marker carries the pid, so a marker abandoned by a hard-killed process
' (Task Manager, crash) is detected and discarded; otherwise the browser would
' be pointed at a dead port ("127.0.0.1 refused to connect").
If fso.FileExists(readyFile) Then
    If MarkerPidAlive() Then
        WriteLog "ready marker present and its process is alive; opening browser only"
        sh.Run "http://127.0.0.1:8848/", 1, False
        WScript.Quit 0
    End If
    WriteLog "stale ready marker (its process is gone); removed it, starting a fresh server"
    DeleteMarker
End If

WriteLog "starting service: " & py & " -m rag.cli serve --port 8848"
cmd = q & py & q & " -m rag.cli serve --port 8848 --no-browser --log-file " & q & logFile & q
sh.Run cmd, 0, False

' Poll the readiness marker the server writes on startup (100ms x 600 = 60s cap).
' Earlier versions polled /api/status: WinHTTP proxy/timeout behaviour is not
' controllable from VBS, so one stuck probe meant waiting out the full timeout
' (which the user experiences as "nothing happens after double-click"). A local
' file-existence check has no network semantics at all and is millisecond-level.
waited = 0
Do While waited < 60000
    If fso.FileExists(readyFile) Then Exit Do
    WScript.Sleep 100
    waited = waited + 100
Loop

If fso.FileExists(readyFile) Then
    WriteLog "service ready after " & FormatNumber(Timer() - t0, 2) & " s"
Else
    WriteLog "still not ready after " & FormatNumber(Timer() - t0, 2) & _
             " s; opening the browser anyway (the service probably failed to start - see web.log)"
End If

sh.Run "http://127.0.0.1:8848/", 1, False
WriteLog "browser opened; script total " & FormatNumber(Timer() - t0, 2) & " s"

' True when the marker names a process that is still running.
' Non-numeric content counts as alive: the server only writes the marker while
' it runs (an external instance writes "running").
' If WMI cannot answer, report alive - the conservative choice. Opening the
' browser at worst lands on a dead port; starting a second server would leave
' two processes fighting over the same port and the same pid file.
Function MarkerPidAlive()
    MarkerPidAlive = True
    On Error Resume Next
    Dim tf, pidText, svc, procs
    Set tf = fso.OpenTextFile(readyFile, 1, False)
    pidText = Trim(tf.ReadAll)
    tf.Close
    If Not IsNumeric(pidText) Then Exit Function
    Set svc = GetObject("winmgmts:\\.\root\cimv2")
    If Err.Number <> 0 Then Exit Function
    Set procs = svc.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE ProcessId=" & CLng(pidText))
    If Err.Number <> 0 Then Exit Function
    MarkerPidAlive = (procs.Count > 0)
    On Error GoTo 0
End Function

Sub DeleteMarker()
    On Error Resume Next
    fso.DeleteFile readyFile, True
    On Error GoTo 0
End Sub

Sub WriteLog(msg)
    On Error Resume Next
    Dim lf
    Set lf = fso.OpenTextFile(launchLog, 8, True)
    lf.WriteLine Now & "  " & msg
    lf.Close
    On Error GoTo 0
End Sub
