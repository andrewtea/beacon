# Aircraft positions: what Beacon keeps of adsb.lol's data, and how

Beacon's aircraft positions come from [adsb.lol](https://www.adsb.lol), whose API is open to everyone and whose data is made available under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/) (ODbL). This page is what that license asks Beacon to tell anyone who is shown something made from the data. The notice beside an aircraft in the app links here.

It is versioned with the code it describes: the method below is `parse_aircraft` and `AdsbLolAircraft` in [`server/beacon/ingest/adsblol.py`](../server/beacon/ingest/adsblol.py), and a change to one is a change to the other in the same commit.

## The license of Beacon's aircraft rows

The aircraft rows in a live Beacon evidence store (`kind` `aircraft`, `source` `adsblol`, `origin` `live`) are a database derived from adsb.lol's. **Beacon makes them available under the Open Database License 1.0**, the same license as the database they are derived from (ODbL 4.4). What the app shows from them (a line of an incident's timeline, the circling-aircraft signal, a ground of a tap's match, a count in the agent trace, a path on the map) is a Produced Work, and carries the notice the license asks for (ODbL 4.3): "Contains information from adsb.lol, which is made available here under the Open Database License (ODbL)", with a link to adsb.lol and a link to the license.

Beacon does not hand out copies of its evidence store. In place of a copy, the license lets Beacon offer the method by which its rows differ from adsb.lol's database (ODbL 4.6 b). That method is the rest of this page, free of charge. Applied to the same answers of adsb.lol's API, it makes the same rows. It cannot make Beacon's rows again after the fact: adsb.lol's API answers with where aircraft are now, and Beacon keeps no copy of an answer, so what the method gives anyone else is the rows of the answers they get when they ask.

**The invented scenario is not adsb.lol's data.** A Beacon server that runs with no live source (the default, and the public demo) shows an invented helicopter over an invented fire. Its positions are written in adsb.lol's format so that the same poller reads them, and they hold nothing from adsb.lol: they carry no adsb.lol notice, their feed is labeled "Aircraft positions", and they are marked as invented.

## The method

### What is asked for, and how often

One request at a time, to `GET https://api.adsb.lol/v2/point/37.7700/-122.4400/7`: the aircraft within 7 nautical miles of the middle of San Francisco, which reaches every corner of the box Beacon covers (latitude 37.70 to 37.84, longitude -122.53 to -122.35; `SAN_FRANCISCO` in [`server/beacon/ingest/area.py`](../server/beacon/ingest/area.py) is the authority for the numbers).

The rows are a sample in time. With no key Beacon asks once every 174 seconds (a day divided by adsb.lol's allowance of 500 requests, less 4), and with a key as often as the operator says the key allows, never more often than every 10 seconds. The interval is counted from the real time just before a request, is kept across restarts, and is made longer by a wait adsb.lol asks for (`Retry-After` on an answer that is no success, up to a week), so the gap between two requests is at least that long and can be much longer. Nothing is asked for again later, and only the positions an answer to one of Beacon's requests held are sampled. The interval spaces Beacon's requests and not the times of its rows: a row's time is when the aircraft was at the position, which an answer gives as up to a minute before the request (`seen_pos`, below).

### What is kept of an answer

An answer lists aircraft under `ac`, with `now`, the moment of the answer in milliseconds. One row is made for an aircraft when all of this holds, and none otherwise:

- `hex` is six hexadecimal digits: an ICAO 24-bit address. An address that begins with `~` is not the aircraft's own and is left out.
- `lat`, `lon`, and `seen_pos` are numbers, and `seen_pos` (the age of the position in seconds) is from 0 to 60.
- `alt_baro` is not the word `ground`, and a height is given: `alt_geom` when it is a number, else `alt_baro`.
- That height is below 3,000 m.
- The position is inside the box above.

So Beacon's rows leave out every aircraft on the ground, at or above 3,000 m, outside San Francisco, with a position more than a minute old, or under a non-ICAO address.

### How a kept aircraft is converted

| Row field | From | Conversion |
| --- | --- | --- |
| time of the row | `now`, `seen_pos` | `now / 1000 - seen_pos`, as a UTC time |
| point | `lat`, `lon` | none |
| `icao24` | `hex` | lower case |
| `callsign` | `flight` | spaces trimmed; null when empty, absent, or longer than 16 characters |
| `altitude_m` | `alt_geom`, else `alt_baro` | feet × 0.3048, rounded to 0.1 m |
| `speed_mps` | `gs` | knots × 1852 / 3600, rounded to 0.01 |
| `track_deg` | `track` | none |
| `climb_mps` | `geom_rate`, else `baro_rate` | feet per minute × 0.3048 / 60, rounded to 0.01 |
| `squawk` | `squawk` | kept when it is text of at most four characters |
| `category` | `category` | the emitter category as a word: A1 `light`, A2 `small`, A3 `large`, A4 `high_vortex_large`, A5 `heavy`, A6 `high_performance`, A7 `rotorcraft`, B1 `glider`, B2 `lighter_than_air`, B4 `ultralight`, B6 `unmanned`; null for any other |
| `position_source` | `mlat`, `tisb`, `type` | `mlat` when the answer's `mlat` list names `lat` or `lon`; else `tisb` when its `tisb` list does; else from `type`: `adsb_icao` and `adsb_icao_nt` are `adsb`, `adsr_icao` is `adsr`, `tisb_icao` is `tisb`, `adsc` is `adsc`, `mlat` is `mlat`; anything else, and a list that cannot be read, is `other` |
| confidence | `position_source` | `high` for `adsb`, `medium` for every other |

Every other field of an answer is dropped: registration, aircraft type, signal strength, navigation accuracy, and the rest.

### How a row is stored

A row is named by `source` `adsblol` and a reference `<icao24>@<whole seconds of the row's time>`. The store keeps one row under a name, so an aircraft that adsb.lol lists twice with the same position time is stored once. A row is never changed after it is stored and holds nothing but the fields above, the moment it was stored, and where it came from (`origin` `live`). No row is merged with another: tracks, and whether an aircraft circled, are worked out from the rows each time they are read ([`server/beacon/incident/aircraft.py`](../server/beacon/incident/aircraft.py)) and are not part of the database.
