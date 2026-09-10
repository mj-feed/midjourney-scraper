"""Find the MINIMUM header set needed to access cdn.midjourney.com.
Test combinations systematically."""
import subprocess
import itertools

URL = "https://cdn.midjourney.com/b872f448-6018-432b-861c-792bd7ca5317/0_0_384_N.webp"

def curl(url, headers_list):
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15"]
    for h in headers_list:
        cmd.extend(["-H", h])
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.stdout.strip()
    except:
        return "ERR"

# Full browser header set (known to work from previous test)
FULL = {
    "UA": "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Ref": "Referer: https://www.midjourney.com/explore",
    "Acc": "Accept: image/webp,image/apng,image/*,*/*;q=0.8",
    "AL": "Accept-Language: en-US,en;q=0.9",
    "SD": "Sec-Fetch-Dest: image",
    "SM": "Sec-Fetch-Mode: no-cors",
    "SS": "Sec-Fetch-Site: cross-site",
}

print("Testing individual headers:")
for name, val in FULL.items():
    s = curl(URL, [val])
    print(f"  {name:4s} only: [{s}]")

print("\nTesting pairs (UA + one other):")
for name, val in FULL.items():
    if name == "UA": continue
    s = curl(URL, [FULL["UA"], val])
    print(f"  UA+{name:4s}: [{s}]")

print("\nTesting UA + Referer variations:")
ref_variations = [
    ("Ref=mj/explore", "Referer: https://www.midjourney.com/explore"),
    ("Ref=mj/", "Referer: https://www.midjourney.com/"),
    ("Ref=mj", "Referer: https://www.midjourney.com"),
    ("Ref=example", "Referer: https://example.com/"),
    ("Ref=google", "Referer: https://www.google.com/"),
    ("Ref=empty", "Referer: "),
    ("Ref=none", None),
]
for label, ref in ref_variations:
    headers = [FULL["UA"]]
    if ref is not None:
        headers.append(ref)
    s = curl(URL, headers)
    print(f"  UA + {label:15s}: [{s}]")

print("\nTesting UA + Referer + Sec-Fetch-* (the magic combo):")
combos = [
    ("UA+Ref", [FULL["UA"], FULL["Ref"]]),
    ("UA+Ref+SD", [FULL["UA"], FULL["Ref"], FULL["SD"]]),
    ("UA+Ref+SD+SM", [FULL["UA"], FULL["Ref"], FULL["SD"], FULL["SM"]]),
    ("UA+Ref+SD+SM+SS", [FULL["UA"], FULL["Ref"], FULL["SD"], FULL["SM"], FULL["SS"]]),
    ("UA+Ref+SD+SM+SS+Acc", [FULL["UA"], FULL["Ref"], FULL["SD"], FULL["SM"], FULL["SS"], FULL["Acc"]]),
    ("UA+Ref+SD+SM+SS+Acc+AL", [FULL["UA"], FULL["Ref"], FULL["SD"], FULL["SM"], FULL["SS"], FULL["Acc"], FULL["AL"]]),
    ("ALL", list(FULL.values())),
]
for label, headers in combos:
    s = curl(URL, headers)
    print(f"  {label:25s}: [{s}]")

print("\nTesting without Referer but with Sec-Fetch-*:")
combos2 = [
    ("UA only", [FULL["UA"]]),
    ("UA+SD", [FULL["UA"], FULL["SD"]]),
    ("UA+SD+SM+SS", [FULL["UA"], FULL["SD"], FULL["SM"], FULL["SS"]]),
    ("UA+SD+SM+SS+Acc+AL", [FULL["UA"], FULL["SD"], FULL["SM"], FULL["SS"], FULL["Acc"], FULL["AL"]]),
]
for label, headers in combos2:
    s = curl(URL, headers)
    print(f"  {label:25s}: [{s}]")

print("\nTesting Sec-Fetch-Site variations (with UA + Ref + SD + SM):")
sf_variations = [
    ("cross-site", "Sec-Fetch-Site: cross-site"),
    ("same-site", "Sec-Fetch-Site: same-site"),
    ("same-origin", "Sec-Fetch-Site: same-origin"),
    ("none", "Sec-Fetch-Site: none"),
]
for label, val in sf_variations:
    s = curl(URL, [FULL["UA"], FULL["Ref"], FULL["SD"], FULL["SM"], val])
    print(f"  SS={label:12s}: [{s}]")
