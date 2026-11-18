# Incidenti

The bot appends runtime incidents to `incidents.jsonl` on the `fund-data` branch. This file is the index.

What counts: a kill switch, a missing stop, a failed paper order, a data source that does not answer, a rule break.

What does not count: a strategy that loses inside its stop. That is a trade, and it goes in the journal.
