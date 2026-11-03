#!/usr/bin/env python3
"""Write the .env files for a self-hosted install. Existing files are never overwritten."""
import argparse, json, secrets, sys, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def set_vars(text, values):
    out, seen = [], set()
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f"{key}={values[key]}"); seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    return "\n".join(out) + "\n"

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--firebase-json", required=True, help="Firebase service account key file")
    ap.add_argument("--public-url", required=True, help="e.g. https://my-pc.tailnet-name.ts.net")
    a = ap.parse_args()
    url = a.public_url.rstrip("/")
    if not url.startswith("https://"):
        sys.exit("--public-url must start with https:// (the Android app needs HTTPS)")
    targets = [ROOT / ".env", ROOT / "api/.env", ROOT / "web/.env"]
    existing = [str(t) for t in targets if t.exists()]
    if existing:
        sys.exit("Refusing to overwrite: " + ", ".join(existing))
    fb = json.loads(Path(a.firebase_json).read_text())
    user, pw = "textbee", secrets.token_urlsafe(24)
    key = fb["private_key"].replace("\n", "\\n")
    targets[0].write_text(f"MONGO_ROOT_USER={user}\nMONGO_ROOT_PASS={pw}\nNEXT_PUBLIC_API_BASE_URL={url}/api/v1\n")
    targets[1].write_text(set_vars((ROOT / "api/.env.example").read_text(), {
        "MONGO_URI": f"mongodb://{user}:{urllib.parse.quote(pw)}@textbee-db:27017/textbee?authSource=admin",
        "MONGO_ROOT_USER": user, "MONGO_ROOT_PASS": pw,
        "JWT_SECRET": secrets.token_urlsafe(48), "EMAIL_LINK_SECRET": secrets.token_urlsafe(48),
        "FRONTEND_URL": url, "API_PUBLIC_URL": url, "APP_PUBLIC_URL": url,
        "FIREBASE_PROJECT_ID": fb["project_id"], "FIREBASE_PRIVATE_KEY_ID": fb["private_key_id"],
        "FIREBASE_PRIVATE_KEY": f'"{key}"', "FIREBASE_CLIENT_EMAIL": fb["client_email"],
        "FIREBASE_CLIENT_ID": fb["client_id"], "FIREBASE_CLIENT_C509_CERT_URL": fb["client_x509_cert_url"],
    }))
    targets[2].write_text(set_vars((ROOT / "web/.env.example").read_text(), {
        "NEXT_PUBLIC_SITE_URL": url, "NEXT_PUBLIC_API_BASE_URL": f"{url}/api/v1",
        "NEXTAUTH_SECRET": secrets.token_urlsafe(48), "NEXTAUTH_URL": url,
    }))
    print("Wrote .env, api/.env and web/.env")

if __name__ == "__main__":
    main()
