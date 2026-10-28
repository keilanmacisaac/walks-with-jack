# Walks with Jack

A small booking website for free 5-minute walks with Jack Vanderveen.

## Included

- Public booking page
- 5-minute slots from 8:00 AM to 2:15 PM
- Last possible start time is 2:10 PM
- Maximum 2 active bookings per email per day
- One person per time slot
- Jack-only password-protected dashboard
- Dashboard statistics and next-walk display
- Upcoming booking list with cancellation
- Private, auto-updating `.ics` calendar subscription feed
- SQLite database
- Responsive phone/desktop design

## Run locally

1. Install Python 3.11+.
2. Open a terminal in this folder.
3. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   On Windows use `.venv\\Scripts\\activate`.

4. Install packages:

   ```bash
   pip install -r requirements.txt
   ```

5. Copy `.env.example` to `.env` and change all three secrets:

   ```bash
   cp .env.example .env
   ```

6. Run:

   ```bash
   python app.py
   ```

7. Open `http://localhost:5000`.

Jack's dashboard is at `http://localhost:5000/admin`.

## Calendar subscription

On Jack's dashboard, copy the private calendar subscription URL.

- **Apple Calendar:** File → New Calendar Subscription, then paste the URL.
- **Google Calendar:** Other calendars → + → From URL, then paste the URL.
- **Outlook:** Add calendar → Subscribe from web, then paste the URL.

The feed is generated live from the booking database. Calendar apps decide how often to refresh subscribed calendars, so changes are automatic but may not appear instantly in every calendar app.

## Deploying

This project can run anywhere that supports Python and persistent storage, such as a small VPS, Railway, Fly.io, or another container host.

Important: SQLite must live on persistent storage in production. Set `DATABASE_PATH` to a path on the host's persistent disk/volume. If the host uses an ephemeral filesystem, the database will be erased when the service restarts.

Example production environment variables:

```text
APP_NAME=Walks with Jack
TIMEZONE=America/Toronto
ADMIN_PASSWORD=<strong private password>
CALENDAR_TOKEN=<long random token>
SECRET_KEY=<long random secret>
BOOKING_DAYS_AHEAD=60
DATABASE_PATH=/data/walks.db
```

## Rules currently enforced

- Walks are free.
- Each walk lasts exactly 5 minutes.
- Slots begin every 5 minutes.
- Walks may run only from 8:00 AM through 2:15 PM.
- A booking therefore may start from 8:00 AM through 2:10 PM.
- A single email can have no more than two active bookings on one date.
- A time slot can only be booked once.
- Past times are hidden and cannot be booked.

## Easy future upgrades

- Email confirmations and cancellation links
- Jack's custom blocked-off times/days
- User accounts
- SMS reminders
- Multiple walkers/staff members
- PostgreSQL/Supabase instead of SQLite
- Booking notes or meeting locations
