param(
    [string]$Contact = "",
    [switch]$FindInput
)

# P0-send：只读的微信 UIA 查询脚本。
# 输出一行 JSON：{ok,window,contact,input,search_box,error}
# 只做查找，不点击、不输入（点击/输入由 Python 侧完成）。

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # PowerShell 5.1 里 OutputEncoding 设置失败不影响结果
}

function Write-Json($value) {
    Write-Output ($value | ConvertTo-Json -Compress -Depth 8)
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
} catch {
    Write-Json @{ ok = $false; window = $null; contact = $null; input = $null;
                  search_box = $null; error = ("uia_unavailable: " + $_.Exception.Message) }
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
    } catch {
        return $null
    }
}

function Find-WeChatWindow {
    $children = $AE::RootElement.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($child in $children) {
        try {
            $current = $child.Current
            if ($current.ClassName -eq "mmui::MainWindow" -or $current.Name -eq "微信") {
                return $child
            }
        } catch {
            continue
        }
    }
    return $null
}

function In-LeftThird($rect, $winRect) {
    if ($null -eq $rect -or $null -eq $winRect) { return $false }
    $limit = $winRect.x + [int]($winRect.w / 3)
    $centerX = $rect.x + [int]($rect.w / 2)
    return ($centerX -lt $limit)
}

function In-LowerHalf($rect, $winRect) {
    if ($null -eq $rect -or $null -eq $winRect) { return $false }
    $limit = $winRect.y + [int]($winRect.h / 2)
    $centerY = $rect.y + [int]($rect.h / 2)
    return ($centerY -gt $limit)
}

try {
    $winElement = Find-WeChatWindow
    if ($null -eq $winElement) {
        Write-Json @{ ok = $false; window = $null; contact = $null; input = $null;
                      search_box = $null; error = "wechat_window_not_found" }
        exit 0
    }
    $winCurrent = $winElement.Current
    $winRect = Rect-Of $winElement
    $window = @{
        handle = [int]$winCurrent.NativeWindowHandle
        name = [string]$winCurrent.Name
        class = [string]$winCurrent.ClassName
        rect = $winRect
    }

    $contactRect = $null
    $inputRect = $null
    $searchRect = $null
    $exact = $false
    $elements = $null
    try {
        $elements = $winElement.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    } catch {
        $elements = $null
    }

    if ($null -ne $elements) {
        $limit = [Math]::Min($elements.Count, 8000)
        for ($i = 0; $i -lt $limit; $i++) {
            $element = $null
            try { $element = $elements.Item($i) } catch { continue }
            if ($null -eq $element) { continue }
            $rect = Rect-Of $element
            if ($null -eq $rect) { continue }
            $cur = $null
            try { $cur = $element.Current } catch { continue }
            $name = [string]$cur.Name
            $type = ""
            try { $type = [string]$cur.ControlType.ProgrammaticName } catch { $type = "" }

            if ($Contact -ne "" -and $null -eq $contactRect -and $name -ne "") {
                if ($name -eq $Contact -or $name.Contains($Contact)) {
                    if (In-LeftThird $rect $winRect) {
                        $contactRect = $rect
                        $exact = ($name -eq $Contact)
                    }
                }
            }
            if ($null -eq $searchRect -and $name -ne "") {
                if ($type -like "*Edit*" -or $type -like "*Document*") {
                    if ($rect.y -lt ($winRect.y + [int]($winRect.h * 0.22)) -and
                        $rect.x -lt ($winRect.x + [int]($winRect.w * 0.75)) -and
                        $rect.w -ge 60) {
                        $searchRect = $rect
                    }
                }
            }
            if ($FindInput -and ($type -like "*Edit*" -or $type -like "*Document*")) {
                if (In-LowerHalf $rect $winRect) {
                    if ($null -eq $inputRect) {
                        $inputRect = $rect
                    } else {
                        # 取面积最大的输入框（聊天输入区通常最大）
                        if (($rect.w * $rect.h) -gt ($inputRect.w * $inputRect.h)) {
                            $inputRect = $rect
                        }
                    }
                }
            }
        }
    }

    $contactInfo = $null
    if ($null -ne $contactRect) {
        $contactInfo = @{ found = $true; exact = $exact; x = $contactRect.x; y = $contactRect.y;
                      w = $contactRect.w; h = $contactRect.h }
    } else {
        $contactInfo = @{ found = $false }
    }
    $inputInfo = $null
    if ($null -ne $inputRect) {
        $inputInfo = @{ found = $true; x = $inputRect.x; y = $inputRect.y;
                    w = $inputRect.w; h = $inputRect.h }
    } else {
        $inputInfo = @{ found = $false }
    }
    $searchBox = $null
    if ($null -ne $searchRect) {
        $searchBox = @{ found = $true; x = $searchRect.x; y = $searchRect.y;
                        w = $searchRect.w; h = $searchRect.h }
    } else {
        $searchBox = @{ found = $false }
    }

    Write-Json @{
        ok = $true
        window = $window
        contact = $contactInfo
        input = $inputInfo
        search_box = $searchBox
        error = ""
    }
} catch {
    Write-Json @{ ok = $false; window = $null; contact = $null; input = $null;
                  search_box = $null; error = ("uia_error: " + $_.Exception.Message) }
}
exit 0
