# Run sheet: the three-minute video

This is for whoever films Beacon's demo video. For each scene it says what to open, what to press, what appears, how long that takes, and one sentence to say that is true. It is written from what was seen on Nov 28, 2026, on the server [`scripts/film`](../scripts/film) runs, with Muse Ember telling the invented story ([README, "Showing the story"](../README.md#showing-the-story)). Two things in it were seen only on a stand-in's telling of the same story, and each says so.

## What the video shows, and what it does not

- **The incident is invented.** It is a made-up three-alarm fire on the 400 block of Hayes St. Its radio calls are synthesized speech of invented words, and its notices are invented PDFs. Every page says "Invented scenario", and the video says so in words in scene 2.
- **Muse Ember does the model's work, for real.** It hears each call, reads each notice, picks the points of the incident's story, and investigates each tap. The tokens, times, and costs on the trace are the ledger's.
- **A tap is sent from an invented place**, "Near the invented fire, Hayes Valley". The filming server takes nothing of a visitor's own, as the public deployment does not: no place, sound, photo, or notification address. So the video does not show the phone's microphone, its camera, or a notification arriving. Say nothing that suggests it does.
- **No real radio is in it.** Do not say any of these, which the build plan's scene table had and which are not true: "a full day of San Francisco radio", "thousands of transmissions", "minutes ahead of the record", or any accuracy of Beacon on real radio.

## Before filming

One telling of the story with Muse Ember is paid for, call by call. The first telling from minute 12 to the end, with two taps, cost $0.018 (50 requests). Each tap Muse Ember has investigated on the story cost between $0.0007 and $0.0013, in three or four requests.

1. **Bring the server up.** In the Amp thread that holds the ledger, send: "Begin the filming server's story anew at minute 0." Or run this in that thread's Terminal, from a checkout of `main`:

   ```sh
   BEACON_DATA_DIR=/home/user/workspace/repo/.amp/in/orb-backup/paid-data \
     BEACON_MOCK_EMBER=live REPLAY=0 EMBER_SPENT_BEFORE_USD=0 \
     BEACON_MOCK_EMBER_BUDGET_USD=0.40 BEACON_SCALE_RUN=ember-sample-1 \
     scripts/film anew 0
   ```

   It sets the last telling's store aside, deletes nothing, builds the app, starts the server, and prints the portal's address and the clock time of every moment of the story. `BEACON_DATA_DIR` is the one data directory that has been paid from: its ledger counts every earlier request. Do not point the command at another.
2. **Open the portal's address on the phone**, signed in to Amp. It is HTTPS and opens for whoever can open the thread.
3. **Read the clock times.** `scripts/film where`, with the same settings, prints them again and says which minute the story is in. The story runs by the clock, once, for 95 minutes, and cannot be held or moved: only `anew` begins it again, and each `anew` is paid for.

The story's minutes, for a telling begun at minute 0:

| Minute | What comes | Scene |
| --- | --- | --- |
| 0 | Dispatch to a structure fire, 400 block of Hayes St | |
| 3 | Engine 36 on scene, smoke showing | 2: tap between minute 3 and minute 6 |
| 4.2 | A working fire is declared | 2: the card changes by itself |
| 6 | Second alarm | Do not tap again until minute 20 |
| 9 | 511 lists Hayes St as closed | |
| 14 | An evacuation of the building next door is mentioned on the radio | |
| 16 | A shelter-in-place warning for smoke is issued | |
| 18 | Third alarm | |
| 19 | The evacuation order is published as a PDF | It is on the page once Muse Ember has read it: 84 seconds later, the one time this was timed |
| 21 to 30 | | 4: the second tap |
| 74 | The order is lifted, in a second PDF | |
| 78 | "All clear" on the radio | |
| 91 | The last units are cleared | 2, last shot: the record is closed |

**Why no tap between minute 6 and minute 20.** Crisis mode is decided when a tap near the incident is investigated, and only the first decision for an incident turns it on. From minute 6 the incident has signals a decision can stand on, so a tap then can turn crisis mode on before the evacuation order exists. Before minute 6 it has none, and crisis mode stands only on signals the incident has: neither tap that was sent before minute 6 turned it on.

