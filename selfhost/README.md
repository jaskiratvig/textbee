# Self-hosted setup

Runs the gateway on a computer that stays on. The phone reaches it privately over Tailscale, with no public ports.

## You need

- Docker
- Android Studio, or the Android SDK
- A free [Firebase](https://console.firebase.google.com) project
- A free [Tailscale](https://tailscale.com) account, with the app on both the computer and the phone
- An Android phone with a SIM that can text

## 1. Firebase

1. Create a project.
2. Add an Android app with the package name `com.vigestates.sms`.
3. Save its `google-services.json` to `android/app/src/selfhost/google-services.json`.
4. Under Project settings > Service accounts, generate a private key and save it as `selfhost/firebase-service-account.json`.

Both paths are ignored by git. Don't commit either file.

## 2. Server

Find your computer's Tailscale address with `tailscale status`, and enable HTTPS certificates in the Tailscale admin console. Then:

```bash
python3 selfhost/setup.py \
  --firebase-json selfhost/firebase-service-account.json \
  --public-url https://YOUR-COMPUTER.YOUR-TAILNET.ts.net

docker compose up -d --build

tailscale serve --bg 3000
tailscale serve --bg --set-path /api http://127.0.0.1:3001/api
```

Open your `https://YOUR-COMPUTER.YOUR-TAILNET.ts.net` address, register an account, then run:

```bash
./selfhost/bootstrap.sh you@example.com
```

Next, create an API key in the dashboard.

## 3. Android app

```bash
cd android
./gradlew assembleSelfhostRelease \
  -PSELFHOST_API_BASE_URL=https://YOUR-COMPUTER.YOUR-TAILNET.ts.net/api/v1/
```

The release build is signed with `~/.android/debug.keystore`, which Android Studio creates the first time it builds an app.

Install the APK from `android/app/build/outputs/apk/selfhost/release/` on the phone and enter your API key. Then:

- Allow the SMS permissions. Android 13 and later may ask you to turn on "Allow restricted settings" for the app first.
- Exempt the app from battery optimization.
- Turn on receiving SMS, which opt-out handling depends on.
- Leave message encryption off, because the script can't read encrypted replies.

## 4. Send

Make a CSV with at least these columns, where `opt_in` records whether that person agreed to texts:

```csv
phone,opt_in,first_name
3125550100,yes,Sam
```

Do a dry run first:

```bash
python3 selfhost/send_batches.py --csv leads.csv \
  --template "Hi {first_name}, this is Jas with Vig Estates. Reply STOP to opt out."
```

When it looks right, send:

```bash
export TEXTBEE_API_KEY=your-key
python3 selfhost/send_batches.py --csv leads.csv \
  --template "Hi {first_name}, this is Jas with Vig Estates. Reply STOP to opt out." \
  --device-id YOUR_DEVICE_ID --live
```

It defaults to 20 texts every 30 minutes, between 9am and 8pm Central. Put your DNC scrub results and any other numbers to skip in `selfhost/data/suppressed.txt`, one per line. Opt-out replies are added there automatically.

A successful run means the batch was queued. Check the dashboard to confirm delivery.
