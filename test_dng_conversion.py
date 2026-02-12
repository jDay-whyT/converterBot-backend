#!/usr/bin/env python3
"""
Test script to simulate DNG file conversion through the converter
"""
import os
import sys
import subprocess
from pathlib import Path

def check_file_info(file_path):
    """Display basic file information"""
    file_path = Path(file_path)
    if not file_path.exists():
        print(f"❌ File not found: {file_path}")
        return False

    size_mb = file_path.stat().st_size / (1024 * 1024)
    print(f"\n📁 File Information:")
    print(f"   Path: {file_path}")
    print(f"   Size: {size_mb:.2f} MB")
    print(f"   Exists: ✓")
    return True

def check_tool(tool_name):
    """Check if a conversion tool is available"""
    try:
        result = subprocess.run(['which', tool_name],
                              capture_output=True,
                              text=True,
                              timeout=5)
        if result.returncode == 0:
            print(f"   ✓ {tool_name}: {result.stdout.strip()}")
            return True
        else:
            print(f"   ✗ {tool_name}: NOT INSTALLED")
            return False
    except Exception as e:
        print(f"   ✗ {tool_name}: ERROR ({e})")
        return False

def simulate_conversion_pipeline(file_path):
    """Simulate the converter's multi-stage RAW conversion pipeline"""
    print(f"\n🔄 Simulating Conversion Pipeline for {Path(file_path).name}")
    print("=" * 70)

    tools = [
        ('exiftool', 'Extract embedded preview from RAW metadata'),
        ('darktable-cli', 'Professional RAW rendering (180s timeout)'),
        ('rawtherapee-cli', 'RAW → TIFF → JPEG conversion (120s timeout)'),
        ('dcraw_emu', 'libraw decoder with cubic interpolation'),
        ('dcraw', 'Fallback RAW decoder'),
        ('magick', 'ImageMagick for format conversion'),
    ]

    print("\n📋 Conversion Fallback Chain:")
    available_tools = []
    for tool, description in tools:
        is_available = check_tool(tool)
        if is_available:
            available_tools.append(tool)
        print(f"      Stage: {description}")

    print(f"\n📊 Summary:")
    print(f"   Total conversion stages: {len(tools)}")
    print(f"   Available tools: {len(available_tools)}/{len(tools)}")
    print(f"   Missing tools: {len(tools) - len(available_tools)}")

    if len(available_tools) == 0:
        print(f"\n⚠️  WARNING: No conversion tools available!")
        print(f"   The converter would fail to process this DNG file.")
        print(f"\n💡 To run conversion, install:")
        print(f"   apt-get install imagemagick libheif-examples libraw-bin")
        print(f"   apt-get install dcraw darktable rawtherapee exiftool")
    elif len(available_tools) < len(tools):
        print(f"\n⚠️  Some tools missing, but conversion might succeed with available tools")
    else:
        print(f"\n✓ All conversion tools available!")

    return available_tools

def analyze_conversion_logic():
    """Explain the converter's logic for DNG files"""
    print(f"\n📚 How the Converter Processes DNG Files:")
    print("=" * 70)
    print("""
1. File Type Detection (exiftool):
   - Detects MIME type and file format
   - Routes to 'raw' conversion pipeline for DNG files

2. Multi-Stage RAW Conversion (with fallback):

   Stage 1: Extract Embedded Preview
   - Uses exiftool to extract PreviewImage/JpgFromRaw
   - Fastest method if preview is available
   - Quality validation: min 200x200px, no black bands

   Stage 2: Darktable Rendering
   - Full RAW processing with professional algorithms
   - Timeout: 180 seconds
   - Output: High-quality JPEG

   Stage 3: RawTherapee
   - Decodes RAW → TIFF → JPEG via ImageMagick
   - Timeout: 120 seconds
   - Reliable for most RAW formats

   Stage 4: dcraw_emu (libraw)
   - Parameters: -T (TIFF), -w (white balance), -q3 (cubic)
   - -H2 (highlight recovery), -6 (16-bit output)

   Stage 5: dcraw (fallback)
   - Last resort for maximum compatibility
   - Same parameters as dcraw_emu

3. Quality Validation:
   - Checks dimensions (minimum 200x200)
   - Detects black bands (Apple ProRAW issue)
   - Validates output size (minimum 50KB)
   - If validation fails, tries next stage

4. Output:
   - Format: JPEG
   - Quality: 92 (default, configurable 1-100)
   - Colorspace: sRGB
   - Auto-orientation from EXIF
   - Metadata stripped
""")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_dng_conversion.py <path_to_dng_file>")
        sys.exit(1)

    dng_file = sys.argv[1]

    print("\n" + "=" * 70)
    print("🎯 DNG File Converter - Test Simulation")
    print("=" * 70)

    # Check file
    if not check_file_info(dng_file):
        sys.exit(1)

    # Simulate conversion
    available_tools = simulate_conversion_pipeline(dng_file)

    # Explain logic
    analyze_conversion_logic()

    # Final verdict
    print("\n" + "=" * 70)
    print("🎯 Conversion Verdict:")
    print("=" * 70)

    if len(available_tools) > 0:
        print(f"✓ Conversion would succeed using: {', '.join(available_tools)}")
        print(f"\n📝 Expected output:")
        print(f"   - Format: JPEG")
        print(f"   - Quality: 92")
        print(f"   - Processing: {available_tools[0]} (primary tool)")
    else:
        print(f"✗ Conversion would FAIL - no tools available")
        print(f"\n🐳 To test with Docker:")
        print(f"   cd converter")
        print(f"   docker build -t photo-converter:test .")
        print(f"   docker run -p 8080:8080 -e CONVERTER_API_KEY=test photo-converter:test")
        print(f"   curl -F 'file=@../IMG_1211.DNG' -H 'X-API-KEY: test' \\")
        print(f"        http://localhost:8080/convert -o output.jpg")

    print("\n")

if __name__ == "__main__":
    main()
