Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, "mmui::MainWindow")
$win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
if ($null -eq $win) { Write-Output "NO WINDOW"; exit 1 }
Write-Output ("WINDOW " + $win.Current.Name + " " + $win.Current.BoundingRectangle)
$all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
Write-Output ("DESCENDANTS " + $all.Count)
$i = 0
foreach ($el in $all) {
  if ($i -ge 400) { break }
  $r = $el.Current.BoundingRectangle
  Write-Output ("{0}|{1}|{2}|{3},{4},{5},{6}" -f $el.Current.ControlType.ProgrammaticName, $el.Current.ClassName, ($el.Current.Name -replace "\|", "/"), [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
  $i++
}
