import os
import time
import hashlib
import requests
from playwright.sync_api import sync_playwright

# ─── CONFIGURATION ──────────────────────────────────────────────────────────────
TARGET_URL = "Paste the Facebook post URL here"
DOWNLOAD_FOLDER = "downloaded_images1"
MAX_IMAGES = 200          # Safety limit — stop after this many images
NAVIGATE_DELAY = 2.0      # Seconds to wait after pressing Right arrow
IMAGE_LOAD_TIMEOUT = 8    # Seconds to wait for the main image to load
STALE_RETRIES = 3         # How many consecutive unchanged-image checks before stopping
# ────────────────────────────────────────────────────────────────────────────────


def download_image(url, filepath, cookies=None):
    """Download an image from a URL, using session cookies for auth."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.facebook.com/",
        }
        response = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        if response.status_code == 200 and len(response.content) > 1000:
            with open(filepath, "wb") as f:
                f.write(response.content)
            size_kb = len(response.content) / 1024
            print(f"  ✓ Saved: {os.path.basename(filepath)} ({size_kb:.1f} KB)")
            return True
        else:
            print(f"  ✗ Bad response: status={response.status_code}, size={len(response.content)}")
            return False
    except Exception as e:
        print(f"  ✗ Download error: {e}")
        return False


def get_main_image_src(page):
    """
    Extract the source URL of the main/largest visible image in FB's photo viewer.
    Facebook renders the main photo inside a spotlight overlay with large <img> tags.
    We pick the image with the largest natural dimensions.
    """
    try:
        # Wait a moment for any lazy-loaded image to appear
        page.wait_for_timeout(500)

        # Strategy 1: Look for the image inside the photo viewer overlay
        # Facebook uses role="img" or data-visualcompletion for the main photo
        src = page.evaluate("""() => {
            // Gather all visible <img> elements
            const imgs = Array.from(document.querySelectorAll('img'));
            let best = null;
            let bestArea = 0;

            for (const img of imgs) {
                const src = img.src || '';
                // Skip tiny icons, emojis, profile pics, etc.
                if (!src.startsWith('http')) continue;
                if (src.includes('emoji') || src.includes('rsrc.php')) continue;

                // Use naturalWidth/Height to find the actual photo (largest image)
                const area = (img.naturalWidth || 0) * (img.naturalHeight || 0);
                if (area > bestArea) {
                    bestArea = area;
                    best = src;
                }
            }
            return best;
        }""")
        return src
    except Exception as e:
        print(f"  Warning: Could not extract image src: {e}")
        return None


def content_hash(url, cookies=None):
    """Download first few KB and hash it to detect duplicate images with different URLs."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.facebook.com/",
        }
        resp = requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=10)
        chunk = resp.raw.read(8192)
        resp.close()
        return hashlib.md5(chunk).hexdigest()
    except Exception:
        return None


def get_browser_cookies(context):
    """Extract cookies from the Playwright browser context for use with requests."""
    pw_cookies = context.cookies()
    jar = {}
    for c in pw_cookies:
        jar[c["name"]] = c["value"]
    return jar


# ─── MAIN ────────────────────────────────────────────────────────────────────────
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()

    print(f"Navigating to: {TARGET_URL}")
    page.goto(TARGET_URL, wait_until="domcontentloaded")

    print("\n" + "=" * 60)
    print("INSTRUCTIONS:")
    print("  1. Log in to Facebook if prompted.")
    print("  2. Make sure the FIRST image of the post is open")
    print("     in Facebook's photo viewer (the lightbox overlay).")
    print("  3. Press ENTER in this terminal to start downloading.")
    print("=" * 60)
    input("\n>>> Press ENTER when ready... ")

    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

    # Grab auth cookies so requests can fetch high-res images
    cookies = get_browser_cookies(context)

    downloaded_hashes = set()   # Content-based dedup
    downloaded_urls = set()     # URL-based dedup
    image_index = 0
    stale_count = 0
    last_hash = None

    print(f"\nStarting download (max {MAX_IMAGES} images)...\n")

    while image_index < MAX_IMAGES:
        # 1. Extract the main image URL
        current_src = get_main_image_src(page)

        if not current_src:
            print("Could not find an image. Retrying once...")
            time.sleep(2)
            current_src = get_main_image_src(page)
            if not current_src:
                print("Still no image found — stopping.")
                break

        # 2. Content-hash based dedup (handles FB's changing CDN URLs)
        c_hash = content_hash(current_src, cookies)

        if c_hash and c_hash == last_hash:
            stale_count += 1
            if stale_count >= STALE_RETRIES:
                print(f"\nImage unchanged for {STALE_RETRIES} consecutive checks — reached end of carousel.")
                break
        else:
            stale_count = 0

        last_hash = c_hash

        # 3. Download if it's a new image
        if current_src not in downloaded_urls and (c_hash is None or c_hash not in downloaded_hashes):
            image_index += 1
            ext = "jpg"
            if ".png" in current_src.lower():
                ext = "png"
            elif ".webp" in current_src.lower():
                ext = "webp"

            filename = os.path.join(DOWNLOAD_FOLDER, f"image_{image_index:03d}.{ext}")
            print(f"[{image_index}] Downloading...")

            success = download_image(current_src, filename, cookies)
            if success:
                downloaded_urls.add(current_src)
                if c_hash:
                    downloaded_hashes.add(c_hash)
            else:
                image_index -= 1  # roll back counter on failure
        else:
            # Already downloaded this one (URL or content match)
            pass

        # 4. Press Right arrow to go to the next image
        page.keyboard.press("ArrowRight")
        time.sleep(NAVIGATE_DELAY)

    # ─── SUMMARY ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"DONE! Downloaded {image_index} images to '{DOWNLOAD_FOLDER}/'")
    print("=" * 60)

    browser.close()

