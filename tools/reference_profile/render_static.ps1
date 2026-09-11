# Local vector-to-raster QA; does not open/control a browser or alter sources.
param([ValidateSet('reference','reference-v1.1')][string]$Stem='reference')
Add-Type -AssemblyName System.Drawing
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$svgFile = Join-Path $taskRoot "outputs/reference_profile/$Stem.svg"
[xml]$svg = [System.IO.File]::ReadAllText($svgFile, [System.Text.Encoding]::UTF8)
$vb = $svg.DocumentElement.GetAttribute('viewBox').Split(' ') | ForEach-Object { [double]::Parse($_, [Globalization.CultureInfo]::InvariantCulture) }
$imgWidth = 1900
$imgHeight = [int]($imgWidth * $vb[3] / $vb[2])
$scale = $imgWidth / $vb[2]
$bitmap = New-Object System.Drawing.Bitmap($imgWidth, $imgHeight)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([System.Drawing.Color]::White)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$font = New-Object System.Drawing.Font('Microsoft YaHei', 10)
foreach ($node in $svg.DocumentElement.ChildNodes) {
    if ($node.LocalName -in @('polyline','polygon')) {
        $pairs = $node.GetAttribute('points').Split(' ')
        $points = [System.Drawing.PointF[]]@($pairs | ForEach-Object {
            $xy = $_.Split(',')
            $px = ([double]::Parse($xy[0], [Globalization.CultureInfo]::InvariantCulture) - $vb[0]) * $scale
            $py = ([double]::Parse($xy[1], [Globalization.CultureInfo]::InvariantCulture) - $vb[1]) * $scale
            New-Object System.Drawing.PointF([single]$px, [single]$py)
        })
        $color = [System.Drawing.ColorTranslator]::FromHtml($node.GetAttribute('stroke'))
        $pen = New-Object System.Drawing.Pen($color, 1)
        if ($node.LocalName -eq 'polygon') {
            $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(85,$color))
            $graphics.FillPolygon($brush,$points)
            $graphics.DrawPolygon($pen,$points)
            $brush.Dispose()
        } else { $graphics.DrawLines($pen,$points) }
        $pen.Dispose()
    } elseif ($node.LocalName -eq 'text') {
        $px = ([double]::Parse($node.GetAttribute('x'), [Globalization.CultureInfo]::InvariantCulture) - $vb[0]) * $scale
        $py = ([double]::Parse($node.GetAttribute('y'), [Globalization.CultureInfo]::InvariantCulture) - $vb[1]) * $scale
        $graphics.DrawString($node.InnerText,$font,[System.Drawing.Brushes]::Black,[single]$px,[single]$py)
    }
}
$outputFile = Join-Path $taskRoot "outputs/reference_profile/$Stem-static.png"
$bitmap.Save($outputFile, [System.Drawing.Imaging.ImageFormat]::Png)
$font.Dispose()
$graphics.Dispose()
$bitmap.Dispose()
Write-Output $outputFile
