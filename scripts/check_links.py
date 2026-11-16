#!/usr/bin/env python3
"""Daily health check for 73 Android.

Level 1: every link on the site pages loads (and local files exist).
Level 2: for every app in radio-apps.json, Obtainium can still find an APK
         (GitHub release asset, F-Droid listing, or the developer's download page).

APKPure and Uptodown block automated checks, so their pages only count as broken
after failing with "not found" two runs in a row (state kept in STATE_FILE).

With --issue (in GitHub Actions), opens / updates / closes one GitHub issue.
Standard library only.
"""
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ["index.html", "about.html", "README.md"]
SITE = "https://doodlecomms.github.io/73_Android/"
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"
STORE_HOSTS = ("apkpure.com", "apkpure.net", "uptodown.com")
BLOCKED_CODES = {401, 403, 429, 503}  # bot walls: "can't tell", not "broken"
ISSUE_TITLE = "Link check: something needs attention"
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(ROOT, ".linkcheck-state.json"))
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

problems = []   # (where, what) shown in the issue
soft_now = []   # store pages that returned "not found" this run
notes = []      # informational, run log only


def is_store(url):
    h = urllib.parse.urlparse(url).hostname or ""
    return any(h == s or h.endswith("." + s) for s in STORE_HOSTS)


def fetch(url, headers=None, body=True, tries=2):
    """Return (status, final_url, text or None, content_type). status 0 = network error."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = (0, url, None, "")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as r:
                ctype = r.headers.get("Content-Type", "")
                text = None
                if body:
                    text = r.read(3_000_000).decode("utf-8", "replace")
                else:
                    r.read(1024)
                return r.status, r.geturl(), text, ctype
        except urllib.error.HTTPError as e:
            last = (e.code, url, None, "")
            if e.code in (404, 410) or e.code in BLOCKED_CODES:
                return last
        except Exception as e:  # DNS, timeout, TLS...
            last = (0, url, None, str(e)[:120])
        if attempt + 1 < tries:
            time.sleep(5)
    return last


def gh_api(path):
    hdrs = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        hdrs["Authorization"] = f"Bearer {GH_TOKEN}"
    st, _, text, _ = fetch("https://api.github.com" + path, hdrs)
    return st, (json.loads(text) if st == 200 and text else None)


# ---------- Level 1: links on the site ----------

def page_links():
    urls, local = set(), set()
    for p in PAGES:
        src = open(os.path.join(ROOT, p), encoding="utf-8").read()
        for u in re.findall(r"https?://[^\s\"'<>`)\]]+", src):
            u = html.unescape(u).rstrip(".,;")
            if "${" in u or u.startswith(SITE):
                continue
            urls.add(u)
        for u in re.findall(r"(?:href|src)=\"([^\"]+)\"", src):
            u = html.unescape(u)
            if re.match(r"(https?:|mailto:|obtainium:|#|\$\{|data:)", u) or "${" in u:
                continue
            local.add(u.split("#")[0].split("?")[0])
    return sorted(urls), sorted(x for x in local if x)


def check_page_link(url):
    st, _, _, err = fetch(url, body=False)
    if is_store(url):
        if st in (404, 410):
            soft_now.append(url)
        elif st != 200:
            notes.append(f"store page not checkable ({st or err}): {url}")
        return
    if st in BLOCKED_CODES:
        notes.append(f"blocked ({st}), not counted: {url}")
    elif st != 200:
        problems.append(("Site link", f"{url} → {st or 'no response: ' + err}"))


def level1():
    urls, local = page_links()
    for f in local:
        if not os.path.exists(os.path.join(ROOT, f)):
            problems.append(("Site file", f"`{f}` is linked but missing from the repo"))
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(check_page_link, urls))
    return len(urls) + len(local)


# ---------- Level 2: can Obtainium still find an APK? ----------

def apk_downloads(url, headers=None):
    st, _, _, ctype = fetch(url, headers, body=False)
    if st not in (200, 206):
        return f"APK link → {st or 'no response'}"
    if "text/html" in ctype:
        return "APK link returns a web page instead of a file"
    return None


def check_github(app, s):
    m = re.match(r"https://github\.com/([^/]+)/([^/#?]+)", app["url"])
    owner, repo = m.group(1), m.group(2)
    if s.get("includePrereleases"):
        st, rels = gh_api(f"/repos/{owner}/{repo}/releases?per_page=10")
        rel = next((r for r in (rels or []) if not r.get("draft")), None)
    else:
        st, rel = gh_api(f"/repos/{owner}/{repo}/releases/latest")
    if st == 404 or (st == 200 and not rel):
        return "no stable GitHub release found (repo moved, deleted, or releases removed)"
    if st != 200:
        notes.append(f"GitHub API {st} for {owner}/{repo}, skipped")
        return None
    names = [a["name"] for a in rel.get("assets", []) if a["name"].lower().endswith(".apk")]
    flt = s.get("apkFilterRegEx") or ""
    if flt:
        inv = s.get("invertAPKFilter")
        names = [n for n in names if bool(re.search(flt, n)) != bool(inv)]
    if not names:
        return f"latest release {rel.get('tag_name')} has no APK matching the list's filter"
    return None


def check_fdroid(app):
    pkg = app["url"].rstrip("/").split("/")[-1]
    st, _, text, _ = fetch(f"https://f-droid.org/api/v1/packages/{pkg}")
    if st == 404:
        return "no longer listed on F-Droid"
    if st != 200:
        return f"F-Droid API → {st or 'no response'}"
    if not json.loads(text).get("packages"):
        return "F-Droid lists no versions"
    return None


def links_on(url, text, pattern):
    hrefs = [urllib.parse.urljoin(url, html.unescape(h)) for h in re.findall(r"href=[\"']([^\"']+)[\"']", text, re.I)]
    return [h for h in hrefs if re.search(pattern, h)]


def vkey(s):
    return [int(x) for x in re.findall(r"\d+", s)]


def check_html(app, s):
    hdrs = {}
    for h in s.get("requestHeader") or []:
        k, _, v = h.get("requestHeader", "").partition(":")
        if k.strip():
            hdrs[k.strip()] = v.strip()
    url = app["url"]
    st, url, text, _ = fetch(url, hdrs)
    if st != 200 or not text:
        return f"download page {app['url']} → {st or 'no response'}"
    for step in s.get("intermediateLink") or []:
        found = links_on(url, text, step.get("customLinkFilterRegex") or ".")
        if not found:
            return "download page layout changed (can't find the version folder)"
        nxt = sorted(set(found), key=vkey)[-1]
        st, url, text, _ = fetch(nxt, hdrs)
        if st != 200 or not text:
            return f"{nxt} → {st or 'no response'}"
    pat = s.get("customLinkFilterRegex") or r"\.apk($|\?)"
    found = links_on(url, text, pat)
    if not found:
        return "download page layout changed (no APK link found)"
    apk = sorted(set(found), key=vkey)[-1]
    ver = s.get("versionExtractionRegEx")
    if ver and not re.search(ver, apk):
        return "APK link found, but the version can't be read from it anymore"
    return apk_downloads(apk, hdrs)


def check_app(app):
    s = json.loads(app.get("additionalSettings") or "{}")
    host = urllib.parse.urlparse(app["url"]).hostname or ""
    try:
        if is_store(app["url"]):
            st, _, _, err = fetch(app["url"], body=False)
            if st in (404, 410):
                soft_now.append(app["url"])
            elif st != 200:
                notes.append(f"{app['name']}: store page not checkable ({st or err})")
            return
        if host == "github.com":
            err = check_github(app, s)
        elif host == "f-droid.org":
            err = check_fdroid(app)
        else:
            err = check_html(app, s)
    except Exception as e:
        err = f"checker error: {e}"
    if err:
        problems.append((app["name"], err))


def level2():
    apps = json.load(open(os.path.join(ROOT, "radio-apps.json")))["apps"]
    with cf.ThreadPoolExecutor(6) as ex:
        list(ex.map(check_app, apps))
    return len(apps)


# ---------- store-page leniency ----------

def apply_soft():
    try:
        prev = set(json.load(open(STATE_FILE)).get("soft", []))
    except Exception:
        prev = set()
    for u in sorted(set(soft_now)):
        if u in prev:
            problems.append(("Store page", f"{u} → not found two runs in a row"))
        else:
            notes.append(f"store page not found once, will confirm next run: {u}")
    with open(STATE_FILE, "w") as f:
        json.dump({"soft": sorted(set(soft_now))}, f)


# ---------- GitHub issue ----------

def gh(method, path, data=None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {GH_TOKEN}",
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "73Android-link-check"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def report_body():
    lines = ["The daily link check found problems. Obtainium installs or the site may be affected.", "",
             "| Where | Problem |", "|---|---|"]
    lines += [f"| {w} | {p.replace('|', '/')} |" for w, p in sorted(problems)]
    run = os.environ.get("GITHUB_RUN_ID")
    if run:
        lines += ["", f"[See the run log](https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run})"]
    lines += ["", "This issue updates itself each day and closes when everything passes again."]
    return "\n".join(lines)


def sync_issue():
    repo = os.environ["GITHUB_REPOSITORY"]
    issues = gh("GET", f"/repos/{repo}/issues?state=open&per_page=100")
    issue = next((i for i in issues if i["title"] == ISSUE_TITLE and "pull_request" not in i), None)
    if problems:
        body = report_body()
        if not issue:
            gh("POST", f"/repos/{repo}/issues", {"title": ISSUE_TITLE, "body": body})
            print("Opened issue")
        elif issue["body"].split("[See the run log]")[0] != body.split("[See the run log]")[0]:
            gh("PATCH", f"/repos/{repo}/issues/{issue['number']}", {"body": body})
            gh("POST", f"/repos/{repo}/issues/{issue['number']}/comments",
               {"body": "The list of problems changed. See the updated description above."})
            print("Updated issue")
        else:
            gh("PATCH", f"/repos/{repo}/issues/{issue['number']}", {"body": body})
            print("Issue unchanged (refreshed run link)")
    elif issue:
        gh("POST", f"/repos/{repo}/issues/{issue['number']}/comments",
           {"body": "Everything passed today. Closing."})
        gh("PATCH", f"/repos/{repo}/issues/{issue['number']}", {"state": "closed"})
        print("Closed issue")


def main():
    extra = os.environ.get("SIMULATE_BROKEN_LINK")
    if extra:
        st, _, _, _ = fetch(extra, body=False)
        if st != 200:
            problems.append(("Test (simulated)", f"{extra} → {st or 'no response'}"))
    n1 = level1()
    n2 = level2()
    apply_soft()
    print(f"Checked {n1} site links/files and {n2} apps.")
    for n in sorted(notes):
        print("  note:", n)
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for w, p in sorted(problems):
            print(f"  - {w}: {p}")
    else:
        print("All good.")
    if "--issue" in sys.argv:
        sync_issue()


if __name__ == "__main__":
    main()
