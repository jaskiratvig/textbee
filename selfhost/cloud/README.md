# Cloud hosting

Runs the server, the drip schedule and backups on an always-on Ubuntu server, so your computer can be off. The Android phone still sends the texts, so it must stay on, charged, and connected to Tailscale.

## Set up

1. Create the server and move your data with steps 1 to 6 of "Move to an always-on server" in `selfhost/README.md`, using this `cloud` branch instead of `self-host`. Skip step 7's cron line. The timers below replace it.
2. On your old computer, remove the drip line with `crontab -e` and stop Docker with `docker compose down`, so only one server is running.
3. On the server, from the `textbee` folder, install the timers:

   ```bash
   ./selfhost/cloud/install.sh YOUR_DEVICE_ID
   ```

   It asks for your API key and stores it in `/etc/textbee-drip.env`, readable only by root.

Sending starts **paused**. Replies sync every 15 minutes, and data is backed up daily at 2:30am to `~/textbee-backups`.

## Before you resume

Scrub the database against your original skip-trace export, the one with DNC columns:

```bash
python3 selfhost/drip.py scrub --csv ~/list.csv
```

Confirm a test text sends and arrives, and that a test reply shows up with `python3 selfhost/drip.py replies`. Then:

```bash
python3 selfhost/drip.py resume
```

Sends then run every 30 minutes from 9:00am to 7:30pm Chicago time, at up to 20 per run, 200 a day and 100 new contacts a day. Change those in `DRIP_ARGS` in `/etc/textbee-drip.env`.

## Day to day

```bash
python3 selfhost/drip.py status
python3 selfhost/drip.py replies
python3 selfhost/drip.py pause          # stop sending right away
journalctl -u textbee-drip -n 50        # recent send runs
systemctl list-timers 'textbee-*'       # next scheduled runs
```

The containers restart on their own after a reboot. Check `journalctl -u textbee-drip` if sends stop, for example after a "Paused: the phone last checked in" message.
