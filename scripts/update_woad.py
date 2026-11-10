#!/usr/bin/env python3
"""Find the current WoAD download on the developer's site and write woad.html,
a one-link page Obtainium can track. The APK itself stays on the developer's
Google Drive; this only adds confirm=t so Drive skips its virus-scan page."""
import html, re, sys, urllib.request

SRC = "https://woad.sumusltd.com/download/"
OUT = "woad.html"

req = urllib.request.Request(SRC, headers={"User-Agent": "Mozilla/5.0 (73_Android link checker)"})
page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")

m = re.search(r"drive\.google\.com/uc\?export=download&(?:amp;)?id=([\w-]+)", page)
text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
v = re.search(r"latest version of WoAD \[(\d+(?:\.\d+)+)\]", text)
if not m or not v:
    sys.exit("Could not find the WoAD download link or version; page layout may have changed.")
file_id, version = m.group(1), v.group(1)

link = (f"https://drive.usercontent.google.com/download?id={file_id}"
        f"&export=download&confirm=t&v={version}")
out = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WoAD download link</title><meta name="robots" content="noindex"></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 16px">
<p>Tracking link for Obtainium. WoAD is by Sumus Technology Limited; get it from
<a href="{SRC}">the developer's download page</a>.</p>
<p><a id="apk" href="{html.escape(link)}">WoAD {version} APK</a></p>
</body></html>
"""
try:
    old = open(OUT).read()
except FileNotFoundError:
    old = ""
if old != out:
    open(OUT, "w").write(out)
    print(f"Updated: WoAD {version} ({file_id})")
else:
    print(f"No change: WoAD {version}")
