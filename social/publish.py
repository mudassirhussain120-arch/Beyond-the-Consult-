#!/usr/bin/env python3
"""Publish today's Beyond the Consult post to Instagram (carousel) and the Facebook Page (photo post).

Reads social/queue/<YYYY-MM-DD>-<slug>/ (post.json, caption.txt, images) and publishes the
folder whose date matches today (UTC). Standard library only. The token is read from the
environment (IG_ACCESS_TOKEN) and is never printed.

Instagram is recorded in posted.json and Facebook in fb_posted.json, so each platform is
published at most once and a failure on one never blocks or repeats the other. The Facebook step
needs a Page token with the pages_manage_posts permission; without it, it is skipped with a warning.

Usage:
  publish.py                 publish today's post
  publish.py --dry-run       check token, files and public image URLs; publish nothing
  publish.py --date YYYY-MM-DD
"""
import argparse, datetime, json, os, sys, time, urllib.error, urllib.parse, urllib.request

GRAPH = os.environ.get("GRAPH_BASE", "https://graph.facebook.com/v26.0").rstrip("/")
SITE = os.environ.get("SITE_BASE", "https://beyondtheconsultation.com").rstrip("/")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue")


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def graph(method, path, token, params=None, soft=False):
    params = dict(params or {})
    params["access_token"] = token
    data = urllib.parse.urlencode(params).encode()
    url = f"{GRAPH}/{path.lstrip('/')}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode())
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body).get("error", {})
            body = f"{err.get('type')}: {err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})"
        except Exception:
            pass
        if soft:
            return {"_error": f"HTTP {e.code}: {body}"}
        fail(f"Graph API {method} {path} -> HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        if soft:
            return {"_error": f"Network error: {e.reason}"}
        fail(f"Network error calling {method} {path}: {e.reason}")


def check_url(url, wait_seconds):
    """Wait until the image is publicly reachable (GitHub Pages can lag after a push)."""
    deadline = time.time() + wait_seconds
    last = None
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "btc-publisher"})
            with urllib.request.urlopen(req, timeout=30) as r:
                ctype = r.headers.get("Content-Type", "")
                if r.status == 200 and "image" in ctype:
                    return True
                last = f"HTTP {r.status}, type {ctype}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(e)
        if time.time() >= deadline:
            return last
        time.sleep(20)


def wait_container(cid, token, label):
    for _ in range(40):
        st = graph("GET", cid, token, {"fields": "status_code,status"})
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            fail(f"Container {label} failed: {st}")
        time.sleep(5)
    fail(f"Container {label} did not finish in time")


