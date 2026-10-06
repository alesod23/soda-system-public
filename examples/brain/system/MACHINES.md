# Machines (example)

One always-on server runs every listener and every night job: the approval hub (:4180), the brain door (:4150) with
Postgres + pgvector, the notification daemon, the system agent (every 10 minutes). A laptop runs what needs a
browser. Git carries the ledgers between them; nothing else is shared.
