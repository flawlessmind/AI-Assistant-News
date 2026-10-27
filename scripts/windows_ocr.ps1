param(
    [Parameter(Mandatory = $true)][string]$JobsPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$LanguageTag = 'ru'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$asyncMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
    $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1

function Wait-WinRt($Operation, [Type]$ResultType) {
    $task = $asyncMethod.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    try {
        if (-not $task.Wait(60000)) { throw 'Windows OCR operation timed out' }
    } catch {
        if ($null -ne $task.Exception) {
            throw ("WinRT " + $ResultType.Name + ": " + $task.Exception.Flatten().InnerExceptions[0].Message)
        }
        throw
    }
    return $task.Result
}

$language = New-Object Windows.Globalization.Language($LanguageTag)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw "OCR language is not installed: $LanguageTag" }
$jobs = Get-Content -LiteralPath $JobsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$writer = New-Object System.IO.StreamWriter($OutputPath, $false, (New-Object System.Text.UTF8Encoding($false)))
try {
    foreach ($job in $jobs) {
        $stream = $null
        $fileStream = $null
        $bitmap = $null
        try {
            $fileStream = [System.IO.File]::OpenRead($job.image)
            $stream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($fileStream)
            $decoder = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Wait-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            if ($bitmap.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
                $bitmap.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
                throw 'Page image exceeds the OCR engine image dimension limit'
            }
            $result = Wait-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @($result.Lines | ForEach-Object {
                $words = @($_.Words | ForEach-Object {
                    @{ text = $_.Text; x = $_.BoundingRect.X; y = $_.BoundingRect.Y;
                       width = $_.BoundingRect.Width; height = $_.BoundingRect.Height }
                })
                @{ text = $_.Text; words = $words }
            })
            $record = @{ page = [int]$job.page; image = $job.image;
                text = (($result.Lines | ForEach-Object { $_.Text }) -join "`n");
                lines = $lines; language = $LanguageTag;
                width = $bitmap.PixelWidth; height = $bitmap.PixelHeight;
                extraction_method = 'windows_media_ocr'; confidence = $null }
            $writer.WriteLine(($record | ConvertTo-Json -Depth 8 -Compress))
            $writer.Flush()
            Write-Output ("Recognized page " + $job.page)
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
            if ($null -ne $fileStream) { $fileStream.Dispose() }
        }
    }
} finally { $writer.Dispose() }