def publish_facebook(d, folder, caption, urls, token, dry_run):
    """Post the same images and caption to the Facebook Page. Never fails the run: problems become warnings."""
    done = os.path.join(d, "fb_posted.json")
    if os.path.exists(done):
        print(f"{folder} is already posted on Facebook. Nothing to do.")
        return
    want = os.environ.get("FB_PAGE_NAME", "Beyond the Consult").strip()
    page = graph("GET", "me", token, {"fields": "id,name"}, soft=True)
    if "_error" in page:
        print(f"::warning::Facebook skipped, could not read the Page with this token: {page['_error']}")
        return
    if page.get("name") != want:
        print(f"::warning::Facebook skipped: token is for '{page.get('name')}', not the Page '{want}'.")
        return
    pid = page["id"]
    print(f"Facebook Page OK: {page.get('name')}")
    if dry_run:
        print("Dry run: Facebook post not published.")
        return

    # Guard against double posting if a previous run published but could not record it.
    recent = graph("GET", f"{pid}/posts", token, {"fields": "message,created_time", "limit": "10"}, soft=True)
    head = caption[:80]
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=20)
    for m in recent.get("data", []):
        try:
            when = datetime.datetime.strptime(m.get("created_time", ""), "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            continue
        if when > cutoff and (m.get("message") or "").startswith(head):
            print("A matching Facebook post was already published in the last 20 hours. Recording it and stopping.")
            json.dump({"post_id": m.get("id"), "note": "found existing post"}, open(done, "w"), indent=2)
            return

    ids = []
    for i, u in enumerate(urls, 1):
        r = graph("POST", f"{pid}/photos", token, {"url": u, "published": "false"}, soft=True)
        if "_error" in r:
            print(f"::warning::Facebook skipped, photo {i} upload failed: {r['_error']}")
            return
        ids.append(r["id"])
    params = {"message": caption}
    for i, pid_ in enumerate(ids):
        params[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid_})
    r = graph("POST", f"{pid}/feed", token, params, soft=True)
    if "_error" in r:
        print(f"::warning::Facebook post failed: {r['_error']}")
        return
    json.dump({"post_id": r.get("id"), "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
              open(done, "w"), indent=2)
    print(f"Published {folder} on Facebook: {r.get('id')}")


def publish_instagram(d, folder, user, token, caption, urls, dry_run, url_wait):
    me = graph("GET", user, token, {"fields": "username"})
    print(f"Token OK for Instagram account @{me.get('username')}")
    if me.get("username") != "beyondtheconsult":
        fail("Token belongs to a different Instagram account than @beyondtheconsult")

    for u in urls:
        r = check_url(u, 0 if dry_run else url_wait)
        if r is not True:
            if dry_run:
                print(f"  NOT reachable yet: {u} ({r})")
            else:
                fail(f"Image not publicly reachable: {u} ({r})")
        else:
            print(f"  reachable: {u}")

    if dry_run:
        print(f"Dry run complete for {folder}. Caption length {len(caption)}. Nothing published.")
        return

    # Guard against double posting if a previous run published but could not record it.
    recent = graph("GET", f"{user}/media", token, {"fields": "caption,timestamp", "limit": "10"}).get("data", [])
    head = caption[:80]
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=20)
    for m in recent:
        ts = m.get("timestamp", "")
        try:
            when = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            continue
        if when > cutoff and (m.get("caption") or "").startswith(head):
            print("A matching post was already published in the last 20 hours. Recording it and stopping.")
            json.dump({"media_id": m.get("id"), "note": "found existing post"}, open(os.path.join(d, "posted.json"), "w"), indent=2)
            return

    children = []
    for i, u in enumerate(urls, 1):
        c = graph("POST", f"{user}/media", token, {"image_url": u, "is_carousel_item": "true"})["id"]
        wait_container(c, token, f"image {i}")
        children.append(c)
    car = graph("POST", f"{user}/media", token,
                {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})["id"]
    wait_container(car, token, "carousel")
    pub = graph("POST", f"{user}/media_publish", token, {"creation_id": car})["id"]
    info = graph("GET", pub, token, {"fields": "permalink"})
    json.dump({"media_id": pub, "permalink": info.get("permalink"),
               "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
              open(os.path.join(d, "posted.json"), "w"), indent=2)
    print(f"Published {folder}: {info.get('permalink')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--url-wait", type=int, default=600, help="seconds to wait for image URLs")
    a = ap.parse_args()

    date = a.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    folders = sorted(d for d in os.listdir(ROOT) if d.startswith(date + "-")) if os.path.isdir(ROOT) else []
    if not folders:
        print(f"No post scheduled for {date}. Nothing to do.")
        return
    if len(folders) > 1:
        fail(f"More than one folder for {date}: {folders}")
    folder = folders[0]
    d = os.path.join(ROOT, folder)

    ig_done = os.path.exists(os.path.join(d, "posted.json"))

    meta = json.load(open(os.path.join(d, "post.json"), encoding="utf8"))
    caption = open(os.path.join(d, "caption.txt"), encoding="utf8").read().strip()
    images = meta["images"]
    if not 2 <= len(images) <= 10:
        fail("A carousel needs 2 to 10 images")
    if len(caption) > 2200:
        fail(f"Caption is {len(caption)} characters; Instagram allows 2200")
    for im in images:
        if not os.path.exists(os.path.join(d, im)):
            fail(f"Missing image file {im}")

    user = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not user or not token:
        fail("IG_USER_ID and IG_ACCESS_TOKEN must be set as repository secrets")

    urls = [f"{SITE}/social/queue/{folder}/{im}" for im in images]
    if ig_done:
        print(f"{folder} is already marked as posted on Instagram.")
    else:
        publish_instagram(d, folder, user, token, caption, urls, a.dry_run, a.url_wait)
    publish_facebook(d, folder, caption, urls, token, a.dry_run)


if __name__ == "__main__":
    main()
