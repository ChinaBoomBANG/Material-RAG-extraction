' MaterialRAG silent launcher (double-click to run)
' Starts the local server in the background with pythonw (no console window),
' then opens the browser once the server writes its readiness marker.
' Logs: data\output\web.log (server) + data\output\launch.log (this script).
' Stop: just close the browser page (idle -> exits after web.shutdown_grace_s,
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

' 已有服务在跑（上次没关干净 / 页面还开着）：标记文件还在，直接开浏览器。
If fso.FileExists(readyFile) Then
    WriteLog "检测到就绪标记，认为服务已在运行，直接打开浏览器"
    sh.Run "http://127.0.0.1:8848/", 1, False
    WScript.Quit 0
End If

WriteLog "开始启动服务"
cmd = q & py & q & " -m rag.cli serve --port 8848 --no-browser --log-file " & q & logFile & q
sh.Run cmd, 0, False

' 轮询服务写下的就绪标记文件（最多 60 秒）。
' 早先的做法是轮询 /api/status：VBS 里 WinHttp 的代理与超时行为不可控，
' 一旦某次探测失败/卡住，就只能耗到上限才开浏览器（用户侧就是"双击后很久没反应"）。
' 文件判定是纯本地操作，不存在网络语义，可靠且是毫秒级。
waited = 0
Do While waited < 60000
    If fso.FileExists(readyFile) Then Exit Do
    WScript.Sleep 100
    waited = waited + 100
Loop

If fso.FileExists(readyFile) Then
    WriteLog "服务就绪，等待 " & FormatNumber(Timer() - t0, 2) & " 秒"
Else
    WriteLog "等待 " & FormatNumber(Timer() - t0, 2) & " 秒仍未就绪，仍尝试打开浏览器（服务可能启动失败，请看 web.log）"
End If

sh.Run "http://127.0.0.1:8848/", 1, False
WriteLog "已打开浏览器，脚本总耗时 " & FormatNumber(Timer() - t0, 2) & " 秒"

Sub WriteLog(msg)
    On Error Resume Next
    Dim f
    Set f = fso.OpenTextFile(launchLog, 8, True)
    f.WriteLine Now & "  " & msg
    f.Close
    On Error GoTo 0
End Sub
