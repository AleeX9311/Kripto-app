# Koledar

CPI and FOMC dates come from the Fair Economy weekly calendar (USD, title contains CPI or FOMC, speeches excluded). Times are stored in UTC and shown in Europe/Ljubljana.

Rules, from the prompt:

- No new entries from 30 minutes before the release until 30 minutes after.
- The first 15 minutes after the release are inside that blackout.
- After the blackout, new entries are half size (0.5% risk) for the rest of that UTC day.
- If a gap through the stops would push the day through the 3% loss limit, size is cut before the release.
- Wide spreads are assumed. The fund does not trade the headline.

The running bot writes the actual timestamps for the current week into the in-app calendar. This file is the rule. It is not a guessed list of future dates.
