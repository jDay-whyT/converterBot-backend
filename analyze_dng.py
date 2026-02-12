#!/usr/bin/env python3
"""
Analyze DNG file structure and headers
DNG is based on TIFF format with additional Adobe tags
"""
import struct
import sys
from pathlib import Path

def read_tiff_header(file_path):
    """Read TIFF/DNG header information"""
    with open(file_path, 'rb') as f:
        # Read byte order
        byte_order = f.read(2)
        if byte_order == b'II':
            endian = '<'  # Little-endian
            print("   Byte Order: Little-endian (Intel)")
        elif byte_order == b'MM':
            endian = '>'  # Big-endian
            print("   Byte Order: Big-endian (Motorola)")
        else:
            print(f"   ⚠️ Unknown byte order: {byte_order}")
            return None

        # Read TIFF magic number (should be 42)
        magic = struct.unpack(f'{endian}H', f.read(2))[0]
        print(f"   TIFF Magic Number: {magic} {'✓' if magic == 42 else '✗'}")

        # Read offset to first IFD (Image File Directory)
        ifd_offset = struct.unpack(f'{endian}I', f.read(4))[0]
        print(f"   First IFD Offset: 0x{ifd_offset:08X} ({ifd_offset} bytes)")

        return endian, ifd_offset

def read_ifd_tags(file_path, endian, ifd_offset, max_tags=20):
    """Read IFD tags to extract metadata"""
    # Common TIFF/DNG tags
    TAG_NAMES = {
        254: 'NewSubfileType',
        256: 'ImageWidth',
        257: 'ImageLength',
        258: 'BitsPerSample',
        259: 'Compression',
        262: 'PhotometricInterpretation',
        271: 'Make',
        272: 'Model',
        273: 'StripOffsets',
        274: 'Orientation',
        277: 'SamplesPerPixel',
        278: 'RowsPerStrip',
        279: 'StripByteCounts',
        282: 'XResolution',
        283: 'YResolution',
        284: 'PlanarConfiguration',
        296: 'ResolutionUnit',
        305: 'Software',
        306: 'DateTime',
        315: 'Artist',
        33432: 'Copyright',
        33434: 'ExposureTime',
        33437: 'FNumber',
        34665: 'ExifIFDPointer',
        34853: 'GPSIFDPointer',
        50706: 'DNGVersion',
        50707: 'DNGBackwardVersion',
        50708: 'UniqueCameraModel',
        50721: 'ColorMatrix1',
        50722: 'ColorMatrix2',
        50778: 'CalibrationIlluminant1',
        50779: 'CalibrationIlluminant2',
        50740: 'DNGPrivateData',
    }

    with open(file_path, 'rb') as f:
        f.seek(ifd_offset)

        # Read number of tags in IFD
        num_tags = struct.unpack(f'{endian}H', f.read(2))[0]
        print(f"\n   Number of IFD Tags: {num_tags}")

        print(f"\n   📋 Sample Tags (showing first {min(max_tags, num_tags)}):")

        tags_data = {}
        for i in range(min(max_tags, num_tags)):
            tag_id = struct.unpack(f'{endian}H', f.read(2))[0]
            tag_type = struct.unpack(f'{endian}H', f.read(2))[0]
            tag_count = struct.unpack(f'{endian}I', f.read(4))[0]
            tag_value = f.read(4)  # Value or offset

            tag_name = TAG_NAMES.get(tag_id, f'UnknownTag({tag_id})')

            # Try to decode simple values
            if tag_type == 3 and tag_count == 1:  # SHORT
                value = struct.unpack(f'{endian}H', tag_value[:2])[0]
            elif tag_type == 4 and tag_count == 1:  # LONG
                value = struct.unpack(f'{endian}I', tag_value)[0]
            else:
                value = f'<complex, type={tag_type}, count={tag_count}>'

            tags_data[tag_id] = value
            print(f"      {tag_name}: {value}")

        return tags_data

def analyze_dng_file(file_path):
    """Main analysis function"""
    file_path = Path(file_path)

    print("\n" + "=" * 70)
    print("📸 DNG File Analysis")
    print("=" * 70)

    if not file_path.exists():
        print(f"❌ File not found: {file_path}")
        return

    size_mb = file_path.stat().st_size / (1024 * 1024)
    print(f"\n📁 File Info:")
    print(f"   Path: {file_path}")
    print(f"   Size: {size_mb:.2f} MB ({file_path.stat().st_size:,} bytes)")

    # Read file header (first 256 bytes)
    with open(file_path, 'rb') as f:
        header = f.read(256)

    print(f"\n🔍 File Header Analysis:")

    # Check for DNG/TIFF signature
    result = read_tiff_header(file_path)

    if result:
        endian, ifd_offset = result

        # Read IFD tags
        print(f"\n📊 IFD (Image File Directory) Tags:")
        tags = read_ifd_tags(file_path, endian, ifd_offset)

        # Extract key information
        print(f"\n📷 Image Properties:")
        if 256 in tags:
            print(f"   Width: {tags[256]} pixels")
        if 257 in tags:
            print(f"   Height: {tags[257]} pixels")
        if 258 in tags:
            print(f"   Bits Per Sample: {tags[258]}")
        if 277 in tags:
            print(f"   Samples Per Pixel: {tags[277]}")

    print(f"\n🔧 How This File Would Be Processed:")
    print("=" * 70)
    print("""
Your converter would process this DNG file through these stages:

1️⃣ File Type Detection:
   - Use exiftool to detect format: image/x-adobe-dng
   - Route to RAW conversion pipeline

2️⃣ Conversion Attempt #1 - Extract Embedded Preview:
   - Run: exiftool -b -PreviewImage IMG_1211.DNG > preview.jpg
   - If preview exists, validate quality and return
   - Fastest method, usually works for modern cameras

3️⃣ Conversion Attempt #2 - Darktable:
   - Run: darktable-cli IMG_1211.DNG output.jpg
   - Full RAW processing with professional algorithms
   - Timeout: 180 seconds

4️⃣ Conversion Attempt #3 - RawTherapee:
   - Run: rawtherapee-cli -o output.tiff -c IMG_1211.DNG
   - Then: magick output.tiff -quality 92 output.jpg
   - Timeout: 120 seconds

5️⃣ Conversion Attempt #4 - dcraw_emu:
   - Run: dcraw_emu -T -w -q3 -H2 -6 IMG_1211.DNG
   - Then: magick IMG_1211.tiff -quality 92 output.jpg

6️⃣ Conversion Attempt #5 - dcraw (Fallback):
   - Run: dcraw -T -w -q3 -H2 -6 IMG_1211.DNG
   - Then: magick IMG_1211.tiff -quality 92 output.jpg

Each stage validates:
- Minimum dimensions: 200x200 pixels
- No black bands (Apple ProRAW artifact detection)
- Minimum file size: 50KB
- Valid JPEG format
""")

    print("\n✅ Analysis Complete!")
    print("=" * 70)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_dng.py <path_to_dng_file>")
        sys.exit(1)

    analyze_dng_file(sys.argv[1])
