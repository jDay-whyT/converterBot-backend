#!/usr/bin/env python3
"""Manual testing script for converter API with real image files."""
import sys
from pathlib import Path

# Add parent directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

import httpx
import time
import subprocess
import signal
from multiprocessing import Process

def start_server():
    """Start the FastAPI server."""
    import uvicorn
    import os
    os.environ["CONVERTER_API_KEY"] = "test_api_key_123"
    os.environ["MAX_FILE_MB"] = "50"

    from app import app
    uvicorn.run(app, host="127.0.0.1", port=8899, log_level="info")

def test_converter():
    """Test the converter with real files."""
    # Wait for server to start
    time.sleep(3)

    base_url = "http://127.0.0.1:8899"
    headers = {"X-API-KEY": "test_api_key_123"}

    # Test health endpoint
    print("=" * 80)
    print("Testing /health endpoint...")
    try:
        response = httpx.get(f"{base_url}/health", timeout=10)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.json()}")
    except Exception as e:
        print(f"ERROR: {e}")

    # Find test image files
    test_files = [
        ("../IMG_1211.DNG", "image/x-adobe-dng"),
        ("../IMG_2557.HEIF", "image/heif"),
        ("../IMG_3837.CR3", "image/x-canon-cr3"),
        ("../IMG_5254.DNG", "image/x-adobe-dng"),
    ]

    for file_path, mime_type in test_files:
        full_path = Path(__file__).parent / file_path
        if not full_path.exists():
            print(f"\nSkipping {file_path} - file not found")
            continue

        print("\n" + "=" * 80)
        print(f"Testing conversion of: {full_path.name}")
        print(f"File size: {full_path.stat().st_size / 1024 / 1024:.2f} MB")
        print(f"MIME type: {mime_type}")

        try:
            with open(full_path, "rb") as f:
                files = {"file": (full_path.name, f, "application/octet-stream")}
                data = {"quality": "85"}

                print(f"Sending POST request to {base_url}/convert...")
                response = httpx.post(
                    f"{base_url}/convert",
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=120,
                )

                print(f"Status code: {response.status_code}")

                if response.status_code == 200:
                    print(f"✓ Success! Output size: {len(response.content) / 1024:.2f} KB")
                    output_file = Path(f"/tmp/output_{full_path.stem}.jpg")
                    output_file.write_bytes(response.content)
                    print(f"Saved to: {output_file}")
                else:
                    print(f"✗ Error!")
                    try:
                        error_detail = response.json()
                        print(f"Error detail: {error_detail}")
                    except:
                        print(f"Response text: {response.text[:500]}")

        except Exception as e:
            print(f"✗ Exception: {type(e).__name__}: {e}")

if __name__ == "__main__":
    # Start server in background process
    server_process = Process(target=start_server)
    server_process.start()

    try:
        # Run tests
        test_converter()
    finally:
        # Stop server
        print("\n" + "=" * 80)
        print("Stopping server...")
        server_process.terminate()
        server_process.join(timeout=5)
        if server_process.is_alive():
            server_process.kill()
        print("Server stopped.")
