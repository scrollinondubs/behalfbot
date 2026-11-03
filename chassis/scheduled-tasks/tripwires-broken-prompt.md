# TripWires heartbeat is broken

The `tripwires` heartbeat normally never invokes a model. You are running
because it reported `count: 1`, meaning the heartbeat itself has a problem and
tripwires may be silently unwatched or their messages undelivered. The gathered
JSON gives the reason:

- `store_unreadable`: `state/tripwires/tripwires.json` is missing or invalid.
- `send_failed`: a tripwire message could not be delivered. It is retried each
  tick and nothing is lost yet.

Post one short message to the operator through the usual channel: name the
reason and say that tripwires may not be reporting until it is fixed.

Do not edit, repair or delete the store. Do not fetch any tripwire URL. Do not
retry the check.
