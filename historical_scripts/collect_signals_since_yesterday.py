import subprocess
import re
import json

deployments = [
    "1efb7cf3-a89e-44ee-90dd-b64dd5a8553d",
    "46a36f54-6b54-43da-812e-591022b6f9d6",
    "88b4d68b-c57a-4e61-b483-58ed93cf5db5",
    "928ae976-1786-4a37-9195-92ed0df1ef2d",
    "40601aad-bc32-4a61-9da4-469e0d8e4b99",
    "094c88af-f27e-43a7-8169-bcb322fac167",
    "29dc388e-2259-448a-87d0-2ff00271df9d",
    "89845129-e95d-4bcf-a744-20bab2f6ee59",
    "49d6e584-2efa-4128-8c0b-2c5318cae937",
    "f69b425f-65c3-42e8-87b3-34e27325b9b2",
    "feca53c7-ab93-4763-b113-11d25f3415b3",
    "8c56d69c-50ba-4564-9a0e-a31ab0aed035",
    "c3434c3f-5269-4a18-abb9-416ff1f43fd4",
    "4222b936-87ff-4180-8baa-dd7b4dedc885",
    "e8dff8ba-980b-46a8-bc20-b45977d1fb8d"
]

all_signals_raw = []

for dep in deployments:
    print(f"Fetching logs for {dep}...")
    try:
        res = subprocess.run(
            [".\\railway.exe", "logs", dep, "--lines", "2500"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )
        lines = res.stdout.splitlines()
        print(f"  Got {len(lines)} lines")
        for line in lines:
            if "NEW SIGNAL:" in line or "Sent EMA" in line:
                all_signals_raw.append(f"[{dep}] {line}")
    except Exception as e:
        print(f"  Error on {dep}: {e}")

with open("raw_signals_since_yesterday.txt", "w", encoding="utf-8") as f:
    for s in all_signals_raw:
        f.write(s + "\n")

print(f"Total raw signal lines collected: {len(all_signals_raw)}")