## The scenes

Times are the build plan's. What appears is quoted from the app.

### 1. The button (0:00 to 0:20)

- **Open:** the portal's address. Film the first screen.
- **Appears:** "Beacon", the tag "Invented scenario", the lines "The incidents, alerts, and notices here are made up." and "Nobody has checked what the hosting service this server is reached through writes down of a request.", and the What's that? button.
- **Say:** "You hear a siren and wonder what is going on. Beacon is one button: What's that?"

### 2. One tap, one answer (0:20 to 0:55)

- **When:** story minute 3 to 6. For the card to change on camera, tap about half a minute before minute 4.2.
- **Press:** What's that?
- **Appears:** "Looking into it" for four to fifteen seconds, then the card: "Fire at the 400 block of Hayes St", "Open in Beacon's record", "Invented", "The radio said: units on scene.", the units named on the radio (B2, E21, E36, T6), the first radio call's time, and under it "Medium confidence" and "Generated radio calls, synthesized speech and no radio, as Ember heard them". Scroll to "Why this incident" and the timeline: each line has its confidence and its source.
- **Then:** the card asks again by itself, with no press. A change shows up to a minute after its moment in the story, because Muse Ember hears the call first: the one time this was timed, the card said "The radio said: crews at work." 37 seconds after minute 4.2. After minute 6 it lists nine units and "Alarm level on the radio 2".
- **Last shot, filmed after minute 91:** open the same answer again from "Your taps" on the button's page. The card says "Closed in Beacon's record". (Seen on a stand-in's telling.)
- **Say:** "This fire is invented, and its radio calls are synthesized speech. Muse Ember heard each call, and when I tap, it investigates and answers in seconds: what, which crews, which block, with a source and a confidence on every line."
- **Say over the last shot:** "When the radio clears the call, Beacon's record of it closes."

### 3. The agent trace (0:55 to 1:15)

- **Press:** "See each step of the investigation", on the answer.
- **Appears:** "How Beacon investigated this tap", "Beacon's investigator is a model, Muse Ember. Its reading can be wrong.", then the totals (requests answered, tokens in and out, time, cost), then each round: "Answered by Muse Ember", its tokens, time, and cost, the tool it called and why, and what the tool found. Both times a tap between minute 3 and minute 4 was tried it was two rounds and an answer: `nearby_incidents` first, and then `get_official`, once by itself and once with `get_radio` and `nearby_taps`.
- **Say:** "Every step is kept. Ember chose which tools to call, code checked its answer, and each request shows its tokens, its time, and its cost." Then read the cost off the screen: it was $0.000797 and $0.000688 the two times.
- **Do not say** that Ember pulled a helicopter's track: at minute 3 the story has no helicopter, and Ember called no aircraft tool in the runs that were seen.

### 4. Crisis mode (1:15 to 1:50)

- **When:** story minute 21 to 30, with no tap sent since minute 6.
- **Press:** "Beacon" at the top, then What's that? again.
- **Appears:** "Looking into it" again (eight seconds, the time this was filmed), then the card, now with twelve units and "Alarm level on the radio 3", and under it the panel "Crisis mode is on": "Beacon's investigator turned on crisis mode for this incident, because of the signals the incident had at that time. That is the investigator's decision, not an authority's. Beacon relays what authorities publish and tells nobody what to do." Scroll to "From authorities": the "Shelter In Place Warning" as an alert in force, and the "Evacuation order", "Published", shown as "Beacon's reading of the document, not the authority's words" with "Read the document" beside it. Under "Key points": "An evacuation was mentioned on the radio. That is radio traffic, not an official order."
- **Then, for the saved place:** press "Your places" on the button's page, then "Save 'Near the invented fire, Hayes Valley'". It says "Beacon has this incident about 100 m from this place."
- **Say:** "Later in the story it is a third alarm. Ember's investigation of this tap turned crisis mode on: its decision, which code checked against the incident's own signals. The evacuation order is shown as a published document with its link, and the evacuation heard on the radio is labeled as radio traffic, not an order."
- **Say over the saved place:** "A saved place stays on the phone. Beacon says how far the incident is from it, and never whether to stay or go."
- **Its trace, as an extra shot:** the time this was filmed it was three requests for $0.001113: `nearby_incidents`, then one round of `get_official`, `get_radio`, `get_aircraft`, and `nearby_taps`, then the answer. Say that Ember checked the aircraft only if `get_aircraft` is on the screen.
- **Do not say** that a notification arrives. Notifications are built and tested against stand-ins for the push services; none has been delivered to a phone, and the filming server takes no notification address.
- **If filmed after minute 78:** the incident's page has "Where sources disagree", which says the official source holds over the all-clear that was reported on the radio. (Seen on a stand-in's telling.)

