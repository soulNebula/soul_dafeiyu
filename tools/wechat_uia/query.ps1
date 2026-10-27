param(
  [string]$Contact = "",
  [switch]$Title,
  [switch]$FindInput
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-WechatWindow {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, "mmui::MainWindow")
  return $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
}

function Get-Rect($el) {
  $r = $el.Current.BoundingRectangle
  return @{ x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height }
}

$result = @{ ok = $false; error = ""; window = $null; contact = $null; title = ""; input = $null }
$win = Get-WechatWindow
if ($null -eq $win) {
  $result.error = "window_not_found"
  $result | ConvertTo-Json -Compress
  exit 0
}
$result.window = @{ handle = [int]$win.Current.NativeWindowHandle; name = $win.Current.Name; class = $win.Current.ClassName; rect = (Get-Rect $win) }
$result.ok = $true

if ($Contact -ne "") {
  $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  $match = $null
  foreach ($el in $all) {
    if ($el.Current.ClassName -ne "mmui::ChatSessionCell") { continue }
    $n = [string]$el.Current.Name
    if ($n -eq $Contact) { $match = $el; break }
  }
  if ($null -eq $match) {
    foreach ($el in $all) {
      if ($el.Current.ClassName -ne "mmui::ChatSessionCell") { continue }
      $n = [string]$el.Current.Name
      if ($n -ne "" -and $n.Contains($Contact)) { $match = $el; break }
    }
  }
  if ($null -ne $match) {
    $result.contact = @{ found = $true; name = [string]$match.Current.Name; rect = (Get-Rect $match) }
  } else {
    $result.contact = @{ found = $false }
  }
}

if ($Title) {
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, "mmui::ChatTitleBarChatSingleView")
  $bar = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
  $text = ""
  if ($null -ne $bar) {
    $kids = $bar.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($k in $kids) {
      $nm = [string]$k.Current.Name
      if ($nm -ne "" -and $nm -ne "mmui::ChatTitleBarChatSingleView") { $text = $nm; break }
    }
    if ($text -eq "") { $text = [string]$bar.Current.Name }
  }
  $result.title = $text
}

if ($FindInput) {
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
  $edits = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
  $best = $null
  $bestBottom = -1
  foreach ($e in $edits) {
    $r = $e.Current.BoundingRectangle
    if ([int]$r.Top -gt $bestBottom -and [int]$r.Width -gt 100) {
      $bestBottom = [int]$r.Top
      $best = $e
    }
  }
  if ($null -ne $best) {
    $result.input = @{ found = $true; name = [string]$best.Current.Name; rect = (Get-Rect $best) }
  } else {
    $result.input = @{ found = $false }
  }
}

$result | ConvertTo-Json -Compress -Depth 5
