# Moobby Incident Gateway API

The local read-only API exposes only `GET /api/v1/incidents` from
`serving.gateway_corridor_incidents`. The deployed view applies publication and
evidence eligibility. The API does not query raw snapshots, event reviews, or
other serving tables directly.

## Response

The endpoint returns a JSON array. Every item contains the 12 projection fields
with their exact camelCase names:

`id`, `title`, `category`, `locationName`, `latitude`, `longitude`,
`nearestStationId`, `nearestStationName`, `distanceToStationKm`, `publishedAt`,
`sourceName`, `verificationStatus`.

`publishedAt` is RFC 3339 UTC (`YYYY-MM-DDTHH:MM:SS[.fraction]Z`). Unknown
location, station, distance, source, and publication values remain JSON `null`.
`GET /api/v1/incidents` returns `[]` when the view has no eligible incidents.

Pagination uses `?limit=20&offset=0`; `limit` must be 1–100 and `offset` a
non-negative integer. Results are ordered by publication time descending,
with null timestamps last, then by incident ID. Invalid pagination returns 400.
Database errors return a generic 503 body; driver details are not returned.
`GET /health` is a process liveness endpoint and does not test database
connectivity.

## Value mapping

The API passes through the serving projection's internal category values:
`delay`, `service_suspension`, `route_change`, `signal_or_power_issue`,
`infrastructure_issue`, `accident_or_safety_issue`, `overcrowding`,
`flooding_or_weather`, `other`, and `unknown`.

`verificationStatus` passes through `unverified`, `corroborated`, `disputed`, or
`inconclusive`. The API does **not** translate `corroborated` to `verified`.
The Moobby application must accept these values or agree on an explicit adapter
contract before any mapping is added.

## Database login

Provision a dedicated LOGIN role named `moobby_incident_gateway_login` and grant
only membership in the existing `moobby_incident_gateway` NOLOGIN role. The
database password must be provisioned out of band. Do not reuse the administrator,
curator, collection, processing reviewer, or Orvix worker login. For Supabase
Session Pooler, use the project-qualified username form if required by the
connection endpoint, such as `moobby_incident_gateway_login.<project-ref>`.

Create `data/automation/incident/gateway_connection.json` with `host`, `port`,
`dbname`, `user`, and `password`, then restrict it:

```sh
chmod 600 data/automation/incident/gateway_connection.json
```

The API rejects any config file without exact mode `0600`, and rejects usernames
other than the dedicated gateway login (optionally project-qualified). It also
forces TLS and a short connection timeout.

## Local use

Install the existing repository requirements, configure the private file, then:

```sh
python -m scripts.incident_gateway_api --host 127.0.0.1 --port 8080
```

In another terminal:

```sh
curl -i 'http://127.0.0.1:8080/health'
curl -i 'http://127.0.0.1:8080/api/v1/incidents?limit=20&offset=0'
```

Offline tests do not connect to PostgreSQL or start an HTTP service:

```sh
python -m unittest tests.test_incident_gateway_api
```
