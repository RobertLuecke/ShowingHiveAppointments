# ShowingHive Extended Application

This repository originally contained a simple Flask demo. To illustrate what a more feature‑rich platform might include, we built an extended version of the app branded for ShowingHive. The extended app isn't committed here because it's large, but you can download it from the project discussion thread.

## Key additions

- Property management and blocked times for showings.
- Scheduling that prevents overlapping bookings and respects blocked time.
- Approval, decline and rescheduling; approval generates a one‑time lockbox code.
- Feedback collection for buyers after a showing.
- Lockbox codes accessible via an endpoint.
- Buyer tours combining multiple approved showings into an itinerary.
- Seller dashboards summarizing upcoming showings, blocked times and feedback.

The extended app uses in‑memory data structures and simplified logic. It's a demonstration only; it does not integrate real maps or lockbox hardware.