### 5. Scale (1:50 to 2:40)

- **Open:** "Scale run results", at the foot of any page. On the filming server it is Muse Ember's paid run of a sample. A desktop browser shows more of it at once.
- **Appears:** what a scale run is, then what this run is ("Generated speech, for Muse Ember to hear. ..."), then each number with its sentence and the tag of the run's kind beside it. The ones to stop on: "DataSF calls in the sample" (100 calls), "Transmissions heard" (288 calls), "Cost of hearing" ($0.0345), "Cost per answered investigation, mean" ($0.0005), "Time per answered investigation, median" (4.5 s), and under "Glossary", "Codes the glossary added, in agreement with DataSF" (5 codes).
- **Say:** "For scale, Beacon generated speech from DataSF's public record of one day of fire calls. Muse Ember was paid to hear a sample, the first hundred calls of that day, 288 transmissions, for three and a half cents. A script replayed 34 taps against them, and an investigation cost a twentieth of a cent on average and took Ember a median of four and a half seconds. This is clean synthetic speech and not real radio, and the page says so beside every number."
- **Say, for the glossary:** "From a table of counts alone, Ember proposed what five unit codes nobody had written down stand for, and all five agreed with DataSF's unit types. A model can also simply know a fire department's codes."
- **For the whole day, optional:** the public deployment's results page (<https://beacon-sf--andrewtea.onamp.dev/#/results>) is a stand-in's run of all of that day's 1,525 generated calls. **Say:** "A stand-in ran the whole day through the same pipeline. That checks the pipeline and measures nothing of Ember."

### 6. Cost (2:40 to 3:00)

- **Open:** the trace of scene 3 again, at its totals, or a terminal with the ledger's total:

  ```sh
  cd server && BEACON_DATA_DIR=/home/user/workspace/repo/.amp/in/orb-backup/paid-data \
    EMBER_SPENT_BEFORE_USD=0 uv run python -m beacon.ember
  ```

  It prints "Live spend", asks Muse Ember nothing, and writes the token trace the submission needs (`ember/trace.jsonl` in the data directory).
- **Say:** "Everything Muse Ember has done for Beacon, every call heard, notice read, story, and investigation, has cost" and then the live spend the command printed. On Nov 28, 2026, after 625 requests, it was $0.115236. "Telling this fire once, from its twelfth minute to its end, cost under two cents."

## When something differs

- **Ember's hearing differs from telling to telling.** Of the same synthesized words it once heard "100 block" for "400 block". If a card shows something the story does not say, that is the model's hearing, shown as such: film it, or begin the story anew.
- **The answer is "unconfirmed".** That is an answer too: the investigator did not match the tap to an incident. Tap again, and mind that a tap after minute 6 can turn crisis mode on.
- **Crisis mode did not come on at the second tap.** It is Ember's decision and it can decide otherwise. Each time a first tap from minute 6 on was tried, it turned it on. Tap again.
- **The orb was paused.** The story went on by the clock. `scripts/film where` says where it is; `scripts/film anew 0` begins it again.
- **`scripts/film` refuses.** Its sentence says why, and nothing was stopped, moved, or started. It never begins a ledger and never changes a spending setting.
