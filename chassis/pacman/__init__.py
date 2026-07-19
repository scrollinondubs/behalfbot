"""chassis.pacman - the Pacman queue and its approval-token scheme.

Pacman is chassis-core, so this lives under chassis/ rather than plugins/.
The 4-gate pipeline itself is prose, not code - it lives in
chassis/skills/pacman.md. What is here is the durable state the prose depends
on: the Postgres-backed queue and the backend-independent approval token that
replaced SiYuan block IDs.
"""
