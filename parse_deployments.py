import subprocess
import re
import json

deployments = [
    "1d91d498-7b70-4881-85d2-8523f646a4a9",
    "409eb627-aaa0-415f-966b-2b506151897d",
    "52e5271d-b73d-4f82-8b11-ed4ae931ddf9",
    "c6bb48b1-134f-4bce-beb3-ebd1824d1a07",
    "60e5deb4-07cd-4681-8dc7-ac5420a45420",
    "811dac20-af77-424c-a14c-0649b2e7c99d",
    "2edc8353-4051-4008-b89f-e547f624f028",
    "30283b6d-a94c-4030-80d9-3e2556ae939f",
    "a4b8f8d2-2d3f-47c0-b0b3-0566f697e793",
    "ab8e0ecf-d878-430f-9882-38179731901c",
    "b86dc315-cf57-416d-85b3-d543328c9755",
    "7d82c372-4e52-4ffb-8d20-10aebdd3d940",
    "c993a556-b089-47ad-a001-c6d707b4efc2",
    "7a585c85-f0db-4236-a025-0ea5c4b80162",
    "da3e6c34-c48a-496e-83aa-455d0c65b9d0",
    "9be1fbbe-08a7-46cf-9be3-111e3f1dc0a8",
    "31ea5b71-ed5a-4107-9fbb-44d5a054cbdc",
    "4b92675e-dcdb-469c-bdf7-bf705cc24eaa",
    "6958078b-1bd9-4a7e-9c69-1e5b4dfd8326",
    "19a4c420-539a-4d68-b944-466dc1c092f9"
]

all_signals = []
raw_matches = []

for dep in deployments:
    print(f"Fetching logs for {dep[:8]}...")
    try:
        proc = subprocess.run(
            ["./railway.exe", "logs", "--deployment", dep],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=40
        )
        output = proc.stdout
        lines = output.splitlines()
        for line in lines:
            if "NEW SIGNAL:" in line or "Sent " in line and "Alert for" in line or "TRADE SIGNAL" in line or "ORDER FILLED" in line:
                raw_matches.append((dep[:8], line))
    except Exception as e:
        print(f"Error fetching {dep}: {e}")

print(f"\nTotal raw matches found: {len(raw_matches)}")
with open("raw_signals_found.txt", "w", encoding="utf-8") as f:
    for dep, line in raw_matches:
        f.write(f"[{dep}] {line}\n")

print("Saved to raw_signals_found.txt")
