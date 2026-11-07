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
tailscale serve --bg --set-path /api/v1 http://127.0.0.1:3001/api/v1
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

Make a CSV with a `phone` column. Any other columns can be used in the template:

```csv
phone,first_name
3125550100,Sam
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

## 5. Drip campaign

Messages and timing live in `selfhost/drip_messages.json`: days 0, 5, 21 and 60. Any reply stops automated texts to that number. Opt-outs, declines and wrong numbers are suppressed permanently.

Before going live, confirm a STOP from your own phone shows up on your server. The drip can't detect a reply that never reaches it.

Set the phone app's Send Delay to 60 seconds, then import your list. The import accepts a cleaned CSV with `phone`, `FirstName` and `PropertyAddress` columns, or a raw skip-trace export:

```bash
python3 selfhost/drip.py import --csv ../list.csv
python3 selfhost/drip.py status
```

A raw export keeps one mobile number per property. It skips DNC numbers, vacant land and blank litigator fields. Add `--litigator-scrubbed` only after screening the list yourself. Business and trust owners are included with a no-name greeting. Add `--limit 100` to cap how many new records get added. Add `--all-numbers` to text every usable mobile number whose contact name matches the owner. A reply from one number only stops that number, so the property's other numbers keep their schedule.

Preview, then send:

```bash
python3 selfhost/drip.py run
export TEXTBEE_API_KEY=your-key
python3 selfhost/drip.py run --device-id YOUR_DEVICE_ID --live
```

Each live run sends up to 20 texts, within limits of 25 a day and 15 new contacts a day. It pauses if the phone hasn't checked in for 90 minutes, or if 20% or more of the last 50 sends failed, once at least 10 results are in. To schedule it hourly during sending hours, run `crontab -e` and add this line, using your own paths:

```cron
0 9-19 * * * cd /Users/you/sms-gateway/textbee && TEXTBEE_API_KEY=your-key /usr/bin/python3 selfhost/drip.py run --device-id YOUR_DEVICE_ID --live >> selfhost/data/drip.log 2>&1
```

Check the campaign with:

```bash
python3 selfhost/drip.py replies   # people to follow up with
python3 selfhost/drip.py status    # counts by status
python3 selfhost/drip.py retry     # requeue failed contacts, after confirming they weren't texted
python3 selfhost/drip.py history   # export all messages to selfhost/data/history.csv
```

Run the tests with `python3 selfhost/test_drip.py`.

## 6. Move to an always-on server

The phone stays the sender. The server runs the API, database and schedule, so your computer can be off.

Create an Ubuntu server with at least 2 GB of memory, plus swap. Then:

1. On the server, clone this branch and run `./selfhost/migrate_server_setup.sh`, then log out and back in.
2. Stop every send on the old machine, and remove its drip line from `crontab -e`.
3. On the old machine, run `./selfhost/migrate_export.sh`, then copy `~/textbee-migration.tar.gz` to the server with `scp`.
4. Rename the old machine in the Tailscale admin console. Then on the server run `sudo tailscale up --hostname=OLD-MACHINE-NAME` so the address stays the same and the phone app doesn't need a rebuild.
5. Enable Serve on the server with `sudo tailscale serve --bg 3000` and `sudo tailscale serve --bg --set-path /api/v1 http://127.0.0.1:3001/api/v1`.
6. On the server, run `./selfhost/migrate_import.sh ~/textbee-migration.tar.gz`, then delete the archive.
7. Check `selfhost/drip.py status` and a phone heartbeat. On the `cloud` branch, follow `selfhost/cloud/README.md` for the schedule instead of cron.

