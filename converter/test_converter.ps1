# PowerShell script to test converter with real photos
# Usage: .\test_converter.ps1

# Configuration
$API_URL = "http://localhost:8000/convert"
$API_KEY = "your_api_key_here"  # Replace with your actual API key
$OUTPUT_DIR = ".\converted"

# Create output directory if it doesn't exist
if (!(Test-Path -Path $OUTPUT_DIR)) {
    New-Item -ItemType Directory -Path $OUTPUT_DIR | Out-Null
    Write-Host "Created output directory: $OUTPUT_DIR" -ForegroundColor Green
}

# Function to convert a single file
function Convert-Image {
    param (
        [string]$FilePath,
        [int]$Quality = 85,
        [int]$MaxSide = 0
    )

    if (!(Test-Path -Path $FilePath)) {
        Write-Host "File not found: $FilePath" -ForegroundColor Red
        return
    }

    $fileName = [System.IO.Path]::GetFileName($FilePath)
    $fileSize = (Get-Item $FilePath).Length / 1MB

    Write-Host "`n================================" -ForegroundColor Cyan
    Write-Host "Converting: $fileName" -ForegroundColor Yellow
    Write-Host "File size: $([math]::Round($fileSize, 2)) MB" -ForegroundColor Gray

    try {
        # Prepare form data
        $form = @{
            file = Get-Item -Path $FilePath
            quality = $Quality
        }

        if ($MaxSide -gt 0) {
            $form.max_side = $MaxSide
        }

        # Prepare headers
        $headers = @{
            "X-API-KEY" = $API_KEY
        }

        # Send request
        Write-Host "Sending request..." -ForegroundColor Gray
        $response = Invoke-RestMethod -Uri $API_URL -Method Post -Form $form -Headers $headers -OutFile "$OUTPUT_DIR\$([System.IO.Path]::GetFileNameWithoutExtension($fileName)).jpg"

        Write-Host "✓ Success! Saved to: $OUTPUT_DIR\$([System.IO.Path]::GetFileNameWithoutExtension($fileName)).jpg" -ForegroundColor Green

    } catch {
        Write-Host "✗ Error: $($_.Exception.Message)" -ForegroundColor Red
        if ($_.ErrorDetails.Message) {
            Write-Host "Details: $($_.ErrorDetails.Message)" -ForegroundColor Red
        }
    }
}

# Test health endpoint
Write-Host "`n======== Testing Converter API ========" -ForegroundColor Cyan
Write-Host "Testing health endpoint..." -ForegroundColor Gray

try {
    $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get
    Write-Host "✓ Health check: $($health.status)" -ForegroundColor Green
} catch {
    Write-Host "✗ Health check failed. Is the server running?" -ForegroundColor Red
    Write-Host "Start the server with: python -m uvicorn app:app --host 0.0.0.0 --port 8000" -ForegroundColor Yellow
    exit 1
}

# Example: Convert images from parent directory
Write-Host "`n======== Converting Test Images ========" -ForegroundColor Cyan

# Test with DNG files
Convert-Image -FilePath "..\IMG_1211.DNG" -Quality 85
Convert-Image -FilePath "..\IMG_5254.DNG" -Quality 90

# Test with HEIF file
Convert-Image -FilePath "..\IMG_2557.HEIF" -Quality 85

# Test with CR3 file
Convert-Image -FilePath "..\IMG_3837.CR3" -Quality 85

# Test with resize
# Convert-Image -FilePath "..\IMG_1211.DNG" -Quality 85 -MaxSide 2048

Write-Host "`n======== Conversion Complete ========" -ForegroundColor Cyan
Write-Host "Check output files in: $OUTPUT_DIR" -ForegroundColor Green
