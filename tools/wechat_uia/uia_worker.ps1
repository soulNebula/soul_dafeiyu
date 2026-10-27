param()

# P0-2：常驻微信 UIA 查询 worker。
# stdin: 每行一个 JSON 命令 {"cmd":"contact","name":"..."} / {"cmd":"title"} / {"cmd":"input"}
# stdout: 每行一个 JSON：{ok,window,contact,input,search_box,title,error}
# 只读查找，不点击、不输入、不发送。复用 uia_query.ps1 的查找逻辑，进程常驻免去
# 每次 PowerShell/UIA 启动开销（bench.py 对比）。

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Json($value) {
    try {
        Write-Output ($value | ConvertTo-Json -Compress -Depth 8)
    } catch {
        Write-Output ('{"ok":false,"error":"json_encode_failed"}')
    }
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
} catch {
    # 循环里每次请求都返回失败，进程不退出（客户端可重试/降级）
    while ($null -ne [Console]::In.ReadLine()) {
        Write-Json @{ ok = $false; window = $null; contact = $null; input = $null
                      search_box = $null; title = ""
                      error = ("uia_unavailable: " + $_.Exception.Message) }
    }
    exit 0
}

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]

function Rect-Of($element) {
    try {
        $rect = $element.Current.BoundingRectangle
        if ($null -eq $rect) { return $null }
        if ($rect.Width -le 0 -or $rect.Height -le 0) { return $null }
        return @{
            x = [int][Math]::Round($rect.X)
            y = [int][Math]::Round($rect.Y)
            w = [int][Math]::Round($rect.Width)
            h = [int][Math]::Round($rect.Height)
        }
    } catch { return $null }
}

function Find-WeChatWindow {
    $children = $AE::RootElement.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($child in $children) {
        try {
            $cur = $child.Current
            if ($cur.ClassName -eq "mmui::MainWindow" -or $cur.Name -eq "微信") {
                return $child
            }
        } catch { continue }
    }
    return $null
}

function In-LeftThird($rect, $winRect) {
    if ($null -eq $rect -or $null -eq $winRect) { return $false }
    $limit = $winRect.x + [int]($winRect.w / 3)
    $cx = $rect.x + [int]($rect.w / 2)
    return ($cx -lt $limit)
}

function In-LowerHalf($rect, $winRect) {
    if ($null -eq $rect -or $null -eq $winRect) { return $false }
    $limit = $winRect.y + [int]($winRect.h / 2)
    $cy = $rect.y + [int]($rect.h / 2)
    return ($cy -gt $limit)
}

function Query-Once([string]$cmd, [string]$name) {
    $result = @{ ok = $false; window = $null; contact = $null; input = $null
                 search_box = $null; title = ""; error = "" }
    $winElement = Find-WeChatWindow
    if ($null -eq $winElement) {
        $result.error = "wechat_window_not_found"
        return $result
    }
    $winCurrent = $winElement.Current
    $winRect = Rect-Of $winElement
    $result.window = @{
        handle = [int]$winCurrent.NativeWindowHandle
        name = [string]$winCurrent.Name
        class = [string]$winCurrent.ClassName
        rect = $winRect
    }
    $result.ok = $true

    $contactRect = $null
    $inputRect = $null
    $searchRect = $null
    $exact = $false
    $titleText = ""

    $elements = $null
    try { $elements = $winElement.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition) }
    catch { $elements = $null }

    if ($null -ne $elements) {
        $limit = [Math]::Min($elements.Count, 8000)
        for ($i = 0; $i -lt $limit; $i++) {
            $element = $null
            try { $element = $elements.Item($i) } catch { continue }
            if ($null -eq $element) { continue }
            $cur = $null
            try { $cur = $element.Current } catch { continue }
            $elName = [string]$cur.Name
            $type = ""
            try { $type = [string]$cur.ControlType.ProgrammaticName } catch { }

            # contact：只在左 1/3 找 ChatSessionCell 同名/包含
            if ($cmd -eq "contact" -and $name -ne "" -and $null -eq $contactRect) {
                $rect = Rect-Of $element
                if ($null -ne $rect -and $elName -ne "") {
                    if ($elName -eq $name -or $elName.Contains($name)) {
                        if (In-LeftThird $rect $winRect) {
                            $contactRect = $rect
                            $exact = ($elName -eq $name)
                        }
                    }
                }
            }
            # 搜索框：上部宽 Edit/Document
            if ($null -eq $searchRect -and $elName -ne "") {
                if ($type -like "*Edit*" -or $type -like "*Document*") {
                    $rect = Rect-Of $element
                    if ($null -ne $rect -and $rect.w -ge 60 -and
                        $rect.y -lt ($winRect.y + [int]($winRect.h * 0.22)) -and
                        $rect.x -lt ($winRect.x + [int]($winRect.w * 0.75))) {
                        $searchRect = $rect
                    }
                }
            }
            # 输入框：下半区面积最大的 Edit/Document
            if ($cmd -eq "input" -and ($type -like "*Edit*" -or $type -like "*Document*")) {
                $rect = Rect-Of $element
                if ($null -ne $rect -and (In-LowerHalf $rect $winRect)) {
                    if ($null -eq $inputRect -or ($rect.w * $rect.h) -gt ($inputRect.w * $inputRect.h)) {
                        $inputRect = $rect
                    }
                }
            }
            # 标题：ChatTitleBarChatSingleView 下第一个非空文本
            if ($cmd -eq "title" -and $titleText -eq "") {
                if ($cur.ClassName -eq "mmui::ChatTitleBarChatSingleView" -or
                    $cur.ClassName -like "*ChatTitleBar*") {
                    if ($elName -ne "" -and $elName -notlike "mmui::*") {
                        $titleText = $elName
                    }
                }
            }
        }
    }

    if ($null -ne $contactRect) {
        $result.contact = @{ found = $true; exact = $exact; x = $contactRect.x; y = $contactRect.y
                             w = $contactRect.w; h = $contactRect.h }
    } else {
        $result.contact = @{ found = $false }
    }
    if ($null -ne $inputRect) {
        $result.input = @{ found = $true; x = $inputRect.x; y = $inputRect.y
                           w = $inputRect.w; h = $inputRect.h }
    } else {
        $result.input = @{ found = $false }
    }
    if ($null -ne $searchRect) {
        $result.search_box = @{ found = $true; x = $searchRect.x; y = $searchRect.y
                                w = $searchRect.w; h = $searchRect.h }
    } else {
        $result.search_box = @{ found = $false }
    }
    $result.title = $titleText
    return $result
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq "") { continue }
    $request = $null
    try {
        $request = $line | ConvertFrom-Json
    } catch {
        Write-Json @{ ok = $false; window = $null; contact = $null; input = $null
                      search_box = $null; title = ""; error = "bad_json" }
        continue
    }
    $cmd = ""
    try { $cmd = [string]$request.cmd } catch { $cmd = "" }
    $name = ""
    try { $name = [string]$request.name } catch { $name = "" }
    if ($cmd -notin @("contact", "title", "input")) {
        Write-Json @{ ok = $false; window = $null; contact = $null; input = $null
                      search_box = $null; title = ""; error = "unknown_cmd" }
        continue
    }
    try {
        Write-Json (Query-Once $cmd $name)
    } catch {
        Write-Json @{ ok = $false; window = $null; contact = $null; input = $null
                      search_box = $null; title = ""
                      error = ("uia_error: " + $_.Exception.Message) }
    }
}
exit 0
